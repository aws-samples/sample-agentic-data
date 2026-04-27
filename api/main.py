import os
"""
Agentic Data — FastAPI Backend
SSE streaming + REST data + config management
All state persisted to DynamoDB.
"""
import json, os, sys, time, threading, queue, uuid
from datetime import datetime, timezone
from decimal import Decimal

import boto3
from boto3.dynamodb.conditions import Key, Attr
from fastapi import FastAPI, Request, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse, JSONResponse
from pydantic import BaseModel
from typing import Optional

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from auth import AuthMiddleware, get_current_user, check_rate_limit, _decode_jwt_payload
from agentic_core.user_memory import load_user_memory, save_user_memory, format_memory_for_prompt, extract_memory_from_conversation
from agentic_core.db_engine import _safe_identifier as _safe_id
from config import (REGION, EVENTS_TABLE, DATA_BUCKET, REPORTS_BUCKET,
    CHAT_TABLE, COST_TABLE, CONFIG_TABLE, FEEDBACK_TABLE,
    AUTH_ENABLED, COGNITO_USER_POOL_ID, COGNITO_CLIENT_ID, COGNITO_REGION,
    COGNITO_DOMAIN, COGNITO_REDIRECT_URI, COGNITO_LOGOUT_URI,
    RATE_LIMIT_PER_MINUTE, DEFAULT_SUPERVISOR_MODEL, DEFAULT_SUB_AGENT_MODEL)

class DecimalEncoder(json.JSONEncoder):
    def default(self, o):
        if isinstance(o, Decimal): return float(o)
        return super().default(o)

def _json(obj):
    return json.loads(json.dumps(obj, cls=DecimalEncoder, ensure_ascii=False))

# ═══ Auto-routing: detect scenario from semantic layer keywords ═══
_route_cache = {"ts": 0, "athena": set(), "pg": set()}

def _build_route_keywords():
    """Build domain keyword sets from semantic layer (cached 60s)."""
    import time
    now = time.time()
    if now - _route_cache["ts"] < 60 and _route_cache["athena"]:
        return _route_cache["athena"], _route_cache["pg"]
    
    try:
        resp = _ddb.Table(CONFIG_TABLE).get_item(Key={"config_key": "semantic_layer"})
        raw = resp.get("Item", {}).get("data", resp.get("Item", {}).get("value", "{}"))
        data = json.loads(raw) if isinstance(raw, str) else raw
        metrics = data.get("metrics", {})
        dims = data.get("dimensions", {})
        syns = data.get("synonyms", {})
        
        athena_words = set()
        pg_words = set()
        
        # Build set of Athena databases for dynamic detection
        _athena_dbs = {ds.get("database", "").lower() for ds in _custom_data_sources if ds.get("type") == "athena"}
        
        def _is_athena_table(tbl):
            return any(db and db in tbl.lower() for db in _athena_dbs) or (isinstance(tbl, str) and "." in tbl and not any(s in tbl for s in ["public.", "aftermarket."]))
        
        for name, m in metrics.items():
            if not isinstance(m, dict): continue
            tbl = m.get("table", "")
            engine = m.get("engine", "")
            words = {name}
            if m.get("description"): words.add(m["description"])
            if engine == "athena" or _is_athena_table(tbl):
                athena_words.update(words)
            elif engine == "postgresql" or any(x in tbl for x in ["public.", "aftermarket."]) or (tbl and "." not in tbl):
                pg_words.update(words)
        
        for name, d in dims.items():
            if not isinstance(d, dict): continue
            tbl = d.get("table", "")
            engine = d.get("engine", "")
            words = {name}
            if d.get("description"): words.add(d["description"])
            if engine == "athena" or _is_athena_table(tbl):
                athena_words.update(words)
            elif engine == "postgresql" or any(x in tbl for x in ["public.", "aftermarket."]) or (tbl and "." not in tbl):
                pg_words.update(words)
        
        # Map synonyms via their target metric
        athena_metrics = {n for n, m in metrics.items() if isinstance(m, dict) and (m.get("engine") == "athena" or _is_athena_table(m.get("table", "")))}
        pg_metrics = {n for n, m in metrics.items() if isinstance(m, dict) and (m.get("engine") == "postgresql" or any(x in m.get("table", "") for x in ["public.", "aftermarket."]))}
        for syn_name, target in syns.items():
            if isinstance(target, str):
                if target in athena_metrics: athena_words.add(syn_name)
                elif target in pg_metrics: pg_words.add(syn_name)
        
        # Filter out overly technical words (table.column format) — keep human-readable ones
        athena_words = {w for w in athena_words if "." not in w and len(w) >= 2}
        pg_words = {w for w in pg_words if "." not in w and len(w) >= 2}
        
        # Core business terms as fallback (semantic layer auto-gen names are too technical)
        athena_words.update({"里程", "行驶", "车型", "vin", "油耗", "速度", "急刹", "刹车", "加速",
            "保有量", "新能源", "bev", "phev", "燃油", "纯电", "混动", "行程", "驾驶",
            "车联网", "mileage", "a4", "a6", "a8", "q5", "q7", "q3", "q6", "q8", "etron", "e-tron"})
        pg_words.update({"产量", "产线", "生产线", "工厂", "质检", "合格率", "良品率", "缺陷",
            "设备", "停机", "班次", "产能", "日产", "长春", "佛山", "上海", "生产", "质量",
            "manufacturing", "production", "quality", "defect", "inspection",
            "维修", "保养", "经销商", "dealer", "工单", "售后", "满意度", "反馈",
            "配件", "工时", "保修", "召回", "客诉", "repair", "warranty"})
        
        _route_cache.update({"ts": now, "athena": athena_words, "pg": pg_words})
        print(f"[AutoRoute] Rebuilt keywords: {len(athena_words)} athena, {len(pg_words)} pg")
    except Exception as e:
        print(f"[AutoRoute] Failed to build keywords: {e}")
    
    return _route_cache["athena"], _route_cache["pg"]


def _detect_sql_engine(sql: str) -> str:
    """Detect SQL engine from SQL text. Returns 'athena', 'snowflake', or 'postgresql'."""
    sql_lower = sql.lower()
    # Check if SQL contains any Athena database prefix
    for ds in _custom_data_sources:
        if ds.get("type") == "athena":
            db = ds.get("database", "").lower()
            if db and f"{db}." in sql_lower:
                return "athena"
    # Check Snowflake patterns (DATABASE.SCHEMA.TABLE)
    for ds in _custom_data_sources:
        if ds.get("type") == "snowflake":
            config = ds.get("config", {})
            db = config.get("database", "").lower()
            if db and f"{db}." in sql_lower:
                return "snowflake"
    # Check MySQL (RDS with engine=mysql or backtick syntax)
    for ds in _custom_data_sources:
        if ds.get("type") == "RDS":
            engine = ds.get("config", {}).get("engine", "").lower()
            if engine == "mysql":
                return "mysql"
    if '`' in sql:
        return "mysql"
    # Default: if no prefix found, assume postgresql
    return "postgresql"


def _detect_sql_datasource(sql: str) -> str:
    """Detect datasource ID from SQL text."""
    sql_lower = sql.lower()
    for ds in _custom_data_sources:
        if ds.get("type") == "athena":
            db = ds.get("database", "").lower()
            if db and f"{db}." in sql_lower:
                return ds.get("id", "")
    # Fallback: first PG datasource
    for ds in _custom_data_sources:
        if ds.get("type") == "postgresql":
            return ds.get("id", "")
    return ""


def _auto_route_scenario(question: str) -> str:
    """Detect which scenario to use based on semantic layer keywords. Returns scenario_id or empty string."""
    q = question.lower()
    athena_kw, pg_kw = _build_route_keywords()
    
    has_athena = any(kw in q for kw in athena_kw)
    has_pg = any(kw in q for kw in pg_kw)
    
    if has_athena and has_pg:
        print(f"[AutoRoute] Cross-domain detected")
        return "cross_domain"
    elif has_athena:
        print(f"[AutoRoute] Vehicle/Athena domain detected")
        return "vehicle_analytics"
    elif has_pg:
        print(f"[AutoRoute] Manufacturing/PG domain detected")
        return "manufacturing"
    else:
        print(f"[AutoRoute] No domain match, Supervisor fallback")
        return ""

_ddb = boto3.resource("dynamodb", region_name=REGION)
_s3 = boto3.client("s3", region_name=REGION)

def load_events():
    return _json(_ddb.Table(EVENTS_TABLE).scan().get("Items", []))

def load_vehicles():
    try:
        return json.loads(_s3.get_object(Bucket=DATA_BUCKET, Key="data/vehicle_info.json")["Body"].read())
    except _s3.exceptions.NoSuchKey:
        return []
    except Exception:
        return []

# ─── Persistence Layer ───

def _save_config(key, data):
    """Save config item to DynamoDB."""
    _ddb.Table(CONFIG_TABLE).put_item(Item={"config_key": key, "data": json.dumps(data, ensure_ascii=False), "updated_at": datetime.now(timezone.utc).isoformat()})

def _load_config(key, default=None):
    """Load config item from DynamoDB."""
    try:
        resp = _ddb.Table(CONFIG_TABLE).get_item(Key={"config_key": key})
        if "Item" in resp:
            return json.loads(resp["Item"]["data"])
    except Exception as _e:

        print(f"[WARN] swallowed exception: {_e}")
    return default

def _save_cost_record(record):
    """Save a cost record to DynamoDB."""
    now = datetime.now(timezone.utc)
    item = {
        "date": now.strftime("%Y-%m-%d"),
        "id": now.isoformat(),
        "timestamp": now.isoformat(),
        **{k: Decimal(str(v)) if isinstance(v, float) else v for k, v in record.items()}
    }
    _ddb.Table(COST_TABLE).put_item(Item=item)

def _load_cost_records(date=None):
    """Load cost records from DynamoDB. Default: today."""
    if not date:
        date = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    try:
        resp = _ddb.Table(COST_TABLE).query(KeyConditionExpression=Key("date").eq(date))
        return _json(resp.get("Items", []))
    except Exception:
        return []

def _save_chat_message(session_id, role, content, meta=None, user_id=""):
    """Save a chat message to DynamoDB."""
    _ddb.Table(CHAT_TABLE).put_item(Item={
        "session_id": session_id,
        "user_id": user_id or "demo_user",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "role": role,
        "content": content[:10000],  # Truncate very long content
        **({"meta": json.dumps(meta, ensure_ascii=False)} if meta else {}),
    })

def _load_chat_sessions(user_id="", role="viewer"):
    """Load all unique session IDs with their latest message, filtered by user."""
    try:
        scan_kwargs = dict(ProjectionExpression="session_id, #ts, #r, content, user_id",
            ExpressionAttributeNames={"#ts": "timestamp", "#r": "role"})
        if user_id and role != "admin":
            scan_kwargs["FilterExpression"] = Attr("user_id").eq(user_id)
        resp = _ddb.Table(CHAT_TABLE).scan(**scan_kwargs)
        items = resp.get("Items", [])
        sessions = {}
        first_user_msg = {}
        for it in items:
            sid = it["session_id"]
            # Track latest message for ordering
            if sid not in sessions or it["timestamp"] > sessions[sid]["timestamp"]:
                sessions[sid] = it
            # Track first user message for title
            if it.get("role") == "user" and (sid not in first_user_msg or it["timestamp"] < first_user_msg[sid]["timestamp"]):
                first_user_msg[sid] = it
        return [{"session_id": k, "title": first_user_msg.get(k, {}).get("content", v.get("content", ""))[:30], "last_time": v["timestamp"]} for k, v in sorted(sessions.items(), key=lambda x: x[1]["timestamp"], reverse=True)]
    except Exception:
        return []

def _load_chat_history(session_id):
    """Load chat messages for a session."""
    try:
        resp = _ddb.Table(CHAT_TABLE).query(KeyConditionExpression=Key("session_id").eq(session_id), ScanIndexForward=True)
        return _json(resp.get("Items", []))
    except Exception:
        return []


# Agent management
_agents = {}
_agent_versions = {}  # session_id -> chatbi_count at agent creation time
_agent_scenarios = {}  # session_id -> scenario_id
_configs = _load_config("global_config", {})  # Load on startup
if isinstance(_configs, dict) and "global" not in _configs:
    _configs = {"global": _configs}
_custom_data_sources = _load_config("custom_datasources", [])  # Load on startup

# Load user-uploaded ChatBI datasets
_custom_chatbi = _load_config("custom_chatbi_datasets", {})
if _custom_chatbi:
    try:
        from agentic_core.tools import CHATBI_DATASETS
        CHATBI_DATASETS.update(_custom_chatbi)
        print(f"[Startup] Loaded {len(_custom_chatbi)} custom ChatBI datasets")
    except Exception as e:
        print(f"[Startup] Failed to load custom ChatBI datasets: {e}")

# In-memory cost cache (also persisted per record)
_cost_data = []

TOOL_LABELS = {
    
    
    
    "save_report": "保存报告",
    "list_reports": "历史报告", 
    "deep_data_analysis": "数据分析(ChatBI)",
    "get_data_catalog": "数据字典", "nl2sql_query": "NL2SQL 查询",
    
    "semantic_query": "语义层查询",
}

# Track data version: bump on any datasource/semantic change
_data_version = [0]

def bump_data_version():
    _data_version[0] += 1

_chatbi_last_load = [0]
_chatbi_last_count = [0]

def _reload_chatbi(force=False):
    """从 DynamoDB 重新加载 CHATBI 数据集 (30s TTL)."""
    from agentic_core.tools import CHATBI_DATASETS
    from agentic_core.dynamic_context import update_tool_descriptions
    now = time.time()
    if not force and (now - _chatbi_last_load[0]) < 30 and _chatbi_last_count[0] == len(CHATBI_DATASETS):
        return
    chatbi = _load_config("custom_chatbi_datasets", {})
    if chatbi:
        CHATBI_DATASETS.update(chatbi)
        update_tool_descriptions()
    _chatbi_last_load[0] = time.time()
    _chatbi_last_count[0] = len(CHATBI_DATASETS)

def get_agent(session_id, config=None, force_new=False, user_email="", scenario_id=None):
    from agentic_core import create_supervisor
    from agentic_core.tools import CHATBI_DATASETS
    from agentic_core.dynamic_context import update_tool_descriptions
    cfg = config or _configs.get("global", {})
    
    # 多 worker 同步: 从 DynamoDB 重新加载 (30s TTL)
    _load_semantic_custom()
    _reload_chatbi()
    
    # ⚠️ 永远动态生成 system_prompt, 不使用持久化的旧版本
    # DynamoDB 里的 system_prompt 可能是数据源变更前保存的，已过时
    if isinstance(cfg, dict) and "system_prompt" in cfg:
        cfg = dict(cfg)  # don't mutate shared config
        del cfg["system_prompt"]
    
    # 从 agent_definitions 读取 supervisor 配置的 tools + model
    try:
        all_defs = _get_agent_defs()
        if isinstance(all_defs, dict) and "agent_definitions" in all_defs:
            all_defs = all_defs["agent_definitions"]
        sup_def = all_defs.get("supervisor", {})
        cfg = dict(cfg) if not isinstance(cfg, dict) else dict(cfg)
        if sup_def.get("tools"):
            cfg["supervisor_tools"] = sup_def["tools"]
        # 从 agent_definitions 注入模型配置 (优先级: agent_def > global_config)
        if sup_def.get("model_id") and not cfg.get("supervisor_model"):
            cfg["supervisor_model"] = sup_def["model_id"]
        elif sup_def.get("model_note") and sup_def["model_note"].startswith("custom:") and not cfg.get("supervisor_model"):
            cfg["supervisor_model"] = sup_def["model_note"]
        # Sub-agent model
        for _aid, _adef in all_defs.items():
            if isinstance(_adef, dict) and _adef.get("role") == "sub-agent":
                sub_mid = _adef.get("model_id") or _adef.get("model_note", "")
                if sub_mid and not cfg.get("sub_agent_model"):
                    cfg["sub_agent_model"] = sub_mid
                break
    except Exception as _e:

        print(f"[WARN] swallowed exception: {_e}")
    
    # Check if cached agent's data is stale or scenario changed
    cached_chatbi_count = _agent_versions.get(session_id, -1)
    current_chatbi_count = len(CHATBI_DATASETS)
    cached_scenario = _agent_scenarios.get(session_id)
    if cached_chatbi_count != current_chatbi_count or cached_scenario != scenario_id:
        force_new = True  # 数据变了或场景变了, 强制重建
    
    if force_new or session_id not in _agents:
        # ── Scenario-driven tool & prompt injection ──
        scenario_cfg = None
        if scenario_id:
            try:
                r = _ddb.Table(CONFIG_TABLE).get_item(Key={"config_key": "scenarios"})
                all_scenarios = json.loads(r.get("Item", {}).get("data", "{}"))
                scenario_cfg = all_scenarios.get(scenario_id)
            except Exception as _e:

                print(f"[WARN] swallowed exception: {_e}")
        
        if scenario_cfg:
            cfg = dict(cfg)
            # 1. Supervisor tools: always use defaults (deep_data_analysis + admin tools)
            #    Scenario tools only control DataAnalystAgent's tool set, not Supervisor's
            #    Supervisor delegates to DataAnalystAgent via deep_data_analysis
            
            # 2. Inject scenario prompt_context into DataAnalystAgent
            prompt_ctx = scenario_cfg.get("prompt_context", "")
            if prompt_ctx:
                cfg["scenario_prompt_context"] = prompt_ctx
            
            # 3. Restrict DataAnalystAgent tools  
            cfg["scenario_da_tools"] = [t["name"] if isinstance(t, dict) else t for t in scenario_cfg.get("tools", [])]
            
            # 4. Pass full scenario config for MCP router
            cfg["_scenario_cfg"] = scenario_cfg
            cfg["_scenario_id"] = scenario_id

            # 5. Load skills from agent definition or scenario
            agent_ids = scenario_cfg.get("agents", [])
            skill_ids = []
            if agent_ids:
                agent_defs = _get_agent_defs()
                for aid in agent_ids:
                    for defk, defv in agent_defs.items():
                        if defk == aid or defv.get("name") == aid:
                            skill_ids.extend(defv.get("skills", []))
                            break
            if skill_ids:
                cfg["skills"] = skill_ids


        # Inject user memory into system prompt
        if user_email:
            memory = load_user_memory(user_email)
            memory_section = format_memory_for_prompt(memory)
            if memory_section:
                cfg = dict(cfg)  # don't mutate shared config
                base_prompt = cfg.get("system_prompt", "")
                if base_prompt:
                    cfg["system_prompt"] = base_prompt + memory_section
        _agents[session_id] = create_supervisor(config=cfg)
        _agent_versions[session_id] = len(CHATBI_DATASETS)
        _agent_scenarios[session_id] = scenario_id
    return _agents[session_id]

# FastAPI
app = FastAPI(title="Agentic Data API")
app.add_middleware(AuthMiddleware)

app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

@app.exception_handler(Exception)
async def global_exception_handler(request, exc):
    """Catch unhandled exceptions and return 500 with details."""
    import traceback, logging
    logging.error(f"Unhandled exception: {exc}\n{traceback.format_exc()}")
    return JSONResponse(
        status_code=500,
        content={"error": str(exc), "type": type(exc).__name__}
    )


@app.on_event("startup")
def startup():
    """Load persisted cost records + auto-discover datasource schemas on startup."""
    global _cost_data
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    records = _load_cost_records(today)
    _cost_data = [{"time": r.get("time",""), "duration": r.get("duration",0), "steps": r.get("steps",0),
                   "input_tokens": r.get("input_tokens",0), "output_tokens": r.get("output_tokens",0),
                   "cost": r.get("cost",0), "model": r.get("model","")} for r in records]
    # Load persisted semantic layer customizations on startup
    _load_semantic_custom()
    # Auto-discover schemas for pre-configured datasources that have no table_descriptions yet
    _auto_discover_on_startup()


def _auto_discover_on_startup():
    """Auto-introspect pre-configured datasources and populate table_descriptions + semantic layer."""
    try:
        datasources = _load_config("custom_datasources", [])
        if not datasources:
            return
        updated = False
        for ds in datasources:
            if not ds.get("enabled", True):
                continue
            # Skip if already introspected (has table_descriptions)
            if ds.get("table_descriptions"):
                continue
            ds_type = ds.get("type", "")
            config = ds.get("config", {})
            ds_id = ds.get("id", "")
            print(f"[AutoDiscover] Introspecting {ds_type} datasource: {ds_id}")
            try:
                from agentic_core.schema_inference import (
                    introspect_athena, introspect_sql_engine
                )
                introspection = []
                if ds_type == "Athena":
                    database = config.get("database", ds.get("database", ""))
                    region = config.get("region", REGION)
                    if database:
                        introspection = introspect_athena(database, region)
                elif ds_type in ("RDS", "PostgreSQL"):
                    from agentic_core.db_engine import get_engine
                    engine_key = config.get("engine", "mysql").lower() if ds_type == "RDS" else "postgresql"
                    eng = get_engine(engine_key)
                    if eng:
                        introspection = introspect_sql_engine(eng)
                
                if introspection:
                    # Populate table_descriptions and tables on the datasource
                    all_tables = []
                    all_table_descs = {}
                    all_metrics = {}
                    all_dims = {}
                    all_synonyms = {}
                    for r in introspection:
                        dataset = r.get("dataset", "")
                        if dataset:
                            all_tables.append(dataset)
                        for f in r.get("fields", []):
                            if dataset and f.get("name"):
                                all_table_descs.setdefault(dataset, {"columns": {}})
                                all_table_descs[dataset]["columns"][f["name"]] = {
                                    "type": f.get("sql_type", f.get("type", "TEXT")),
                                    "description": f.get("chinese_name", f["name"]),
                                }
                        all_metrics.update(r.get("metrics", {}))
                        all_dims.update(r.get("dimensions", {}))
                        all_synonyms.update(r.get("synonyms", {}))
                    
                    ds["tables"] = all_tables
                    ds["table_descriptions"] = all_table_descs
                    updated = True
                    print(f"[AutoDiscover] {ds_id}: {len(all_tables)} tables, {len(all_metrics)} metrics, {len(all_dims)} dims")
                    
                    # Auto-apply semantic layer
                    if all_metrics or all_dims or all_synonyms:
                        _load_semantic_custom()
                        if isinstance(_semantic_custom.get("metrics"), list):
                            _semantic_custom["metrics"] = {}
                        if isinstance(_semantic_custom.get("dimensions"), list):
                            _semantic_custom["dimensions"] = {}
                        if isinstance(_semantic_custom.get("synonyms"), list):
                            _semantic_custom["synonyms"] = {}
                        for k, v in all_metrics.items():
                            _semantic_custom.setdefault("metrics", {})[k] = v
                        for k, v in all_dims.items():
                            _semantic_custom.setdefault("dimensions", {})[k] = v
                        for k, v in all_synonyms.items():
                            _semantic_custom.setdefault("synonyms", {})[k] = v
                        _save_semantic_custom()
                        print(f"[AutoDiscover] Semantic layer updated: +{len(all_metrics)} metrics, +{len(all_dims)} dims, +{len(all_synonyms)} synonyms")
                else:
                    print(f"[AutoDiscover] {ds_id}: no tables found")
            except Exception as e:
                print(f"[AutoDiscover] {ds_id} introspect failed: {e}")
        
        if updated:
            _save_config("custom_datasources", datasources)
            print("[AutoDiscover] Datasources updated with table descriptions")
    except Exception as e:
        print(f"[AutoDiscover] Startup auto-discover failed: {e}")

# ─── Data APIs ───

@app.get("/api/events")
def api_events():
    return load_events()

@app.get("/api/vehicles")
def api_vehicles():
    return load_vehicles()

@app.get("/api/overview")
def api_overview():
    global _custom_data_sources
    _custom_data_sources = _load_config("custom_datasources", [])
    all_ds = _data_sources_default + _custom_data_sources
    if not all_ds:
        return {"total_events": 0, "total_vehicles": 0, "by_severity": {}, "by_vehicle": {}, "by_type": {}, "high_risk": [], "vehicles": []}
    try:
        events = load_events()
    except Exception:
        events = []
    try:
        vehicles = load_vehicles()
    except Exception:
        vehicles = []
    by_severity, by_vehicle, by_type = {}, {}, {}
    for e in events:
        s, v, t = e.get("severity",""), e.get("vin",""), e.get("event_type","")
        by_severity[s] = by_severity.get(s,0)+1
        by_vehicle[v] = by_vehicle.get(v,0)+1
        by_type[t] = by_type.get(t,0)+1
    high_risk = sorted([e for e in events if e.get("severity") in ("CRITICAL","HIGH")],
                       key=lambda x: {"CRITICAL":0,"HIGH":1}.get(x.get("severity",""),9))
    return {
        "total_events": len(events), "total_vehicles": len(vehicles),
        "by_severity": by_severity,
        "by_vehicle": {v.get("vin",""): {"count": by_vehicle.get(v.get("vin",""),0), "plate": v.get("plate",""), "driver": v.get("driver","")} for v in vehicles},
        "by_type": dict(sorted(by_type.items(), key=lambda x:-x[1])[:10]),
        "high_risk": high_risk[:10],
        "vehicles": vehicles,
    }


@app.get("/api/suggested-questions")
def api_suggested_questions():
    """根据已连接的数据源和语义层动态生成推荐问题"""
    _load_semantic_custom()
    from agentic_core.semantic_layer import METRICS, DIMENSIONS, QUERY_TEMPLATES
    
    has_data = len(METRICS) > 0
    
    if not has_data:
        return {"questions": [
            "平台支持哪些数据源？如何连接？",
            "如何添加 S3 数据源？",
            "语义层是什么？怎么配置？",
        ], "has_data": False}
    
    questions = []
    # Generate from query templates (these are known-good queries)
    for name, tmpl in list(QUERY_TEMPLATES.items())[:3]:
        questions.append(tmpl.get("description", name))
    
    # Generate from metrics
    metric_names = list(METRICS.keys())
    if len(metric_names) >= 2:
        questions.append(f"{metric_names[0]}和{metric_names[1]}的对比分析")
    if any("产" in m for m in metric_names):
        questions.append("各产线的生产效率和良品率对比")
    if any("里程" in m for m in metric_names):
        questions.append("各车型平均行驶里程TOP10")
    if any("车" in m for m in metric_names):
        questions.append("车辆总数及各车型分布")
    
    # Deduplicate and limit
    seen = set()
    unique = []
    for q in questions:
        if q not in seen:
            seen.add(q)
            unique.append(q)
    
    return {"questions": unique[:8], "has_data": True, "metrics": len(METRICS), "dimensions": len(DIMENSIONS)}

@app.get("/api/chatbi/overview")
def api_chatbi_overview():
    """ChatBI data lake overview — only shows datasets from connected datasources."""
    from agentic_core.tools import CHATBI_DATASETS
    _reload_chatbi(force=True)  # Force reload before overview
    active_ds = _get_active_chatbi_datasets()
    print(f"[Overview] active_ds={active_ds}, CHATBI_DATASETS={list(CHATBI_DATASETS.keys())}", flush=True)
    result = []
    for name, info in CHATBI_DATASETS.items():
        if active_ds and name not in active_ds:
            continue
        try:
            data = _load_chatbi(name)
            # Get field names and sample
            fields = list(data[0].keys()) if data else []
            result.append({
                "name": name,
                "description": info["desc"],
                "record_count": len(data),
                "fields": fields,
                "field_count": len(fields),
            })
        except Exception as e:
            result.append({"name": name, "description": info.get("description", info.get("desc", "")), "record_count": 0, "error": str(e)})
    return {"datasets": result, "total_datasets": len(result), "total_records": sum(d.get("record_count",0) for d in result)}

@app.get("/api/chatbi/detail/{dataset}")
def api_chatbi_detail(dataset: str, limit: int = 20):
    """Get sample data for a ChatBI dataset."""
    from agentic_core.tools import CHATBI_DATASETS
    if dataset not in CHATBI_DATASETS:
        return {"error": f"Dataset '{dataset}' not found"}
    data = _load_chatbi(dataset)
    fields = []
    if data:
        fields = [{"name": k, "type": type(v).__name__} for k, v in data[0].items()]
    return {"dataset": dataset, "description": CHATBI_DATASETS[dataset]["desc"],
            "total": len(data), "fields": fields, "rows": data[:limit]}

@app.get("/api/sql/tables")
def api_sql_tables():
    """Get SQL database table info."""
    return {"tables": [], "note": "Use /api/data-catalog for schema info"}

@app.get("/api/sql/sample/{table}")
def api_sql_sample(table: str, limit: int = 20):
    """Get sample rows from SQL table."""
    from agentic_core.tools import _run_sql
    # Sanitize table name
    if table not in ("vehicle_sales", "customer_profiles"):
        return {"error": "Invalid table"}
    result = _run_sql(f"SELECT * FROM {_safe_id(table)} LIMIT {int(limit)}")  # nosec B608
    count_result = _run_sql(f"SELECT COUNT(*) as cnt FROM {_safe_id(table)}")  # nosec B608
    result["total"] = count_result["rows"][0]["cnt"] if count_result.get("rows") else 0
    result["table"] = table
    return result

# [REMOVED] map-events endpoint (dead feature)

# ─── Config APIs (persisted) ───

class ConfigUpdate(BaseModel):
    config: dict

@app.get("/api/config")
def api_get_config():
    from agentic_core import (AVAILABLE_MODELS, SUB_AGENT_MODELS,
        DEFAULT_SYSTEM_PROMPT,
        DEFAULT_DATA_ANALYST_PROMPT)
    from agentic_core.dynamic_context import build_data_analyst_prompt, build_supervisor_data_section
    return {
        "current": _configs.get("global", {}),
        "available_models": AVAILABLE_MODELS,
        "sub_agent_models": SUB_AGENT_MODELS,
        "default_prompts": {
            "supervisor": DEFAULT_SYSTEM_PROMPT.replace("{{DYNAMIC_DATA_SECTION}}", build_supervisor_data_section()),
                    "data_analyst": build_data_analyst_prompt(),
        },
        "tools": [
            {"name": k, "label": v, "source": "DynamoDB" if "event" in k or "fleet" in k or "driver" in k or "risk" in k else ("Athena" if "telemetry" in k or "timeline" in k else ("Sub-Agent" if "deep_" in k else "S3"))}
            for k, v in TOOL_LABELS.items()
        ],
    }

@app.post("/api/config")
def api_update_config(req: ConfigUpdate):
    _configs["global"] = req.config
    _save_config("global_config", _configs)  # Persist
    _agents.clear()
    return {"ok": True}

# ─── Cost APIs (persisted) ───

@app.get("/api/cost")
def api_cost():
    provider = os.environ.get("AGENTIC_AUTO_MODEL_PROVIDER", "bedrock")
    currency = "CNY" if provider == "siliconflow" else "USD"
    symbol = "¥" if currency == "CNY" else "$"
    return {"queries": _cost_data, "total_cost": sum(q.get("cost",0) for q in _cost_data),
            "total_tokens": sum(q.get("input_tokens",0)+q.get("output_tokens",0) for q in _cost_data),
            "currency": currency, "symbol": symbol}

@app.get("/api/cost/{date}")
def api_cost_by_date(date: str):
    """Get cost records for a specific date (YYYY-MM-DD)."""
    records = _load_cost_records(date)
    formatted = [{"time": r.get("time",""), "duration": r.get("duration",0), "steps": r.get("steps",0),
                  "input_tokens": r.get("input_tokens",0), "output_tokens": r.get("output_tokens",0),
                  "cost": r.get("cost",0), "model": r.get("model","")} for r in records]
    return {"date": date, "queries": formatted,
            "total_cost": sum(q.get("cost",0) for q in formatted),
            "total_tokens": sum(q.get("input_tokens",0)+q.get("output_tokens",0) for q in formatted)}

@app.get("/api/mcp-status")
def api_mcp_status():
    import urllib.request
    try:
        urllib.request.urlopen("http://localhost:8520/sse", timeout=2)  # nosec B310
        return {"status": "running", "url": "http://localhost:8520/sse", "tools": 11}
    except:
        return {"status": "stopped", "url": None, "tools": 0}

# ─── Chat APIs (persisted) ───

@app.get("/api/chat/sessions")
def api_chat_sessions(request: Request):
    """List all persisted chat sessions for current user."""
    user = get_current_user(request)
    user_id = user.get("email") or user.get("user_id", "")
    return {"sessions": _load_chat_sessions(user_id=user_id, role=user.get("role", "viewer"))}

@app.get("/api/chat/history/{session_id}")
def api_chat_history(session_id: str):
    """Load chat history for a session."""
    messages = _load_chat_history(session_id)
    return {"session_id": session_id, "messages": messages}

@app.delete("/api/chat/history/{session_id}")
def api_delete_chat_history(session_id: str):
    """Delete all messages for a session."""
    try:
        table = _ddb.Table(CHAT_TABLE)
        resp = table.query(KeyConditionExpression=Key("session_id").eq(session_id), ProjectionExpression="session_id, #ts", ExpressionAttributeNames={"#ts": "timestamp"})
        with table.batch_writer() as batch:
            for item in resp.get("Items", []):
                batch.delete_item(Key={"session_id": item["session_id"], "timestamp": item["timestamp"]})
        _agents.pop(session_id, None)
        return {"ok": True, "deleted": len(resp.get("Items", []))}
    except Exception as e:
        return {"ok": False, "error": str(e)}

class ChatRequest(BaseModel):
    message: str
    session_id: str = ""
    config: Optional[dict] = None
    scenario_id: Optional[str] = None

@app.post("/api/chat")
async def api_chat(req: ChatRequest, request: Request):
    session_id = req.session_id or f"s-{uuid.uuid4().hex[:8]}"
    trace_id = f"tr-{uuid.uuid4().hex[:12]}"
    event_queue = queue.Queue()
    agent_state = {"done": False, "error": None, "steps": 0}
    trace_events = []  # Collect all trace spans for persistence

    user = get_current_user(request)
    user_id = user.get("email") or user.get("user_id", "")
    # Persist user message
    _save_chat_message(session_id, "user", req.message, user_id=user_id)

    def run():
        try:
            from strands.hooks import BeforeToolCallEvent, AfterToolCallEvent
            from agentic_core import set_trace_callback, MODEL_PRICING
            step_count = [0]
            sub_tool_start_times = {}
            tool_times = {}

            # Wire sub-agent notifications into SSE
            import agentic_core.agents as _ag_mod
            def _on_sub_notify(agent_name, status, data=None):
                try:
                    data = data or {}
                    if status == "start":
                        event_queue.put(("sub", json.dumps({"agent": agent_name, "type": "start", "data": data.get("question","") if isinstance(data, dict) else str(data)})))
                    elif status == "end":
                        event_queue.put(("sub", json.dumps({"agent": agent_name, "type": "end", "data": str(data) if data else ""})))
                    elif status in ("tool_start", "tool_before"):
                        tool_name = data.get("tool","") if isinstance(data, dict) else ""
                        label = data if isinstance(data, str) else TOOL_LABELS.get(tool_name, tool_name)
                        step_count[0] += 1
                        sub_tool_start_times[tool_name or label] = time.time()
                        event_queue.put(("sub", json.dumps({"agent": agent_name, "type": "tool_before", "data": label})))
                    elif status in ("tool_end", "tool_after"):
                        label = data if isinstance(data, str) else ""
                        if not label and isinstance(data, dict):
                            tool_name = data.get("tool","")
                            label = TOOL_LABELS.get(tool_name, tool_name)
                            ms = data.get("ms", 0)
                            if ms:
                                label = f"{label} ({ms}ms)"
                        event_queue.put(("sub", json.dumps({"agent": agent_name, "type": "tool_after", "data": label})))
                    elif status == "cost":
                        # Capture sub-agent cost in Supervisor mode too
                        event_queue.put(("sub", json.dumps({"agent": agent_name, "type": "cost", "data": data})))
                    elif status == "artifact":
                        event_queue.put(("artifact", json.dumps(data, ensure_ascii=False, default=str)))
                except: pass
            _ag_mod._sub_agent_callback = _on_sub_notify

            def on_before(event: BeforeToolCallEvent):
                try:
                    tu = event.tool_use if isinstance(event.tool_use, dict) else {"name": getattr(event.tool_use,"name",""), "input": getattr(event.tool_use,"input",{})}
                    name = tu.get("name","")
                    inp = tu.get("input",{}) if isinstance(tu.get("input"), dict) else {}
                    step_count[0] += 1
                    label = TOOL_LABELS.get(name, name)
                    # Add context for chatbi/sql tools
                    detail = ""
                    if False:  # cleaned
                        detail = f" ({inp['dataset']})"
                    elif False:  # cleaned
                        detail = f" ({inp['datasets']})"
                    elif name == "nl2sql_query" and inp.get("sql"):
                        detail = f": {inp['sql'][:60]}..."
                    elif name == "deep_data_analysis":
                        detail = f": {inp.get('question','')[:40]}"
                    tool_times[name+str(step_count[0])] = time.time()
                    event_queue.put(("tool", json.dumps({"step":step_count[0],"tool":label+detail,"name":name,"status":"start"})))
                    trace_events.append({"ts": time.time(), "agent": "Supervisor", "type": "tool_start", "tool": name, "step": step_count[0]})
                except: pass

            def on_after(event: AfterToolCallEvent):
                try:
                    name = event.tool_use.get("name","") if isinstance(event.tool_use, dict) else getattr(event.tool_use,"name","")
                    label = TOOL_LABELS.get(name, name)
                    ms = int((time.time()-tool_times.get(name+str(step_count[0]),time.time()))*1000)
                    event_queue.put(("tool", json.dumps({"step":step_count[0],"tool":label,"name":name,"status":"done","ms":ms})))
                    trace_events.append({"ts": time.time(), "agent": "Supervisor", "type": "tool_end", "tool": name, "step": step_count[0], "latency_ms": ms})
                except: pass

            def on_text(**kwargs):
                data = kwargs.get("data","")
                if data: event_queue.put(("text", json.dumps(data)))

            def sub_trace(agent_name, event_type, data):
                # Don't emit raw Orchestrator events — Direct mode has no Orchestrator
                if agent_name == "Orchestrator" and event_type in ("start", "end", "mode"):
                    # Still log for trace
                    trace_events.append({"ts": time.time(), "agent": agent_name, "type": event_type, "data": str(data)[:200]})
                    return
                # Artifact events need their own SSE event type (not wrapped in "sub")
                if event_type == "artifact":
                    event_queue.put(("artifact", json.dumps(data, ensure_ascii=False, default=str)))
                    trace_events.append({"ts": time.time(), "agent": agent_name, "type": "artifact"})
                    # Capture SQL for VQR feedback
                    if isinstance(data, dict) and data.get("type") == "query_result" and data.get("sql") and not data.get("error"):
                        agent_state["last_sql"] = data["sql"]
                        # Detect engine from SQL pattern: Athena uses database.table format
                        _sql_lower = data["sql"].lower()
                        _athena_ds = next((ds for ds in _custom_data_sources if ds.get("type") == "athena"), None)
                        _athena_db = _athena_ds.get("database", "").lower() if _athena_ds else ""
                        agent_state["last_sql_engine"] = "athena" if (_athena_db and f"{_athena_db}." in _sql_lower) else "postgresql"
                    return
                event_queue.put(("sub", json.dumps({"agent":agent_name,"type":event_type,"data":data})))
                trace_events.append({"ts": time.time(), "agent": agent_name, "type": event_type, "data": str(data)[:200]})
                # Text streaming from sub-agent: also emit as SSE text event
                # (Direct mode: no Supervisor to relay text, DataAnalyst text goes here)
                if event_type == "text" and isinstance(data, dict) and data.get("text"):
                    event_queue.put(("text", json.dumps(data["text"])))
                # Track tool calls as steps (Direct mode: sub-agent tools are the steps)
                if event_type == "tool_before":
                    step_count[0] += 1
                if event_type == "tool_after":
                    pass  # step already counted on tool_before
                # Insight result: inject directly as text into SSE stream
                if event_type == "insight_result" and data:
                    # Store insight for later merge with result_text (avoid separate text event)
                    agent_state["_insight_block"] = data
                # Capture sub-agent cost and update agent_state for meta
                if event_type == "cost":
                    try:
                        cost_info = json.loads(data)
                        sub_model = cost_info.get("model", "unknown")
                        sub_in = cost_info.get("input_tokens", 0)
                        sub_out = cost_info.get("output_tokens", 0)
                        pricing = MODEL_PRICING.get(sub_model, {"input": 0.001, "output": 0.005})
                        sub_cost = round((sub_in/1000*pricing["input"]) + (sub_out/1000*pricing["output"]), 6)
                        # Update agent_state so meta includes token/cost data
                        agent_state["input_tokens"] = agent_state.get("input_tokens", 0) + sub_in
                        agent_state["output_tokens"] = agent_state.get("output_tokens", 0) + sub_out
                        agent_state["cost"] = round(agent_state.get("cost", 0) + sub_cost, 6)
                        sub_record = {"time": datetime.now(timezone.utc).strftime("%H:%M:%S"),
                            "duration": 0, "steps": 0,
                            "input_tokens": sub_in, "output_tokens": sub_out,
                            "cost": sub_cost, "model": sub_model, "agent": agent_name,
                            "trace_id": trace_id, "latency_ms": 0}
                        _save_cost_record(sub_record)
                    except Exception as _e:
                        print(f"[WARN] sub-agent cost record: {_e}")

            set_trace_callback(sub_trace)
            # 重置 deep_* 调用限制器 (每个请求重新计数)
            from agentic_core.agents import _reset_deep_limit
            _reset_deep_limit()

            # ═══ Phase 2: Orchestrator Direct Mode (Fast-Path) ═══
            # V2 Scene + direct mode → 绕过 Supervisor，直接调 DataAnalyst
            _use_direct = False
            _use_parallel = False
            _use_pipeline = False
            _parallel_agent_defs = []
            _pipeline_agent_defs = []
            print(f"[Orchestrator] scenario_id={req.scenario_id}")
            
            # ═══ Auto-routing: if no scenario selected, detect from question keywords ═══
            if not req.scenario_id:
                _auto_scenario = _auto_route_scenario(req.message)
                if _auto_scenario:
                    req.scenario_id = _auto_scenario
                    print(f"[Orchestrator] Auto-routed to scenario: {_auto_scenario}")
                    # Notify frontend about auto-routing
                    from agentic_core.agent_registry import get_scene as _get_auto_scene
                    _auto_scene_def = _get_auto_scene(_auto_scenario)
                    _auto_name = _auto_scene_def.get("name", _auto_scenario) if _auto_scene_def else _auto_scenario
                    event_queue.put(("sub", json.dumps({"agent": "AutoRoute", "type": "info", "data": f"自动识别场景: {_auto_name}"})))
            
            if req.scenario_id:
                try:
                    from agentic_core.agent_registry import get_scene, get_agent_def as _get_v2_agent
                    _v2_scene = get_scene(req.scenario_id)
                    if _v2_scene:
                        _orch = _v2_scene.get("orchestration", {})
                        _v2_mode = _orch.get("mode", "")
                        _v2_agent_refs = _orch.get("agents", [])
                        if _v2_mode == "direct" and _v2_agent_refs:
                            _v2_aid = _v2_agent_refs[0]["id"] if isinstance(_v2_agent_refs[0], dict) else _v2_agent_refs[0]
                            _v2_agent_def = _get_v2_agent(_v2_aid)
                            if _v2_agent_def:
                                _use_direct = True
                                print(f"[Orchestrator] DIRECT mode: {_v2_aid} ({_v2_agent_def.get('name','')})")
                        elif _v2_mode == "parallel" and len(_v2_agent_refs) >= 2:
                            for ref in _v2_agent_refs:
                                aid = ref["id"] if isinstance(ref, dict) else ref
                                adef = _get_v2_agent(aid)
                                if adef:
                                    _parallel_agent_defs.append(adef)
                            if len(_parallel_agent_defs) >= 2:
                                _use_parallel = True
                                print(f"[Orchestrator] PARALLEL mode: {[a.get('name','?') for a in _parallel_agent_defs]}")
                        elif _v2_mode == "pipeline" and len(_v2_agent_refs) >= 2:
                            _use_pipeline = True
                            _pipeline_agent_defs = []
                            for ref in _v2_agent_refs:
                                aid = ref["id"] if isinstance(ref, dict) else ref
                                adef = _get_v2_agent(aid)
                                if adef:
                                    _pipeline_agent_defs.append(adef)
                            print(f"[Orchestrator] PIPELINE mode: {[a.get('name','?') for a in _pipeline_agent_defs]}")
                except Exception as _e:
                    print(f"[Orchestrator] V2 check failed: {_e}, falling back to Supervisor")

            if _use_direct:
                # Fast-Path: 直接配置 _sub_agent_config 并调 deep_data_analysis
                from agentic_core.agents import _sub_agent_config, deep_data_analysis
                import agentic_core.agents as _agents_mod
                
                # 从 V2 Agent Definition 构建 sub_agent config (thread-local)
                # Direct mode 默认用 Haiku (快5-10x)，除非 agent_def 显式指定了模型
                _direct_model = _v2_agent_def.get("model_id") or _configs.get("global", {}).get("sub_agent_model", "us.anthropic.claude-haiku-4-5-20251001-v1:0")
                _agents_mod._set_sub_agent_config({
                    "model_id": _direct_model,
                    "max_tokens": _v2_agent_def.get("max_tokens", 4096),
                    "scenario_da_tools": _v2_agent_def.get("tools", []),
                    "scenario_prompt_context": _v2_agent_def.get("system_prompt", ""),
                    "_scenario_cfg": {"datasources": _v2_agent_def.get("datasources", [])},
                    "skills": _v2_agent_def.get("skills", []),
                    "agent_display_name": _v2_agent_def.get("name", "DataAnalyst"),
                })
                
                # Emit mode as a control event (not sub-agent) so frontend knows it's Direct mode
                # without creating an Orchestrator card in the workbench
                event_queue.put(("mode", json.dumps({"mode": "direct"})))
                
                # Build context from chat history for multi-turn conversations
                _direct_question = req.message
                try:
                    from boto3.dynamodb.conditions import Key as DKey
                    hist_resp = _ddb.Table(CHAT_TABLE).query(
                        KeyConditionExpression=DKey("session_id").eq(session_id),
                        ScanIndexForward=True, Limit=20
                    )
                    hist_items = hist_resp.get("Items", [])
                    if hist_items:
                        ctx_parts = []
                        for hm in hist_items[-8:]:
                            role = hm.get("role", "")
                            content = str(hm.get("content", ""))[:500]
                            if role == "user":
                                ctx_parts.append(f"用户: {content}")
                            elif role == "assistant" and content:
                                ctx_parts.append(f"助手: {content[:300]}")
                        if ctx_parts:
                            _direct_question = "对话历史:\n" + "\n".join(ctx_parts) + "\n\n当前问题: " + req.message
                except Exception as _he:
                    print(f"[Orchestrator] History load warning: {_he}")
                
                try:
                    # ── VQR Pre-check: if verified query matches, execute directly ──
                    _vqr_handled = False
                    try:
                        from agentic_core.vqr import match_verified_query, record_hit
                        _ds_filter = _v2_agent_def.get("datasources", [])
                        _vqr = match_verified_query(req.message, datasource_filter=_ds_filter)
                        if _vqr and _vqr.get("sql"):
                            record_hit(_vqr["id"])
                            print(f"[Orchestrator] VQR HIT in Direct mode: {_vqr['match_type']} (score={_vqr['match_score']}) → {_vqr.get('question','')[:50]}")
                            # Emit semantic artifact with VQR badge
                            event_queue.put(("artifact", json.dumps({
                                "type": "semantic", "question": req.message,
                                "vqr_hit": True, "vqr_match_type": _vqr["match_type"],
                                "matched_metrics": [], "matched_dimensions": [], "matched_templates": [],
                            }, ensure_ascii=False)))
                            # Execute verified SQL
                            _vqr_engine = _vqr.get("engine", "athena")
                            from agentic_core.tools import _run_pg, _run_athena
                            import time as _t
                            _t0 = _t.time()
                            if _vqr_engine == "postgresql":
                                _vqr_result = _run_pg(_vqr["sql"])
                            else:
                                _vqr_result = _run_athena(_vqr["sql"])
                            _vqr_ms = int((_t.time() - _t0) * 1000)
                            event_queue.put(("artifact", json.dumps({
                                "type": "query_result", "sql": _vqr["sql"],
                                "columns": _vqr_result.get("columns", []),
                                "rows": _vqr_result.get("rows", [])[:20],
                                "total_rows": _vqr_result.get("count", 0),
                                "elapsed_ms": _vqr_ms, "error": _vqr_result.get("error"),
                                "vqr_source": True,
                            }, ensure_ascii=False, default=str)))
                            # Store SQL for feedback
                            agent_state["last_sql"] = _vqr["sql"]
                            agent_state["last_sql_engine"] = _vqr_engine
                            # VQR 命中 + 数据成功返回 → 直接生成回答，不调 Bedrock
                            _vqr_rows = _vqr_result.get("rows", [])
                            _vqr_cols = _vqr_result.get("columns", [])
                            if _vqr_rows and not _vqr_result.get("error"):
                                # 模板生成 Markdown 表格回答
                                _vqr_question = _vqr.get("question", req.message)
                                _vqr_md = f"## {_vqr_question}\n\n"
                                if _vqr_cols:
                                    _vqr_md += "| " + " | ".join(str(c) for c in _vqr_cols) + " |\n"
                                    _vqr_md += "| " + " | ".join("---" for _ in _vqr_cols) + " |\n"
                                    for _r in _vqr_rows[:20]:
                                        _vqr_md += "| " + " | ".join(str(_r.get(c, "")) for c in _vqr_cols) + " |\n"
                                _vqr_md += f"\n共 {len(_vqr_rows)} 条记录，查询耗时 {_vqr_ms}ms（VQR 缓存命中，零 LLM 调用）。"
                                _vqr_handled = True
                                result_text = _vqr_md
                                step_count[0] = 0
                                print(f"[Orchestrator] VQR fast-path: {len(_vqr_rows)} rows, {_vqr_ms}ms, zero Bedrock calls")
                            else:
                                # VQR SQL 执行有错或无数据，回退到 Agent
                                _vqr_data_str = f"已通过VQR验证查询获取数据（{len(_vqr_rows)}行）。原始问题: {req.message}\n查询结果:\n"
                                for _r in _vqr_rows[:15]:
                                    _vqr_data_str += " | ".join(str(_r.get(c,"")) for c in _vqr_cols) + "\n"
                                _direct_question = f"以下是已执行的SQL查询结果，请直接基于数据回答用户问题，不要再调用任何查询工具。\n\n{_vqr_data_str}\n\n用户问题: {req.message}"
                    except Exception as _vqr_e:
                        print(f"[Orchestrator] VQR pre-check error: {_vqr_e}")

                    if not _vqr_handled:
                        result_text = deep_data_analysis(_direct_question)
                    print(f"[Orchestrator] Direct result: type={type(result_text).__name__}, len={len(str(result_text)) if result_text else 0}, step_count={step_count[0]}, preview={str(result_text)[:100] if result_text else 'NONE'}")
                    agent_state["steps"] = step_count[0]
                    agent_state["result_text"] = result_text
                    
                    # Push result text to SSE stream (Direct mode: no Supervisor to relay)
                    # Merge insight block if present
                    _insight = agent_state.get("_insight_block", "")
                    if _insight and result_text:
                        result_text = result_text + "\n" + _insight
                    if result_text and result_text != "None" and len(result_text.strip()) > 5:
                        event_queue.put(("text", json.dumps(result_text)))
                    elif not result_text or result_text == "None":
                        event_queue.put(("text", json.dumps("抱歉，Agent 未返回有效结果。请尝试重新提问或换个问法。")))
                        print(f"[Orchestrator] WARN: empty result from deep_data_analysis")
                    
                    # Cost: only DataAnalyst cost (no Supervisor)
                    agent_state["model"] = (_v2_agent_def.get("model_id", "").split(".")[-1] or "unknown")
                except Exception as e:
                    err_msg = str(e)
                    print(f"[Orchestrator] Direct mode error: {err_msg}")
                    if "Deserialization" in err_msg:
                        _reset_deep_limit()
                        result_text = deep_data_analysis(req.message)
                        agent_state["result_text"] = result_text
                        if result_text:
                            event_queue.put(("text", json.dumps(result_text)))
                    else:
                        raise
            elif _use_parallel:
                # Parallel mode: run multiple Agents concurrently
                from agentic_core.agents import deep_data_analysis, _sub_agent_config
                import agentic_core.agents as _agents_mod
                from concurrent.futures import ThreadPoolExecutor, as_completed as _as_completed
                
                event_queue.put(("sub", json.dumps({"agent": "Orchestrator", "type": "mode", "data": "parallel"})))
                
                def _run_one_agent(agent_def):
                    """Run a single agent with thread-local config (now safe for parallel)."""
                    _agents_mod._set_sub_agent_config({
                        "model_id": agent_def.get("model_id") or _configs.get("global", {}).get("sub_agent_model", "us.anthropic.claude-sonnet-4-6"),
                        "max_tokens": agent_def.get("max_tokens", 4096),
                        "scenario_da_tools": agent_def.get("tools", []),
                        "scenario_prompt_context": agent_def.get("system_prompt", ""),
                        "_scenario_cfg": {"datasources": agent_def.get("datasources", [])},
                        "skills": agent_def.get("skills", []),
                        "agent_display_name": agent_def.get("name", "DataAnalyst"),
                    })
                    return deep_data_analysis(req.message)
                
                _parallel_results = {}
                _parallel_errors = {}
                _t_par = time.time()
                
                # Emit start events for each agent
                for adef in _parallel_agent_defs:
                    aname = adef.get("name", adef.get("id", "?"))
                    event_queue.put(("sub", json.dumps({"agent": aname, "type": "start", "data": f"{aname} 开始分析..."})))
                
                # True parallel execution — _sub_agent_config is now thread-local
                with ThreadPoolExecutor(max_workers=len(_parallel_agent_defs)) as _executor:
                    _futures = {}
                    for adef in _parallel_agent_defs:
                        aname = adef.get("name", adef.get("id", "?"))
                        _futures[_executor.submit(_run_one_agent, adef)] = aname
                    
                    for fut in _as_completed(_futures):
                        aname = _futures[fut]
                        try:
                            r = fut.result()
                            _parallel_results[aname] = r
                            elapsed_ms = int((time.time() - _t_par) * 1000)
                            event_queue.put(("sub", json.dumps({"agent": aname, "type": "end", "data": f"{aname} 完成 ({elapsed_ms}ms)"})))
                        except Exception as e:
                            _parallel_errors[aname] = str(e)
                            event_queue.put(("sub", json.dumps({"agent": aname, "type": "error", "data": str(e)[:200]})))
                
                total_par_ms = int((time.time() - _t_par) * 1000)
                event_queue.put(("sub", json.dumps({"agent": "Orchestrator", "type": "end", "data": f"并行完成 ({total_par_ms}ms), {len(_parallel_results)} 成功, {len(_parallel_errors)} 失败"})))
                
                # Merge results: deterministic synthesis with cross-domain insight
                agent_names = list(_parallel_results.keys())
                raw_parts = []
                for aname, r in _parallel_results.items():
                    raw_parts.append(r.strip())
                
                # Build a unified cross-domain report
                header = f"## 跨域综合分析\n\n> **{' + '.join(agent_names)}** 分别查询了不同数据源，以下为综合对比：\n"
                sections = []
                for aname, r in _parallel_results.items():
                    sections.append(f"### {aname}\n\n{r.strip()}")
                
                result_text = header + "\n\n" + "\n\n---\n\n".join(sections)
                
                # ── Cross-domain intelligent merge ──
                # Extract key numbers from both results for automated insight
                cross_insight_lines = []
                try:
                    import re as _re
                    _all_text = " ".join(raw_parts)
                    
                    # Try to extract model name from the question
                    _q = req.message
                    
                    # Extract numeric patterns for comparison
                    _nums = {}
                    for aname, r in _parallel_results.items():
                        _nums[aname] = {}
                        # Look for 保有量/total_vehicles patterns
                        m = _re.search(r'保有量[^\d]*?([\d,]+(?:\.\d+)?)\s*(?:辆|万)', r)
                        if m:
                            val_str = m.group(1).replace(",", "")
                            _nums[aname]["保有量"] = float(val_str)
                        # Look for 合计 patterns
                        m = _re.search(r'合计[^\d]*?([\d,]+)', r)
                        if m:
                            val_str = m.group(1).replace(",", "")
                            _nums[aname]["合计"] = float(val_str)
                        # Look for 良品率/pass_rate
                        m = _re.search(r'良品率[^\d]*?([\d.]+)\s*%', r)
                        if m:
                            _nums[aname]["良品率"] = float(m.group(1))
                        # Look for 不合格率/fail_rate
                        m = _re.search(r'不合格率[^\d]*?([\d.]+)\s*%', r)
                        if m:
                            _nums[aname]["不合格率"] = float(m.group(1))
                        # Look for 完工/completed
                        m = _re.search(r'完工[^\d]*?([\d,]+)', r) or _re.search(r'completed[^\d]*?([\d,]+)', r, _re.IGNORECASE)
                        if m:
                            val_str = m.group(1).replace(",", "")
                            _nums[aname]["完工"] = float(val_str)
                        # Look for 产量
                        m = _re.search(r'(?:计划|目标)[^\d]*?([\d,]+)\s*辆', r)
                        if m:
                            val_str = m.group(1).replace(",", "")
                            _nums[aname]["计划产量"] = float(val_str)
                    
                    # Generate cross-domain insights
                    all_nums = {}
                    for an in _nums.values():
                        all_nums.update(an)
                    
                    if all_nums:
                        cross_insight_lines.append("### 跨域洞察\n")
                        
                        # Insight 1: 保有量 vs 产量 comparison
                        bhy = all_nums.get("保有量") or all_nums.get("合计")
                        wg = all_nums.get("完工")
                        jh = all_nums.get("计划产量")
                        if bhy and wg:
                            if bhy > wg * 10:
                                cross_insight_lines.append(f"- **市场保有量 ({int(bhy):,} 辆) 远超当前生产完工量 ({int(wg):,} 辆)**，说明绝大部分保有量来自历史累计产能，当前批次仅占市场存量的 {wg/bhy*100:.1f}%")
                            elif bhy > wg:
                                cross_insight_lines.append(f"- 市场保有量 ({int(bhy):,} 辆) 大于当前完工量 ({int(wg):,} 辆)，历史产能是保有量主要来源")
                            else:
                                cross_insight_lines.append(f"- 完工量 ({int(wg):,} 辆) 已接近或超过市场保有量 ({int(bhy):,} 辆)，新产能正在快速投放市场")
                        
                        # Insight 2: Quality assessment
                        lpl = all_nums.get("良品率")
                        if lpl:
                            if lpl >= 97:
                                cross_insight_lines.append(f"- 良品率 {lpl}%，生产质量优秀，市场端的质量投诉风险较低")
                            elif lpl >= 95:
                                cross_insight_lines.append(f"- 良品率 {lpl}%，质量水平中上，仍有 {100-lpl:.1f}% 的质量损耗可优化")
                            else:
                                cross_insight_lines.append(f"- ⚠️ 良品率仅 {lpl}%，建议重点关注生产环节质控，可能影响市场口碑")
                        
                        # Insight 3: Production plan completion
                        if wg and jh:
                            rate = wg / jh * 100
                            if rate >= 95:
                                cross_insight_lines.append(f"- 订单完成率 {rate:.1f}%，产能交付正常")
                            else:
                                cross_insight_lines.append(f"- 订单完成率 {rate:.1f}%（计划 {int(jh):,} / 完工 {int(wg):,}），仍有在制订单")
                    
                except Exception as _merge_err:
                    print(f"[Parallel] Cross-domain insight extraction error: {_merge_err}")
                
                if cross_insight_lines:
                    result_text += "\n\n---\n\n" + "\n".join(cross_insight_lines)
                else:
                    result_text += "\n\n---\n\n### 跨域洞察\n\n以上数据分别来自 **" + "** 和 **".join(agent_names) + "**，可以通过车型名称(model_name)进行交叉关联分析。"
                
                if _parallel_errors:
                    result_text += "\n\n---\n\n### ⚠️ 错误\n" + "\n".join([f"- {k}: {v}" for k, v in _parallel_errors.items()])
                
                agent_state["result_text"] = result_text
                if result_text:
                    event_queue.put(("text", json.dumps(result_text)))
                agent_state["model"] = "parallel"
            elif _use_pipeline:
                # Pipeline mode: run agents sequentially, each feeding into the next
                from agentic_core.agents import deep_data_analysis, _sub_agent_config
                import agentic_core.agents as _agents_mod
                
                event_queue.put(("sub", json.dumps({"agent": "Orchestrator", "type": "mode", "data": "pipeline"})))
                
                _pipeline_input = req.message
                _pipeline_results = []
                
                for _step_i, _adef in enumerate(_pipeline_agent_defs):
                    aname = _adef.get("name", _adef.get("id", f"step_{_step_i}"))
                    event_queue.put(("sub", json.dumps({"agent": aname, "type": "start", "data": f"Pipeline step {_step_i+1}/{len(_pipeline_agent_defs)}"})))
                    
                    _agents_mod._set_sub_agent_config({
                        "model_id": _adef.get("model_id") or _configs.get("global", {}).get("sub_agent_model", "us.anthropic.claude-sonnet-4-6"),
                        "max_tokens": _adef.get("max_tokens", 4096),
                        "scenario_da_tools": _adef.get("tools", []),
                        "scenario_prompt_context": _adef.get("system_prompt", ""),
                        "_scenario_cfg": {"datasources": _adef.get("datasources", [])},
                        "skills": _adef.get("skills", []),
                    })
                    
                    try:
                        _step_result = deep_data_analysis(_pipeline_input)
                        _pipeline_results.append(f"## {aname}\n\n{_step_result}")
                        # Feed output as input to next step
                        _pipeline_input = f"基于以下上一步分析结果，继续:\n\n{_step_result}\n\n原始问题: {req.message}"
                        event_queue.put(("sub", json.dumps({"agent": aname, "type": "end", "data": f"{aname} 完成"})))
                    except Exception as e:
                        _pipeline_results.append(f"## {aname}\n\n[错误] {str(e)[:200]}")
                        event_queue.put(("sub", json.dumps({"agent": aname, "type": "error", "data": str(e)[:200]})))
                        break  # Pipeline stops on error
                
                result_text = "\n\n---\n\n".join(_pipeline_results)
                agent_state["result_text"] = result_text
                if result_text:
                    event_queue.put(("text", json.dumps(result_text)))
                agent_state["model"] = "pipeline"
            else:
                # Legacy path: Supervisor Agent
                event_queue.put(("sub", json.dumps({"agent": "Orchestrator", "type": "mode", "data": "supervisor"})))
                agent = get_agent(session_id, req.config, user_email=user_id, scenario_id=req.scenario_id)
                agent.callback_handler = on_text
                agent.add_hook(on_before, BeforeToolCallEvent)
                agent.add_hook(on_after, AfterToolCallEvent)
                try:
                    result = agent(req.message)
                except Exception as agent_err:
                    err_msg = str(agent_err)
                    if "Deserialization" in err_msg or "unable to process" in err_msg.lower():
                        _reset_deep_limit()
                        agent = get_agent(session_id, req.config, force_new=True, user_email=user_id)
                        agent.callback_handler = on_text
                        agent.add_hook(on_before, BeforeToolCallEvent)
                        agent.add_hook(on_after, AfterToolCallEvent)
                        result = agent(req.message)
                    else:
                        raise
                agent_state["steps"] = step_count[0]
                result_text = str(result)
                agent_state["result_text"] = result_text
                
                _fail_patterns = ["分析失败", "资源限制", "服务当前遭遇", "暂时无法完成", "临时异常"]
                if any(p in result_text for p in _fail_patterns):
                    _agents.pop(session_id, None)

                try:
                    m = result.metrics
                    if m and m.agent_invocations:
                        inv = m.agent_invocations[-1]
                        u = inv.usage
                        agent_state["input_tokens"] = u.get("inputTokens", 0)
                        agent_state["output_tokens"] = u.get("outputTokens", 0)
                        cfg = _configs.get("global", {})
                        model_id = cfg.get("supervisor_model", "us.anthropic.claude-sonnet-4-6")
                        pricing = MODEL_PRICING.get(model_id, {"input": 0.003, "output": 0.015})
                        cost = (u.get("inputTokens",0)/1000*pricing["input"]) + (u.get("outputTokens",0)/1000*pricing["output"])
                        agent_state["cost"] = round(cost, 6)
                        agent_state["model"] = model_id.split(".")[-1] if "." in model_id else model_id
                except Exception as _e:
                    print(f"[WARN] swallowed exception: {_e}")
        except Exception as e:
            agent_state["error"] = str(e)
        finally:
            agent_state["done"] = True

    async def stream():
        import asyncio
        t0 = time.time()
        last_send = time.time()
        thread = threading.Thread(target=run, daemon=True)
        thread.start()
        # Initial heartbeat to confirm connection
        yield ": connected\n\n"
        while not agent_state["done"]:
            try:
                etype, data = event_queue.get_nowait()
                yield f"event: {etype}\ndata: {data}\n\n"
                last_send = time.time()
            except queue.Empty:
                # Send heartbeat every 5s to keep CloudFront/proxy alive
                now = time.time()
                if now - last_send > 5:
                    yield f": heartbeat {int(now-t0)}s\n\n"
                    last_send = now
                await asyncio.sleep(0.3)  # Non-blocking wait, allows uvicorn to flush
        # Drain remaining events (with retry to avoid race condition)
        for _drain_attempt in range(3):
            try:
                while True:
                    etype, data = event_queue.get_nowait()
                    yield f"event: {etype}\ndata: {data}\n\n"
            except queue.Empty:
                pass
            if _drain_attempt < 2:
                await asyncio.sleep(0.1)
        elapsed = round(time.time()-t0, 1)
        in_tok = agent_state.get("input_tokens", 0)
        out_tok = agent_state.get("output_tokens", 0)
        cost = agent_state.get("cost", 0)
        model = agent_state.get("model", "sonnet-4-6")
        meta = {"elapsed":elapsed,"steps":agent_state["steps"],"error":agent_state["error"],"session_id":session_id,
                "input_tokens":in_tok,"output_tokens":out_tok,"cost":cost,"model":model,"trace_id":trace_id}
        if agent_state.get("last_sql"):
            meta["sql"] = agent_state["last_sql"]
            meta["sql_engine"] = agent_state.get("last_sql_engine", "athena")

        # Persist cost record
        latency_ms = int(elapsed * 1000)
        cost_record = {"time":datetime.now(timezone.utc).strftime("%H:%M:%S"),
            "duration":elapsed,"steps":agent_state["steps"],"input_tokens":in_tok,"output_tokens":out_tok,
            "cost":cost,"model":model,"agent":"Supervisor","trace_id":trace_id,"latency_ms":latency_ms,
            "session_id":session_id,"question":req.message[:100],
            "error":agent_state.get("error") or ""}
        _cost_data.append(cost_record)
        _save_cost_record(cost_record)

        # Persist trace
        try:
            _ddb.Table(COST_TABLE).put_item(Item={
                "date": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
                "id": f"trace_{trace_id}",
                "timestamp": f"trace_{trace_id}",
                "trace_id": trace_id,
                "session_id": session_id,
                "question": req.message[:200],
                "events": json.dumps(trace_events, ensure_ascii=False, default=str),
                "total_ms": latency_ms,
                "steps": agent_state["steps"],
                "error": agent_state.get("error") or "",
            })
        except Exception as _te:
            print(f"[WARN] trace persist: {_te}")

        # Persist assistant response
        result_text = agent_state.get("result_text", "")

        # Auto-inject chart/drill if missing
        if result_text and '```chart' not in result_text:
            try:
                from agentic_core.agents import _ensure_chart_drill
                enhanced = _ensure_chart_drill(result_text, req.message)
                if enhanced != result_text:
                    extra = enhanced[len(result_text):]
                    yield f"event: text\ndata: {json.dumps(extra)}\n\n"
                    result_text = enhanced
                    agent_state["result_text"] = result_text
            except Exception as _e:

                print(f"[WARN] swallowed exception: {_e}")

        if result_text:
            _save_chat_message(session_id, "assistant", result_text, meta, user_id=user_id)
            # Extract and save user memory
            if user_id:
                try:
                    existing = load_user_memory(user_id)
                    updated = extract_memory_from_conversation(req.message, result_text, existing)
                    if updated != existing:
                        save_user_memory(user_id, updated)
                except Exception as mem_err:
                    logger.warning(f"Memory extraction failed: {mem_err}")

        yield f"event: done\ndata: {json.dumps(meta)}\n\n"

    return StreamingResponse(stream(), media_type="text/event-stream",
                             headers={"Cache-Control":"no-cache","X-Accel-Buffering":"no"})

@app.delete("/api/session/{session_id}")
def delete_session(session_id: str):
    _agents.pop(session_id, None)
    return {"ok": True}

# ═══════ Scenarios API ═══════

@app.get("/api/scenarios")
def get_scenarios(request: Request):
    """Get all scenarios for the current user, with V2 RBAC filtering."""
    try:
        # Get user role from auth
        user_role = getattr(request.state, "role", "admin") if hasattr(request, "state") else "admin"
        
        r = _ddb.Table(CONFIG_TABLE).get_item(Key={"config_key": "scenarios"})
        data = r.get("Item", {}).get("data", "{}")
        if isinstance(data, str):
            import json as _j
            scenarios = _j.loads(data)
        else:
            scenarios = data
        
        # Check V2 scenes for RBAC
        v2_scenes = {}
        try:
            from agentic_core.agent_registry import list_scenes
            v2_scenes = list_scenes()
        except Exception:
            pass
        
        result = {}
        for k, v in scenarios.items():
            if not v.get("enabled", True):
                continue
            # RBAC: check V2 scene access roles
            v2 = v2_scenes.get(k)
            if v2:
                allowed_roles = v2.get("access", {}).get("roles", [])
                if allowed_roles and user_role not in allowed_roles:
                    continue
            result[k] = v
        
        # Merge V2-only scenes (created in Agent Studio but not in V1 scenarios)
        for k, v2 in v2_scenes.items():
            if k in result:
                continue
            if not v2.get("enabled", True):
                continue
            allowed_roles = v2.get("access", {}).get("roles", [])
            if allowed_roles and user_role not in allowed_roles:
                continue
            # Convert V2 scene to V1-compatible format for chat page
            orch = v2.get("orchestration", {})
            result[k] = {
                "name": v2.get("name", k),
                "desc": v2.get("description", ""),
                "enabled": True,
                "mode": orch.get("mode", "direct"),
                "agents": [a.get("agent_id", a) if isinstance(a, dict) else a for a in orch.get("agents", [])],
                "datasources": v2.get("datasource_ids", []),
                "suggested_questions": v2.get("suggestions", {}).get("manual", []),
                "tags": v2.get("tags", []),
                "color": v2.get("color", ""),
                "icon": v2.get("icon", ""),
                "sort_order": v2.get("sort_order", 99),
                "_v2": True,
            }
        return {"ok": True, "scenarios": result}
    except Exception as e:
        return {"ok": False, "error": str(e)[:200]}

@app.get("/api/scenarios/{scenario_id}")
def get_scenario(scenario_id: str):
    try:
        r = _ddb.Table(CONFIG_TABLE).get_item(Key={"config_key": "scenarios"})
        data = r.get("Item", {}).get("data", "{}")
        if isinstance(data, str):
            import json as _j
            scenarios = _j.loads(data)
        else:
            scenarios = data
        s = scenarios.get(scenario_id)
        if not s:
            return {"ok": False, "error": "场景不存在"}
        return {"ok": True, "scenario": s}
    except Exception as e:
        return {"ok": False, "error": str(e)[:200]}

@app.put("/api/scenarios/{scenario_id}")
def update_scenario(scenario_id: str, req: dict):
    """Create or update a scenario."""
    try:
        r = _ddb.Table(CONFIG_TABLE).get_item(Key={"config_key": "scenarios"})
        data = r.get("Item", {}).get("data", "{}")
        import json as _j
        scenarios = _j.loads(data) if isinstance(data, str) else data
        # Merge incoming fields
        existing = scenarios.get(scenario_id, {})
        for k in ["name", "desc", "icon", "icon_type", "color", "agents", "tools", "datasources", "suggested_questions", "roles", "enabled", "tags", "prompt_context", "sort_order"]:
            if k in req:
                existing[k] = req[k]
        if "enabled" not in existing:
            existing["enabled"] = True
        scenarios[scenario_id] = existing
        sj = _j.dumps(scenarios, ensure_ascii=False)
        _ddb.Table(CONFIG_TABLE).put_item(Item={"config_key": "scenarios", "data": sj, "value": sj})
        bump_data_version()
        return {"ok": True, "scenario": existing}
    except Exception as e:
        return {"ok": False, "error": str(e)[:200]}

@app.delete("/api/scenarios/{scenario_id}")
def delete_scenario(scenario_id: str):
    try:
        r = _ddb.Table(CONFIG_TABLE).get_item(Key={"config_key": "scenarios"})
        data = r.get("Item", {}).get("data", "{}")
        import json as _j
        scenarios = _j.loads(data) if isinstance(data, str) else data
        if scenario_id in scenarios:
            del scenarios[scenario_id]
            sj = _j.dumps(scenarios, ensure_ascii=False)
            _ddb.Table(CONFIG_TABLE).put_item(Item={"config_key": "scenarios", "data": sj, "value": sj})
            bump_data_version()
        return {"ok": True}
    except Exception as e:
        return {"ok": False, "error": str(e)[:200]}

# ═══════════════════ V2 Agent Registry API ═══════════════════
# 独立 Agent 实体 CRUD — 数据源/工具/prompt 绑定 Agent 而非 Scene

@app.get("/api/v2/agents")
def api_v2_list_agents():
    """List all V2 Agent definitions."""
    from agentic_core.agent_registry import list_agents as _list_v2
    agents = _list_v2()
    return {"ok": True, "agents": agents}

@app.get("/api/v2/agents/{agent_id}")
def api_v2_get_agent(agent_id: str):
    from agentic_core.agent_registry import get_agent_def
    a = get_agent_def(agent_id)
    if not a:
        raise HTTPException(status_code=404, detail="Agent not found")
    return {"ok": True, "agent": a}

@app.post("/api/v2/agents")
def api_v2_create_agent(req: dict):
    from agentic_core.agent_registry import save_agent
    agent = save_agent(req)
    _agents.clear()
    return {"ok": True, "agent": agent}

@app.put("/api/v2/agents/{agent_id}")
def api_v2_update_agent(agent_id: str, req: dict):
    from agentic_core.agent_registry import get_agent_def, save_agent
    existing = get_agent_def(agent_id)
    if not existing:
        raise HTTPException(status_code=404, detail="Agent not found")
    # Merge updates
    existing.update(req)
    existing["id"] = agent_id  # Prevent ID change
    agent = save_agent(existing)
    _agents.clear()
    return {"ok": True, "agent": agent}

@app.delete("/api/v2/agents/{agent_id}")
def api_v2_delete_agent(agent_id: str):
    from agentic_core.agent_registry import delete_agent
    delete_agent(agent_id)
    _agents.clear()
    return {"ok": True}

@app.post("/api/v2/agents/{agent_id}/clone")
def api_v2_clone_agent(agent_id: str, req: dict = None):
    from agentic_core.agent_registry import clone_agent
    req = req or {}
    cloned = clone_agent(agent_id, new_name=req.get("name", ""))
    return {"ok": True, "agent": cloned}

@app.get("/api/v2/agents/{agent_id}/versions")
def api_v2_agent_versions(agent_id: str):
    from agentic_core.agent_registry import get_version_history
    history = get_version_history(agent_id)
    return {"ok": True, "versions": history}

@app.post("/api/v2/agents/{agent_id}/rollback")
def api_v2_rollback_agent(agent_id: str, req: dict):
    from agentic_core.agent_registry import rollback_agent
    version = req.get("version")
    if not version:
        raise HTTPException(status_code=400, detail="version required")
    result = rollback_agent(agent_id, version)
    if not result:
        raise HTTPException(status_code=404, detail="Version not found")
    _agents.clear()
    return {"ok": True, "agent": result}

@app.get("/api/v2/templates")
def api_v2_list_templates():
    from agentic_core.agent_registry import list_templates
    return {"ok": True, "templates": list_templates()}

@app.post("/api/v2/templates/{template_id}/create")
def api_v2_create_from_template(template_id: str, req: dict = None):
    from agentic_core.agent_registry import create_from_template
    req = req or {}
    try:
        agent = create_from_template(template_id, overrides=req)
        return {"ok": True, "agent": agent}
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))


class ABTestRequest(BaseModel):
    message: str
    agent_a: str
    agent_b: str
    session_id: str = ""
    scenario_id: str = ""


@app.post("/api/v2/ab-test")
async def api_v2_ab_test(req: ABTestRequest, request: Request):
    """Run two agents on the same question and return both results for comparison."""
    from agentic_core.agents import deep_data_analysis, _sub_agent_config
    import agentic_core.agents as _agents_mod
    from agentic_core.agent_registry import get_agent_def as _get_v2_agent
    from concurrent.futures import ThreadPoolExecutor, as_completed
    
    agent_a_def = _get_v2_agent(req.agent_a)
    agent_b_def = _get_v2_agent(req.agent_b)
    
    if not agent_a_def:
        raise HTTPException(status_code=404, detail=f"Agent A '{req.agent_a}' not found")
    if not agent_b_def:
        raise HTTPException(status_code=404, detail=f"Agent B '{req.agent_b}' not found")
    
    def run_agent(agent_def):
        _agents_mod._set_sub_agent_config({
            "model_id": agent_def.get("model_id") or _configs.get("global", {}).get("sub_agent_model", "us.anthropic.claude-sonnet-4-6"),
            "max_tokens": agent_def.get("max_tokens", 4096),
            "scenario_da_tools": agent_def.get("tools", []),
            "scenario_prompt_context": agent_def.get("system_prompt", ""),
            "_scenario_cfg": {"datasources": agent_def.get("datasources", [])},
            "skills": agent_def.get("skills", []),
        })
        t0 = time.time()
        try:
            result = deep_data_analysis(req.message)
            return {"text": result, "latency": round(time.time() - t0, 1), "error": None}
        except Exception as e:
            return {"text": "", "latency": round(time.time() - t0, 1), "error": str(e)[:200]}
    
    results = {}
    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = {executor.submit(run_agent, agent_a_def): "a", executor.submit(run_agent, agent_b_def): "b"}
        for future in as_completed(futures):
            label = futures[future]
            results[label] = future.result()
    
    return {
        "ok": True,
        "question": req.message,
        "a": {"agent_id": req.agent_a, "name": agent_a_def.get("name", ""), **results.get("a", {})},
        "b": {"agent_id": req.agent_b, "name": agent_b_def.get("name", ""), **results.get("b", {})},
    }

# ─── V2 Scene API ───

@app.get("/api/v2/scenes")
def api_v2_list_scenes():
    from agentic_core.agent_registry import list_scenes
    scenes = list_scenes()
    return {"ok": True, "scenes": scenes}

@app.get("/api/v2/scenes/{scene_id}")
def api_v2_get_scene(scene_id: str):
    from agentic_core.agent_registry import get_scene
    s = get_scene(scene_id)
    if not s:
        raise HTTPException(status_code=404, detail="Scene not found")
    return {"ok": True, "scene": s}

@app.post("/api/v2/scenes")
def api_v2_create_scene(req: dict):
    from agentic_core.agent_registry import save_scene
    scene = save_scene(req)
    return {"ok": True, "scene": scene}

@app.put("/api/v2/scenes/{scene_id}")
def api_v2_update_scene(scene_id: str, req: dict):
    from agentic_core.agent_registry import get_scene, save_scene
    existing = get_scene(scene_id)
    if not existing:
        raise HTTPException(status_code=404, detail="Scene not found")
    existing.update(req)
    existing["id"] = scene_id
    scene = save_scene(existing)
    return {"ok": True, "scene": scene}

@app.delete("/api/v2/scenes/{scene_id}")
def api_v2_delete_scene(scene_id: str):
    from agentic_core.agent_registry import delete_scene
    delete_scene(scene_id)
    return {"ok": True}

# ─── V1 → V2 Migration ───

@app.post("/api/v2/migrate")
def api_v2_migrate():
    """Migrate V1 agent_definitions + scenarios to V2 format."""
    from agentic_core.agent_registry import migrate_v1_to_v2, migrate_scenes_v1_to_v2
    agents = migrate_v1_to_v2()
    scenes = migrate_scenes_v1_to_v2()
    return {"ok": True, "migrated_agents": agents, "migrated_scenes": scenes}


# Serve React frontend
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
import pathlib

WEB_DIR = pathlib.Path(__file__).parent.parent / "web"

@app.get("/")
def serve_index():
    return FileResponse(WEB_DIR / "index.html", headers={
        "Cache-Control": "no-cache, no-store, must-revalidate",
        "Pragma": "no-cache",
        "Expires": "0",
    })

@app.get("/flow")
def serve_flow():
    return FileResponse(WEB_DIR / "flow-diagram.html", headers={
        "Cache-Control": "no-cache, no-store, must-revalidate",
    })

@app.get("/arch")
def serve_arch():
    return FileResponse(WEB_DIR / "aws-arch.html", headers={
        "Cache-Control": "no-cache, no-store, must-revalidate",
    })

@app.get("/api/health")
def health():
    from config import VERSION, ENVIRONMENT
    ds_count = len(_custom_data_sources) if _custom_data_sources else 0
    return {"status": "ok", "service": "agentic-data", "version": VERSION, "env": ENVIRONMENT, "datasources": ds_count}

# ═══════ Skills Management ═══════
# Skills = reusable prompt fragments (domain knowledge, SQL strategies, etc.)
# Stored in DDB config table, key = "skills"

def _load_skills():
    """Load skills from DDB."""
    try:
        resp = boto3.resource("dynamodb", region_name=REGION).Table(CONFIG_TABLE).get_item(Key={"config_key": "skills"})
        if "Item" in resp:
            return json.loads(resp["Item"].get("data", resp["Item"].get("value", "{}")))
    except Exception as _e:

        print(f"[WARN] swallowed exception: {_e}")
    return {}

def _save_skills(skills: dict):
    """Save skills to DDB."""
    val = json.dumps(skills, ensure_ascii=False, default=str)
    boto3.resource("dynamodb", region_name=REGION).Table(CONFIG_TABLE).put_item(
        Item={"config_key": "skills", "data": val, "value": val}
    )

@app.get("/api/skills")
def api_list_skills():
    return {"ok": True, "skills": _load_skills()}

@app.get("/api/skills/{skill_id}")
def api_get_skill(skill_id: str):
    skills = _load_skills()
    if skill_id not in skills:
        return JSONResponse({"error": "Skill not found"}, 404)
    return {"ok": True, "skill": {**skills[skill_id], "id": skill_id}}

@app.put("/api/skills/{skill_id}")
def api_upsert_skill(skill_id: str, req: dict):
    skills = _load_skills()
    now = datetime.now().isoformat()
    existing = skills.get(skill_id, {})
    skill = {
        "name": req.get("name", existing.get("name", skill_id)),
        "description": req.get("description", existing.get("description", "")),
        "content": req.get("content", existing.get("content", "")),
        "tags": req.get("tags", existing.get("tags", [])),
        "updated_at": now,
        "created_at": existing.get("created_at", now),
    }
    skills[skill_id] = skill
    _save_skills(skills)
    return {"ok": True, "skill": {**skill, "id": skill_id}}

@app.delete("/api/skills/{skill_id}")
def api_delete_skill(skill_id: str):
    skills = _load_skills()
    if skill_id not in skills:
        return JSONResponse({"error": "Skill not found"}, 404)
    del skills[skill_id]
    _save_skills(skills)
    return {"ok": True}


# ═══════ Custom Tools Management ═══════
# Custom tools = user-defined tools stored in DDB
# Sources: MCP servers, code snippets, API wrappers, etc.
# Built-in tools (@tool in code) are read-only.

def _load_custom_tools():
    try:
        resp = boto3.resource("dynamodb", region_name=REGION).Table(CONFIG_TABLE).get_item(Key={"config_key": "custom_tools"})
        if "Item" in resp:
            return json.loads(resp["Item"].get("data", resp["Item"].get("value", "{}")))
    except Exception as _e:

        print(f"[WARN] swallowed exception: {_e}")
    return {}

def _save_custom_tools(tools: dict):
    val = json.dumps(tools, ensure_ascii=False, default=str)
    boto3.resource("dynamodb", region_name=REGION).Table(CONFIG_TABLE).put_item(
        Item={"config_key": "custom_tools", "data": val, "value": val}
    )

BUILTIN_TOOL_NAMES = {"semantic_query", "nl2sql_query", "pg_query", "snowflake_query", "get_data_catalog",
                       "manage_alert_rules", "manage_kpi_rules", "save_report", "list_reports",
                       "deep_data_analysis", "deep_data_analyst_analysis", "list_datasets"}

@app.get("/api/tools")
def api_list_all_tools():
    """List all tools from all sources: built-in, custom, MCP."""
    # 1. Built-in tools (from code)
    builtin = []
    from agentic_core.tools import semantic_query, nl2sql_query, pg_query, snowflake_query, get_data_catalog
    from agentic_core.tools import manage_alert_rules, manage_kpi_rules, save_report, list_reports
    tool_fns = [semantic_query, nl2sql_query, pg_query, snowflake_query, get_data_catalog,
                manage_alert_rules, manage_kpi_rules, save_report, list_reports]
    for fn in tool_fns:
        spec = getattr(fn, '_tool_spec', None) or {}
        builtin.append({
            "name": spec.get("name", fn.__name__),
            "description": spec.get("description", fn.__doc__ or "")[:200],
            "source": "built-in",
            "editable": False
        })

    # 2. Custom tools (from DDB)
    custom = []
    for tid, t in _load_custom_tools().items():
        custom.append({**t, "id": tid, "source": t.get("source", "custom"), "editable": True})

    # 3. MCP tools (from connected servers)
    mcp_tools = []
    _load_ext_mcp()
    for s in (_ext_mcp_servers or []):
        for tname in (s.get("tools") or []):
            mcp_tools.append({
                "name": tname, "description": f"MCP tool from {s['name']}",
                "source": f"mcp:{s['name']}", "editable": False, "mcp_server": s["name"]
            })

    return {"ok": True, "builtin": builtin, "custom": custom, "mcp": mcp_tools}

@app.put("/api/tools/{tool_id}")
def api_upsert_custom_tool(tool_id: str, req: dict):
    if tool_id in BUILTIN_TOOL_NAMES:
        return JSONResponse({"error": "Cannot modify built-in tool"}, 400)
    tools = _load_custom_tools()
    now = datetime.now().isoformat()
    existing = tools.get(tool_id, {})
    tool = {
        "name": req.get("name", existing.get("name", tool_id)),
        "description": req.get("description", existing.get("description", "")),
        "source": req.get("source", existing.get("source", "custom")),
        "type": req.get("type", existing.get("type", "mcp")),
        "mcp_server": req.get("mcp_server", existing.get("mcp_server", "")),
        "config": req.get("config", existing.get("config", {})),
        "updated_at": now,
        "created_at": existing.get("created_at", now),
    }
    tools[tool_id] = tool
    _save_custom_tools(tools)
    return {"ok": True, "tool": {**tool, "id": tool_id}}

@app.delete("/api/tools/{tool_id}")
def api_delete_custom_tool(tool_id: str):
    if tool_id in BUILTIN_TOOL_NAMES:
        return JSONResponse({"error": "Cannot delete built-in tool"}, 400)
    tools = _load_custom_tools()
    if tool_id not in tools:
        return JSONResponse({"error": "Tool not found"}, 404)
    del tools[tool_id]
    _save_custom_tools(tools)
    return {"ok": True}# ═══════ Third-party Model Management ═══════

_custom_models = []

_custom_models_load_ts = [0]

def _load_custom_models():
    global _custom_models
    # 30s TTL cache
    if _custom_models and (time.time() - _custom_models_load_ts[0]) < 30:
        return
    try:
        resp = boto3.resource("dynamodb", region_name=REGION).Table(CONFIG_TABLE).get_item(Key={"config_key": "custom_models"})
        if "Item" in resp:
            import json as _j
            raw = resp["Item"].get("value") or resp["Item"].get("data", "[]")
            if isinstance(raw, str):
                _custom_models = _j.loads(raw)
            elif isinstance(raw, list):
                _custom_models = raw
            elif isinstance(raw, dict):
                _custom_models = list(raw.values())
            else:
                _custom_models = []
            print(f"[Models] Loaded {len(_custom_models)} models from DDB: {[m.get('name') for m in _custom_models]}")
    except Exception as _e:
        print(f"[Models] Failed to load custom_models: {_e}")
    _custom_models_load_ts[0] = time.time()

def _save_custom_models():
    try:
        import json as _j
        _val = _j.dumps(_custom_models, ensure_ascii=False)
        boto3.resource("dynamodb", region_name=REGION).Table(CONFIG_TABLE).put_item(
            Item={"config_key": "custom_models", "data": _val, "value": _val}
        )
        # Invalidate agent-side custom model cache
        try:
            from agentic_core.agents import invalidate_custom_models_cache
            invalidate_custom_models_cache()
        except Exception as _e:

            print(f"[WARN] swallowed exception: {_e}")
    except: pass

@app.get("/api/models")
def api_list_models():
    """List all models: Bedrock + custom third-party, all from DDB."""
    from agentic_core import AVAILABLE_MODELS, SUB_AGENT_MODELS, reload_models
    reload_models()  # always refresh from DDB
    _load_custom_models()
    bedrock_models = [m for m in _custom_models if m.get("protocol") == "bedrock"]
    third_party = [m for m in _custom_models if m.get("protocol") != "bedrock"]
    return {
        "bedrock": [{"name": m["name"], "model_id": m["model_id"], "provider": "bedrock", "type": "bedrock"} for m in bedrock_models],
        "builtin": [{"name": m["name"], "model_id": m["model_id"], "provider": "bedrock", "type": "bedrock"} for m in bedrock_models],
        "custom": third_party,
        "provider": os.environ.get("AGENTIC_AUTO_MODEL_PROVIDER", "bedrock"),
    }

@app.post("/api/models/custom")
def api_add_custom_model(req: dict):
    """Add any model — Bedrock or third-party (OpenAI-compatible, Ollama, etc.)."""
    from agentic_core import reload_models
    _load_custom_models()
    name = req.get("name", "").strip()
    model_id = req.get("model_id", "").strip()
    protocol = req.get("protocol", "openai")  # bedrock | openai | ollama | custom
    endpoint = req.get("endpoint", "").strip().rstrip("/")
    api_key = req.get("api_key", "").strip()
    if not name or not model_id:
        return JSONResponse({"error": "name and model_id required"}, 400)
    
    model = {
        "name": name, "model_id": model_id, "protocol": protocol,
        "added_at": __import__("time").time(),
    }
    if protocol == "bedrock":
        model["endpoint"] = "bedrock"
        model["provider"] = "bedrock"
    else:
        if not endpoint:
            return JSONResponse({"error": "endpoint required for non-Bedrock models"}, 400)
        model["endpoint"] = endpoint
        model["api_key"] = api_key
        model["type"] = "custom"
        model["provider"] = "third-party"
    
    # Dedup by name
    _custom_models[:] = [m for m in _custom_models if m["name"] != name]
    _custom_models.append(model)
    _save_custom_models()
    reload_models()
    return {"status": "ok", "model": name}

@app.delete("/api/models/custom/{name}")
def api_del_custom_model(name: str):
    from agentic_core import reload_models
    _load_custom_models()
    _custom_models[:] = [m for m in _custom_models if m["name"] != name]
    _save_custom_models()
    reload_models()
    return {"status": "ok"}

@app.post("/api/models/test")
def api_test_model(req: dict):
    """Test connection to a model endpoint."""
    endpoint = req.get("endpoint", "").strip().rstrip("/")
    api_key = req.get("api_key", "")
    model_id = req.get("model_id", "")
    protocol = req.get("protocol", "openai")
    
    if not model_id:
        return JSONResponse({"error": "model_id required"}, 400)
    
    # Bedrock models don't need endpoint
    if protocol == "bedrock" or endpoint == "bedrock":
        protocol = "bedrock"
        endpoint = ""
    # 非 Bedrock 模型: 自动填充 SiliconFlow endpoint
    elif not endpoint:
        provider = os.environ.get("AGENTIC_AUTO_MODEL_PROVIDER", "bedrock")
        if provider == "siliconflow":
            from config import SILICONFLOW_API_KEY, SILICONFLOW_BASE_URL
            endpoint = SILICONFLOW_BASE_URL.rstrip("/")
            # Strip /v1 suffix — 后面会自动拼接 /v1/chat/completions
            if endpoint.endswith("/v1"):
                endpoint = endpoint[:-3]
            api_key = api_key or SILICONFLOW_API_KEY
            protocol = "openai"
        elif provider == "bedrock":
            protocol = "bedrock"
        else:
            return JSONResponse({"error": "endpoint required"}, 400)
    
    import urllib.request, urllib.error, time as _time
    start = _time.time()
    
    try:
        if protocol == "bedrock":
            # Test Bedrock model
            from config import BEDROCK_REGION
            client = boto3.client("bedrock-runtime", region_name=BEDROCK_REGION)
            import json as _j
            resp = client.invoke_model(
                modelId=model_id,
                body=_j.dumps({"anthropic_version":"bedrock-2023-05-31","max_tokens":32,"messages":[{"role":"user","content":"Hi"}]}),
                contentType="application/json"
            )
            elapsed = round((_time.time() - start) * 1000)
            return {"status": "ok", "latency_ms": elapsed, "message": f"Bedrock {model_id} 连接成功"}
        
        elif protocol in ("openai", "custom"):
            # OpenAI-compatible: POST /v1/chat/completions
            # 智能拼接: 避免 /v1/v1 重复
            if endpoint.endswith("/chat/completions"):
                url = endpoint
            elif endpoint.endswith("/v1"):
                url = endpoint + "/chat/completions"
            else:
                url = endpoint + "/v1/chat/completions"
            headers = {"Content-Type": "application/json"}
            if api_key:
                headers["Authorization"] = f"Bearer {api_key}"
            import json as _j
            body = _j.dumps({
                "model": model_id,
                "messages": [{"role": "user", "content": "Say ok"}],
                "max_tokens": 16, "temperature": 0,
            }).encode()
            if not str(url).startswith(("http://", "https://")):
                raise ValueError("URL must use http:// or https://")
            r = urllib.request.Request(url, data=body, headers=headers, method="POST")
            resp = urllib.request.urlopen(r, timeout=15)  # nosec B310
            data = _j.loads(resp.read())
            elapsed = round((_time.time() - start) * 1000)
            reply = data.get("choices", [{}])[0].get("message", {}).get("content", "")[:50]
            return {"status": "ok", "latency_ms": elapsed, "message": f"连接成功", "reply": reply}
        
        elif protocol == "ollama":
            # Ollama: POST /api/generate
            url = endpoint + "/api/generate"
            headers = {"Content-Type": "application/json"}
            import json as _j
            body = _j.dumps({"model": model_id, "prompt": "Say ok", "stream": False}).encode()
            if not str(url).startswith(("http://", "https://")):
                raise ValueError("URL must use http:// or https://")
            r = urllib.request.Request(url, data=body, headers=headers, method="POST")
            resp = urllib.request.urlopen(r, timeout=30)  # nosec B310
            data = _j.loads(resp.read())
            elapsed = round((_time.time() - start) * 1000)
            reply = data.get("response", "")[:50]
            return {"status": "ok", "latency_ms": elapsed, "message": f"连接成功", "reply": reply}
        
        else:
            return JSONResponse({"error": f"未知协议: {protocol}"}, 400)
    
    except urllib.error.HTTPError as e:
        elapsed = round((_time.time() - start) * 1000)
        body_text = ""
        try: body_text = e.read().decode()[:200]
        except: pass
        return JSONResponse({"status": "error", "latency_ms": elapsed, "message": f"HTTP {e.code}: {body_text}"}, 200)
    except urllib.error.URLError as e:
        return JSONResponse({"status": "error", "message": f"连接失败: {str(e.reason)[:100]}"}, 200)
    except Exception as e:
        return JSONResponse({"status": "error", "message": f"错误: {str(e)[:150]}"}, 200)

app.mount("/static", StaticFiles(directory=str(WEB_DIR)), name="static")

# ─── Data Source APIs (persisted) ───

_data_sources_default = []  # Cleared: user connects from scratch

def _sanitize_ds(ds):
    """Remove sensitive fields before returning to frontend."""
    import copy
    out = copy.deepcopy(ds)
    cfg = out.get("config", {})
    for secret_key in ("password", "api_key", "secret_key", "secret_access_key"):
        if secret_key in cfg:
            cfg[secret_key] = "••••••"
    return out

@app.get("/api/datasources")
def api_datasources():
    global _custom_data_sources
    # Reload from DynamoDB to stay in sync across workers
    _custom_data_sources = _load_config("custom_datasources", [])
    return {"sources": [_sanitize_ds(ds) for ds in _data_sources_default + _custom_data_sources]}


def _resolve_aws_creds(config):
    """Resolve AWS credentials from datasource config.
    Supports 3 modes: platform (default IAM Role), assume_role (STS), aksk (direct keys).
    Returns dict of kwargs for boto3.client/resource.
    """
    region = config.get("region", REGION)
    auth_mode = config.get("auth_mode", "platform")
    kwargs = {"region_name": region}

    if auth_mode == "assume_role":
        role_arn = config.get("role_arn", "").strip()
        external_id = config.get("external_id", "").strip()
        if not role_arn:
            raise ValueError("AssumeRole 模式需要提供 Role ARN")
        sts = boto3.client("sts", region_name=region)
        assume_params = {"RoleArn": role_arn, "RoleSessionName": "agentic-data-cross-account", "DurationSeconds": 3600}
        if external_id:
            assume_params["ExternalId"] = external_id
        creds = sts.assume_role(**assume_params)["Credentials"]
        kwargs["aws_access_key_id"] = creds["AccessKeyId"]
        kwargs["aws_secret_access_key"] = creds["SecretAccessKey"]
        kwargs["aws_session_token"] = creds["SessionToken"]
    elif auth_mode == "aksk":
        ak = config.get("access_key", "").strip()
        sk = config.get("secret_key", "").strip()
        if not ak or not sk:
            raise ValueError("AK/SK 模式需要提供 Access Key 和 Secret Key")
        kwargs["aws_access_key_id"] = ak
        kwargs["aws_secret_access_key"] = sk
    # else: platform mode, use default IAM Role

    return kwargs

def _boto3_client(service, config):
    """Create boto3 client with resolved credentials."""
    return boto3.client(service, **_resolve_aws_creds(config))

def _boto3_resource(service, config):
    """Create boto3 resource with resolved credentials."""
    return boto3.resource(service, **_resolve_aws_creds(config))

@app.post("/api/datasources/test")
def api_datasource_test(req: dict):
    ds_type = req.get("type", "")
    config = req.get("config", {})
    # Support passing full datasource object (from card test button)
    if not ds_type and req.get("id"):
        ds_type = req.get("type", "")
    # Merge top-level fields into config for datasources stored with flat structure
    for k in ["database", "region", "host", "port", "output_location", "user", "password", "schema"]:
        if k in req and k not in config:
            config[k] = req[k]
    # If password is masked (from sanitized frontend), restore from DDB
    ds_id = req.get("id", "")
    if ds_id and config.get("password") in ("••••••", "******", ""):
        saved = _load_config("custom_datasources", [])
        match = next((d for d in saved if d.get("id") == ds_id), None)
        if match and match.get("config", {}).get("password"):
            config["password"] = match["config"]["password"]
    try:
        if ds_type == "DynamoDB":
            table_name = config.get("table", "")
            region = config.get("region", REGION)
            ddb = _boto3_resource("dynamodb", config)
            t = ddb.Table(table_name)
            resp = t.scan(Limit=1)
            return {"ok": True, "message": f"连接成功 — 表 {table_name}，约 {t.item_count} 条记录", "sample_keys": list(resp.get("Items",[{}])[0].keys()) if resp.get("Items") else []}
        elif ds_type == "S3":
            bucket = config.get("bucket", "")
            prefix = config.get("prefix", "")
            region = config.get("region", REGION)
            s3c = _boto3_client("s3", config)
            resp = s3c.list_objects_v2(Bucket=bucket, Prefix=prefix, MaxKeys=5)
            files = [o["Key"] for o in resp.get("Contents", [])]
            return {"ok": True, "message": f"连接成功 — {bucket}/{prefix}，{resp.get('KeyCount',0)} 个对象", "sample_files": files}
        elif ds_type in ("Athena", "athena"):
            database = config.get("database", "")
            region = config.get("region", REGION)
            ath = _boto3_client("athena", config)
            resp = ath.list_table_metadata(CatalogName="AwsDataCatalog", DatabaseName=database, MaxResults=10)
            tables = [t["Name"] for t in resp.get("TableMetadataList", [])]
            return {"ok": True, "message": f"连接成功 — 数据库 {database}，{len(tables)} 张表", "tables": tables}
        elif ds_type in ("postgresql", "PostgreSQL"):
            host = config.get("host", os.environ.get("POSTGRES_HOST", ""))
            port = int(config.get("port", os.environ.get("POSTGRES_PORT", "5432")))
            database = config.get("database", os.environ.get("POSTGRES_DATABASE", ""))
            user = config.get("user", os.environ.get("POSTGRES_USER", ""))
            password = config.get("password", os.environ.get("POSTGRES_PASSWORD", ""))
            pg_schema = config.get("schema", "").strip()
            if not host:
                return {"ok": False, "message": "PostgreSQL 未配置: 缺少 host"}
            try:
                import psycopg2
                conn = psycopg2.connect(host=host, port=port, dbname=database, user=user, password=password, connect_timeout=10)
                cur = conn.cursor()
                if pg_schema:
                    schemas_to_scan = [pg_schema]
                else:
                    cur.execute("SELECT schema_name FROM information_schema.schemata WHERE schema_name NOT IN ('pg_catalog','information_schema','pg_toast') ORDER BY schema_name")
                    schemas_to_scan = [r[0] for r in cur.fetchall()]
                tables = []
                row_info = []
                for sch in schemas_to_scan:
                    cur.execute("SELECT table_name FROM information_schema.tables WHERE table_schema=%s ORDER BY table_name", (sch,))
                    for (t,) in cur.fetchall():
                        cur.execute(f'SELECT COUNT(*) FROM "{_safe_id(sch)}"."{_safe_id(t)}"')  # nosec B608
                        cnt = cur.fetchone()[0]
                        label = f"{sch}.{t}" if len(schemas_to_scan) > 1 else t
                        tables.append(label)
                        row_info.append(f"{label}({cnt:,}行)")
                conn.close()
                schema_label = pg_schema if pg_schema else ', '.join(schemas_to_scan)
                return {"ok": True, "message": f"连接成功 — {host}:{port}/{database} (schema: {schema_label})，{len(tables)} 张表: {', '.join(row_info)}", "tables": tables}
            except Exception as e:
                return {"ok": False, "message": f"PostgreSQL 连接失败: {str(e)[:200]}"}
        elif ds_type == "RDS":
            engine = config.get("engine", "mysql").lower()
            host = config.get("host", req.get("host", ""))
            port = int(config.get("port", req.get("port", "3306" if engine == "mysql" else "5432")))
            database = config.get("database", req.get("database", ""))
            user = config.get("username", config.get("user", req.get("user", "admin")))
            password = config.get("password", req.get("password", ""))
            if not host:
                return {"ok": False, "message": "请填写 Host"}
            try:
                if engine == "mysql":
                    import pymysql
                    conn = pymysql.connect(host=host, port=port, database=database or None,
                                           user=user, password=password, connect_timeout=10, charset='utf8mb4')
                    cur = conn.cursor()
                    cur.execute("SELECT table_name FROM information_schema.tables WHERE table_schema=%s", (database or 'information_schema',))
                    tables = [r[0] for r in cur.fetchall()]
                    row_info = []
                    for t in tables[:20]:
                        cur.execute(f"SELECT COUNT(*) FROM `{_safe_id(database)}`.`{_safe_id(t)}`")  # nosec B608
                        cnt = cur.fetchone()[0]
                        row_info.append(f"{t}({cnt}行)")
                    conn.close()
                    return {"ok": True, "message": f"MySQL 连接成功 — {host}:{port}/{database}，{len(tables)} 张表: {', '.join(row_info)}", "tables": tables}
                else:
                    import psycopg2
                    conn = psycopg2.connect(host=host, port=port, dbname=database, user=user, password=password, connect_timeout=10)
                    cur = conn.cursor()
                    cur.execute("SELECT table_name FROM information_schema.tables WHERE table_schema='public' AND table_type='BASE TABLE'")
                    tables = [r[0] for r in cur.fetchall()]
                    row_info = []
                    for t in tables[:20]:
                        cur.execute(f'SELECT COUNT(*) FROM "{_safe_id(t)}"')  # nosec B608
                        cnt = cur.fetchone()[0]
                        row_info.append(f"{t}({cnt}行)")
                    conn.close()
                    return {"ok": True, "message": f"PostgreSQL 连接成功 — {host}:{port}/{database}，{len(tables)} 张表: {', '.join(row_info)}", "tables": tables}
            except Exception as e:
                return {"ok": False, "message": f"RDS {engine} 连接失败: {str(e)[:200]}"}
        elif ds_type == "SQLite":
            try:
                import sqlite3
                db_path = config.get("db_path", "/app/data/agentic_auto.db")
                conn = sqlite3.connect(db_path)
                tables = [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()]
                row_info = []
                for t in tables:
                    cnt = conn.execute(f"SELECT COUNT(*) FROM {_safe_id(t)}").fetchone()[0]  # nosec B608
                    row_info.append(f"{t}({cnt}行)")
                conn.close()
                return {"ok": True, "message": f"连接成功 — SQLite {db_path}，{len(tables)} 张表: {', '.join(row_info)}"}
            except Exception as e:
                return {"ok": False, "message": f"SQLite 连接失败: {str(e)[:100]}"}
        elif ds_type == "Snowflake":
            account = config.get("account", "")
            user = config.get("user", "")
            password = config.get("password", "")
            warehouse = config.get("warehouse", "COMPUTE_WH")
            database = config.get("database", "")
            schema = config.get("schema", "PUBLIC")
            if not all([account, user, password, database]):
                return {"ok": False, "message": "缺少必填字段: account, user, password, database"}
            try:
                import snowflake.connector
                conn = snowflake.connector.connect(
                    account=account, user=user, password=password,
                    warehouse=warehouse, database=database, schema=schema,
                    login_timeout=10,
                )
                cur = conn.cursor()
                cur.execute("SELECT CURRENT_DATABASE(), CURRENT_WAREHOUSE(), CURRENT_SCHEMA()")
                row = cur.fetchone()
                cur.execute("SHOW TABLES")
                tables = [r[1] for r in cur.fetchall()]
                conn.close()
                return {"ok": True, "message": f"连接成功 — {row[0]}.{row[2]} @ {row[1]}，{len(tables)} 张表", "tables": tables[:20]}
            except ImportError:
                return {"ok": False, "message": "Snowflake connector 未安装 (pip install snowflake-connector-python)"}
            except Exception as e:
                return {"ok": False, "message": f"Snowflake 连接失败: {str(e)[:200]}"}
        elif ds_type == "Redshift":
            cluster = config.get("cluster", "")
            database = config.get("database", "dev")
            workgroup = config.get("workgroup", "")
            region = config.get("region", REGION)
            if not (cluster or workgroup):
                return {"ok": False, "message": "请填写集群名称或 Serverless Workgroup"}
            try:
                client = boto3.client("redshift-data", region_name=region)
                params = {"Database": database, "Sql": "SELECT tablename FROM pg_tables WHERE schemaname='public' LIMIT 20"}
                if workgroup:
                    params["WorkgroupName"] = workgroup
                else:
                    params["ClusterIdentifier"] = cluster
                    params["DbUser"] = config.get("user", "admin")
                resp = client.execute_statement(**params)
                stmt_id = resp["Id"]
                # Poll for result (up to 10s)
                import time as _t
                for _ in range(20):
                    _t.sleep(0.5)
                    status = client.describe_statement(Id=stmt_id)
                    if status["Status"] in ("FINISHED", "FAILED", "ABORTED"):
                        break
                if status["Status"] == "FINISHED":
                    result = client.get_statement_result(Id=stmt_id)
                    tables = [r[0]["stringValue"] for r in result.get("Records", []) if r]
                    return {"ok": True, "message": f"连接成功 — {database}，{len(tables)} 张表", "tables": tables}
                else:
                    return {"ok": False, "message": f"Redshift 查询失败: {status.get('Error', status['Status'])}"}
            except Exception as e:
                return {"ok": False, "message": f"Redshift 连接失败: {str(e)[:200]}"}
        elif ds_type == "OpenSearch":
            domain = config.get("domain", "")
            if not domain:
                return {"ok": False, "message": "请填写 OpenSearch 域端点"}
            try:
                from urllib.request import urlopen, Request
                import json as _j
                url = domain.rstrip("/") + "/_cat/indices?format=json"
                req = Request(url, headers={"Content-Type": "application/json"})
                resp = urlopen(req, timeout=10)  # nosec B310
                indices = _j.loads(resp.read())
                names = [idx["index"] for idx in indices if not idx["index"].startswith(".")]
                return {"ok": True, "message": f"连接成功 — {len(names)} 个索引", "indices": names[:20]}
            except Exception as e:
                return {"ok": False, "message": f"OpenSearch 连接失败: {str(e)[:200]}"}
        else:
            return {"ok": False, "message": f"不支持的数据源类型: {ds_type}"}
    except Exception as e:
        return {"ok": False, "message": f"连接失败: {str(e)}"}

@app.put("/api/datasources/{ds_id}")
def api_update_datasource(ds_id: str, req: dict):
    """Edit an existing datasource — update any provided fields."""
    global _custom_data_sources
    _custom_data_sources = _load_config("custom_datasources", [])
    found = None
    for ds in _custom_data_sources:
        if ds.get("id") == ds_id:
            found = ds
            break
    if not found:
        return {"ok": False, "error": f"数据源 {ds_id} 不存在"}
    # Update fields
    for k in ["name", "type", "description", "database", "region", "host", "port",
              "output_location", "enabled", "tables", "table_descriptions", "config"]:
        if k in req:
            found[k] = req[k]
    _save_config("custom_datasources", _custom_data_sources)
    try:
        from agentic_core.tools import invalidate_pg_pool
        invalidate_pg_pool()
    except: pass
    bump_data_version()
    _agents.clear()
    return {"ok": True, "source": found}

@app.post("/api/datasources")
def api_add_datasource(req: dict):
    ds = {
        "id": f"custom-{uuid.uuid4().hex[:8]}",
        "name": req.get("name", "自定义数据源"),
        "type": req.get("type", ""),
        "icon": "db",
        "config": req.get("config", {}),
        "description": req.get("description", ""),
        "tools": [],
        "custom": True,
    }
    _custom_data_sources.append(ds)
    _save_config("custom_datasources", _custom_data_sources)
    try:
        from agentic_core.tools import invalidate_pg_pool
        invalidate_pg_pool()
    except: pass  # Persist
    bump_data_version(); _agents.clear(); # semantic already loaded at startup  # 新数据源 → 清所有 Agent 缓存, 下次请求重建
    return {"ok": True, "source": ds}

@app.delete("/api/datasources/{ds_id}")
def api_delete_datasource(ds_id: str):
    global _custom_data_sources

    # Find the datasource before removing
    removed = None
    for ds in _custom_data_sources:
        if ds.get("id") == ds_id:
            removed = ds
            break

    _custom_data_sources = [d for d in _custom_data_sources if d["id"] != ds_id]
    _save_config("custom_datasources", _custom_data_sources)
    try:
        from agentic_core.tools import invalidate_pg_pool
        invalidate_pg_pool()
    except: pass
    bump_data_version(); _agents.clear(); # semantic already loaded at startup  # 删数据源 → 清 Agent 缓存

    # Derive dataset_name(s) from ds config or id
    dataset_names = []
    if removed:
        cfg = removed.get("config", {})
        prefix = cfg.get("prefix", "")
        key = cfg.get("key", "")
        # Try to extract from S3 path: chatbi/battery_health.json → battery_health
        for p in [prefix, key]:
            if "chatbi/" in p:
                dataset_names.append(p.split("chatbi/")[-1].replace(".json","").replace(".csv",""))
                break
        # datalake 数据源: prefix 是目录, 可能对应多个 dataset
        if not dataset_names and ("datalake" in prefix or "datalake" in key):
            # 从 ChatBI DATASETS 里找所有匹配的 dataset
            from agentic_core.tools import CHATBI_DATASETS
            # 尝试直接用数据源名称匹配
            ds_name_clean = removed.get("name", "").replace(" ", "_").replace("-", "_").lower()
            for ds_key in list(CHATBI_DATASETS.keys()):
                if ds_key == ds_name_clean or ds_key in ds_name_clean or ds_name_clean in ds_key:
                    dataset_names.append(ds_key)
            # 如果还没找到, 把所有 datalake 来源的 dataset 都加上
            if not dataset_names:
                for ds_key, ds_val in list(CHATBI_DATASETS.items()):
                    src = ds_val.get("source", "")
                    if "datalake" in src or prefix in src:
                        dataset_names.append(ds_key)
        # Fallback: use datasource name (cleaned)
        if not dataset_names:
            dataset_names.append(removed.get("name", "").replace(" ", "_").replace("-", "_"))
    # Legacy: chatbi_ prefix in id
    if not dataset_names and ds_id.startswith("chatbi_"):
        dataset_names.append(ds_id.replace("chatbi_", "", 1))
    dataset_name = dataset_names[0] if dataset_names else ""
    cleaned = {"metrics": [], "dimensions": [], "synonyms": [], "chatbi": False, "dataset": dataset_name}

    # 清理所有关联 dataset（datalake 可能有多个）
    all_datasets = dataset_names if dataset_names else ([dataset_name] if dataset_name else [])
    from agentic_core.tools import CHATBI_DATASETS; _chatbi_cache = {}
    from agentic_core.semantic_layer import METRICS, DIMENSIONS, SYNONYMS
    _load_semantic_custom()

    for ds_name in all_datasets:
        if not ds_name:
            continue
        # 1. Remove from ChatBI registry + cache
        if ds_name in CHATBI_DATASETS:
            del CHATBI_DATASETS[ds_name]
            _chatbi_cache.pop(ds_name, None)
            cleaned["chatbi"] = True

        # 2. Remove auto-generated metrics for this dataset
        metrics_to_remove = [
            name for name, defn in list(METRICS.items())
            if defn.get("dataset") == ds_name
            and name in _semantic_custom.get("metrics", {})
        ]
        for name in metrics_to_remove:
            METRICS.pop(name, None)
            _semantic_custom["metrics"].pop(name, None)
            cleaned["metrics"].append(name)

        # 3. Remove auto-generated dimensions
        dims_to_remove = [
            name for name, defn in list(DIMENSIONS.items())
            if defn.get("chatbi_field") and name in _semantic_custom.get("dimensions", {})
            and _semantic_custom["dimensions"][name].get("dataset", "") == ds_name
        ]
        for name in dims_to_remove:
            DIMENSIONS.pop(name, None)
            _semantic_custom["dimensions"].pop(name, None)
            cleaned["dimensions"].append(name)

        # 4. Remove synonyms that originated from this dataset
        syn_sources = _semantic_custom.get("synonym_sources", {})
        syns_to_remove = [
            alias for alias, src in list(syn_sources.items())
            if src == ds_name
        ]
        for alias in syns_to_remove:
            SYNONYMS.pop(alias, None)
            _semantic_custom["synonyms"].pop(alias, None)
            _semantic_custom.get("synonym_sources", {}).pop(alias, None)

        # 5. Delete S3 data (chatbi/ JSON + datalake/ Parquet)
        try:
            _s3.delete_object(Bucket=DATA_BUCKET, Key=f"chatbi/{ds_name}.json")
        except Exception as _e:

            print(f"[WARN] swallowed exception: {_e}")
        # Also clean datalake directories (ODS/DWD/DWS/ADS layers)
        try:
            for layer in ["ods", "dwd", "dws", "ads"]:
                prefix_key = f"datalake/{layer}/{ds_name}/"
                resp_s3 = _s3.list_objects_v2(Bucket=DATA_BUCKET, Prefix=prefix_key, MaxKeys=100)
                for obj in resp_s3.get("Contents", []):
                    _s3.delete_object(Bucket=DATA_BUCKET, Key=obj["Key"])
        except Exception as _e:

            print(f"[WARN] swallowed exception: {_e}")

    # Persist cleaned ChatBI datasets
    if cleaned["chatbi"]:
        _save_config("custom_chatbi_datasets", {
            k: v for k, v in CHATBI_DATASETS.items()
            if k not in ("vehicle_master", "app_usage", "service_records",
                         "driving_daily", "battery_health", "ota_records",
                         "customer_feedback", "charging_records")
        })



    if cleaned['metrics'] or cleaned['dimensions'] or cleaned['synonyms']:
        _save_semantic_custom()
    # 6. Clean dashboards that reference deleted datasets
    try:
        scan = _ddb.Table(CONFIG_TABLE).scan(
            FilterExpression=Attr("config_key").begins_with("dashboard:")
        )
        for item in scan.get("Items", []):
            db_data = item.get("data", "{}")
            if isinstance(db_data, str):
                db_data = _json.loads(db_data)
            cards = db_data.get("cards", [])
            # Remove cards whose dataset matches any deleted dataset
            original_count = len(cards)
            cards = [c for c in cards if c.get("dataset", "") not in all_datasets]
            if len(cards) < original_count:
                if cards:
                    db_data["cards"] = cards
                    _ddb.Table(CONFIG_TABLE).update_item(
                        Key={"config_key": item["config_key"]},
                        UpdateExpression="SET #d = :d",
                        ExpressionAttributeNames={"#d": "data"},
                        ExpressionAttributeValues={":d": _json.dumps(db_data, ensure_ascii=False)},
                    )
                else:
                    # All cards removed — delete the dashboard
                    _ddb.Table(CONFIG_TABLE).delete_item(Key={"config_key": item["config_key"]})
                cleaned["dashboards"] = cleaned.get("dashboards", 0) + (original_count - len(cards))
    except Exception as _e:

        print(f"[WARN] swallowed exception: {_e}")

    # 7. Force recreate agents (tool descriptions changed)
    _agents.clear()

    return {"ok": True, "cleaned": cleaned}


@app.post("/api/datasources/{ds_id}/preview-semantic")
def api_ds_preview_semantic(ds_id: str):
    """Preview semantic layer for an existing datasource (schema inference without applying)."""
    # Find the datasource
    all_ds = _data_sources_default + _custom_data_sources
    ds = None
    for d in all_ds:
        if d.get("id") == ds_id:
            ds = d
            break
    if not ds:
        return JSONResponse({"error": "数据源不存在"}, 404)

    ds_type = ds.get("type", "")
    config = ds.get("config", {})
    
    from agentic_core.schema_inference import (
        introspect_dynamodb, introspect_s3_json, introspect_s3_prefix,
        introspect_athena, introspect_sql_engine
    )
    
    try:
        introspection = []
        if ds_type == "DynamoDB":
            result = introspect_dynamodb(config.get("table", ""), config.get("region", REGION), sample_limit=200,
                                          access_key=config.get("access_key",""), secret_key=config.get("secret_key",""), role_arn=config.get("role_arn",""), external_id=config.get("external_id",""))
            if "error" not in result:
                introspection = [result]
        elif ds_type == "S3":
            bucket = config.get("bucket", "")
            key = config.get("key", "")
            prefix = config.get("prefix", "")
            if key:
                result = introspect_s3_json(bucket, key, config.get("region", REGION),
                                              access_key=config.get("access_key",""), secret_key=config.get("secret_key",""), role_arn=config.get("role_arn",""), external_id=config.get("external_id",""))
                if "error" not in result:
                    introspection = [result]
            else:
                introspection = introspect_s3_prefix(bucket, prefix, config.get("region", REGION),
                                                       access_key=config.get("access_key",""), secret_key=config.get("secret_key",""), role_arn=config.get("role_arn",""), external_id=config.get("external_id",""))
        elif ds_type == "Athena":
            introspection = introspect_athena(config.get("database", ""), config.get("region", REGION),
                                                access_key=config.get("access_key",""), secret_key=config.get("secret_key",""), role_arn=config.get("role_arn",""), external_id=config.get("external_id",""))
        elif ds_type in ("RDS", "SQLite", "Snowflake", "PostgreSQL"):
            from agentic_core.db_engine import get_engine
            # Try engine_name from config, then config.engine (for RDS), then type-based lookup
            eng_name = config.get("engine_name", "")
            eng = get_engine(eng_name) if eng_name else None
            if not eng and ds_type == "RDS":
                # RDS stores actual engine in config.engine (mysql/postgresql)
                eng = get_engine(config.get("engine", "mysql").lower())
            if not eng:
                eng = get_engine(ds_type.lower())  # e.g. "sqlite", "snowflake", "postgresql"
            if not eng:
                eng = get_engine()  # fallback to default (sqlite)
            if eng:
                introspection = introspect_sql_engine(eng)
        
        if not introspection:
            return {"ok": False, "message": f"无法从 {ds_type} 数据源推断 Schema"}
        
        # Merge all results
        all_metrics = {}
        all_dims = {}
        all_synonyms = {}
        for r in introspection:
            all_metrics.update(r.get("metrics", {}))
            all_dims.update(r.get("dimensions", {}))
            all_synonyms.update(r.get("synonyms", {}))
        
        return {
            "ok": True,
            "ds_id": ds_id,
            "ds_name": ds.get("name", ds_id),
            "metrics": all_metrics,
            "dimensions": all_dims,
            "synonyms": all_synonyms,
            "summary": f"{len(all_metrics)} 指标, {len(all_dims)} 维度, {len(all_synonyms)} 同义词"
        }
    except Exception as e:
        print(f"Preview semantic for {ds_id} failed: {e}")
        return {"ok": False, "message": str(e)}


@app.post("/api/datasources/{ds_id}/apply-semantic")
def api_ds_apply_semantic(ds_id: str, req: dict = {}):
    """Apply (merge) semantic layer from datasource inference results."""
    # First do the inference
    preview = api_ds_preview_semantic(ds_id)
    if not preview.get("ok"):
        return preview
    
    metrics = req.get("metrics") or preview.get("metrics", {})
    dims = req.get("dimensions") or preview.get("dimensions", {})
    synonyms = req.get("synonyms") or preview.get("synonyms", {})
    
    applied = {"metrics": 0, "dimensions": 0, "synonyms": 0}
    
    # Ensure _semantic_custom sub-keys are dicts (DDB may store as list)
    _load_semantic_custom()
    for sk in ("metrics", "dimensions", "synonyms"):
        val = _semantic_custom.get(sk)
        if isinstance(val, list):
            if sk == "synonyms":
                _semantic_custom[sk] = {item.get("term", item.get("alias", "")): item.get("maps_to", item.get("target", "")) for item in val if isinstance(item, dict)}
            else:
                _semantic_custom[sk] = {item["name"]: item for item in val if isinstance(item, dict) and "name" in item}
        elif not isinstance(val, dict):
            _semantic_custom[sk] = {}
    
    if metrics:
        from agentic_core.semantic_layer import METRICS
        for name, defn in metrics.items():
            METRICS[name] = defn
            _semantic_custom["metrics"][name] = defn
        applied["metrics"] = len(metrics)
    
    if dims:
        from agentic_core.semantic_layer import DIMENSIONS
        for name, defn in dims.items():
            DIMENSIONS[name] = defn
            _semantic_custom["dimensions"][name] = defn
        applied["dimensions"] = len(dims)
    
    if synonyms:
        from agentic_core.semantic_layer import SYNONYMS
        for alias, target in synonyms.items():
            SYNONYMS[alias] = target
            _semantic_custom["synonyms"][alias] = target
        applied["synonyms"] = len(synonyms)
    
    
    # Force agent recreation
    _agents.clear()
    
    return {"ok": True, "applied": applied, "message": f"已应用: {applied['metrics']} 指标, {applied['dimensions']} 维度, {applied['synonyms']} 同义词"}


# ═══════ BI Report Management (Tableau / QuickSight / etc.) ═══════

_default_reports = [
    {
        "id": "tableau-sales",
        "name": "销售分析仪表盘",
        "type": "tableau",
        "url": "https://public.tableau.com/views/SuperstoreSales_17427932468690/Overview",
        "embed_url": "https://public.tableau.com/views/SuperstoreSales_17427932468690/Overview?:embed=y&:showVizHome=no&:toolbar=yes",
        "description": "销售业绩总览 — 营收趋势/区域分布/产品分析/客户分层",
        "icon": "analytics",
        "tags": ["销售", "营收", "趋势"],
        "data_source": "snowflake-dw",
    },
    {
        "id": "tableau-customer",
        "name": "客户画像分析",
        "type": "tableau",
        "url": "https://public.tableau.com/views/CustomerAnalysis_17427932468690/CustomerSegmentation",
        "embed_url": "https://public.tableau.com/views/CustomerAnalysis_17427932468690/CustomerSegmentation?:embed=y&:showVizHome=no&:toolbar=yes",
        "description": "客户分群 — RFM模型/NPS分布/忠诚度分析/流失预警",
        "icon": "users",
        "tags": ["客户", "NPS", "分群"],
        "data_source": "snowflake-dw",
    },
    {
        "id": "tableau-ops",
        "name": "运营监控大屏",
        "type": "tableau",
        "url": "https://public.tableau.com/views/OperationsDashboard/Ops",
        "embed_url": "https://public.tableau.com/views/OperationsDashboard/Ops?:embed=y&:showVizHome=no&:toolbar=yes",
        "description": "实时运营 — 车辆状态/充电网络/OTA覆盖/售后工单",
        "icon": "server",
        "tags": ["运营", "监控", "实时"],
        "data_source": "snowflake-dw",
    },
]

_custom_reports = _load_config("custom_reports", [])

@app.get("/api/reports")
def api_reports():
    """List all BI reports (built-in + custom)."""
    return {"reports": _default_reports + _custom_reports}


@app.post("/api/reports")
def api_add_report(req: dict):
    """Add a custom BI report."""
    global _custom_reports
    r = {
        "id": f"report-{int(__import__('time').time()*1000)}",
        "name": req.get("name", "未命名报表"),
        "type": req.get("type", "tableau"),
        "url": req.get("url", ""),
        "embed_url": req.get("embed_url", req.get("url", "")),
        "description": req.get("description", ""),
        "icon": req.get("icon", "📊"),
        "tags": req.get("tags", []),
        "data_source": req.get("data_source", ""),
        "custom": True,
    }
    _custom_reports.append(r)
    _save_config("custom_reports", _custom_reports)
    return {"ok": True, "report": r}


@app.put("/api/reports/{report_id}")
def api_update_report(report_id: str, req: dict):
    """Update a custom report."""
    global _custom_reports
    for i, r in enumerate(_custom_reports):
        if r["id"] == report_id:
            _custom_reports[i] = {**r, **{k: v for k, v in req.items() if k != "id"}}
            _save_config("custom_reports", _custom_reports)
            return {"ok": True, "report": _custom_reports[i]}
    return {"ok": False, "message": "Report not found"}


@app.delete("/api/reports/{report_id}")
def api_delete_report(report_id: str):
    """Delete a custom report."""
    global _custom_reports
    _custom_reports = [r for r in _custom_reports if r["id"] != report_id]
    _save_config("custom_reports", _custom_reports)
    return {"ok": True}



# ═══════ Tableau Integration API ═══════

@app.get("/api/tableau/status")
def api_tableau_status():
    """Get Tableau connection status and metadata summary."""
    from agentic_core.tableau_client import get_tableau_client
    client = get_tableau_client()
    return client.test_connection()


@app.get("/api/tableau/dashboards")
def api_tableau_dashboards():
    """List all Tableau dashboards/views."""
    from agentic_core.tableau_client import get_tableau_client
    client = get_tableau_client()
    views = client.list_views()
    workbooks = client.list_workbooks()
    return {"workbooks": workbooks, "views": views}


@app.get("/api/tableau/views/{view_id}/data")
def api_tableau_view_data(view_id: str, max_rows: int = 200):
    """Query data from a Tableau view."""
    from agentic_core.tableau_client import get_tableau_client
    client = get_tableau_client()
    return client.query_view_data(view_id, max_rows=max_rows)


@app.get("/api/tableau/datasources")
def api_tableau_datasources():
    """List Tableau data sources with field metadata."""
    from agentic_core.tableau_client import get_tableau_client
    client = get_tableau_client()
    datasources = client.list_datasources()
    result = []
    for ds in datasources:
        fields = client.get_datasource_fields(ds["id"])
        result.append({**ds, "fields": fields, "field_count": len(fields)})
    return {"datasources": result}


@app.get("/api/tableau/semantic-context")
def api_tableau_semantic():
    """Get Tableau semantic context (for Agent prompt injection)."""
    from agentic_core.tableau_client import get_tableau_client
    client = get_tableau_client()
    return {"context": client.build_semantic_context(), "prompt": client.format_prompt_context()}


@app.post("/api/tableau/connect")
def api_tableau_connect(req: dict):
    """Connect to a Tableau Server at runtime (no restart needed)."""
    from agentic_core.tableau_client import TableauClient, reset_client
    reset_client()
    try:
        client = TableauClient(
            server_url=req.get("server_url", ""),
            site_id=req.get("site_id", ""),
            token_name=req.get("token_name", ""),
            token_secret=req.get("token_secret", ""),
            username=req.get("username", ""),
            password=req.get("password", ""),
        )
        test = client.test_connection()
        if test["ok"]:
            # Replace global client
            import agentic_core.tableau_client as tc
            tc._client = client
        return test
    except Exception as e:
        return {"ok": False, "message": str(e)}


# ═══════ SQL Engine Management ═══════

@app.get("/api/sql-engines")
def api_sql_engines():
    """List all registered SQL engines and their status."""
    from agentic_core.db_engine import get_multi_engine
    multi = get_multi_engine()
    engines = []
    for key in multi.engine_names:
        eng = multi.get(key)
        engines.append({
            "key": key, "name": eng.name, "dialect": eng.dialect,
            "is_default": key == multi.default_key,
        })
    return {"engines": engines, "default": multi.default_key}


@app.post("/api/sql-engines/test")
def api_test_sql_engine(req: dict):
    """Test connection to a specific SQL engine."""
    from agentic_core.db_engine import get_multi_engine
    key = req.get("engine", "")
    multi = get_multi_engine()
    if key and key not in multi.engines:
        return {"ok": False, "message": f"Engine \'{key}\' not registered", "details": {}}
    if key:
        return multi.get(key).test_connection()
    return multi.test_all()


@app.post("/api/sql-engines/test-all")
def api_test_all_engines():
    """Test all registered SQL engines."""
    from agentic_core.db_engine import get_multi_engine
    return get_multi_engine().test_all()


@app.get("/api/sql-engines/{engine_key}/schema")
def api_engine_schema(engine_key: str):
    """Get schema for a specific SQL engine."""
    from agentic_core.db_engine import get_multi_engine
    multi = get_multi_engine()
    if engine_key not in multi.engines:
        return {"error": f"Engine \'{engine_key}\' not found"}
    eng = multi.get(engine_key)
    return {"engine": engine_key, "name": eng.name, "schema": eng.get_schema()}


@app.post("/api/sql-engines/query")
def api_engine_query(req: dict):
    """Execute SQL on a specific engine (admin use)."""
    from agentic_core.db_engine import get_multi_engine
    sql = req.get("sql", "")
    engine_key = req.get("engine", "")
    max_rows = req.get("max_rows", 50)
    if not sql:
        return {"error": "sql is required"}
    multi = get_multi_engine()
    eng = multi.get(engine_key if engine_key else None)
    result = eng.execute(sql, max_rows)
    result["engine"] = engine_key or multi.default_key
    result["engine_name"] = eng.name
    return result


@app.post("/api/sql-engines/add-snowflake")
def api_add_snowflake(req: dict):
    """Dynamically add/update a Snowflake engine at runtime (no restart needed)."""
    from agentic_core.db_engine import get_multi_engine, SnowflakeEngine
    multi = get_multi_engine()
    try:
        sf = SnowflakeEngine(
            account=req.get("account", ""),
            user=req.get("user", ""),
            password=req.get("password", ""),
            warehouse=req.get("warehouse", "COMPUTE_WH"),
            database=req.get("database", ""),
            schema=req.get("schema", "PUBLIC"),
            role=req.get("role", ""),
            private_key_path=req.get("private_key_path", ""),
        )
        # Test first
        test = sf.test_connection()
        if not test["ok"]:
            return {"ok": False, "message": f"Connection failed: {test['message']}"}
        # Register
        make_default = req.get("make_default", False)
        multi.register("snowflake", sf, is_default=make_default)
        return {"ok": True, "message": test["message"], "details": test["details"]}
    except Exception as e:
        return {"ok": False, "message": str(e)}

# ═══════ External MCP Server Management ═══════

_ext_mcp_servers = []
_ext_mcp_load_ts = [0]

def _load_ext_mcp():
    global _ext_mcp_servers
    if _ext_mcp_servers and (time.time() - _ext_mcp_load_ts[0]) < 30:
        return
    try:
        resp = boto3.resource("dynamodb", region_name=REGION).Table(CONFIG_TABLE).get_item(Key={"config_key": "ext_mcp_servers"})
        if "Item" in resp:
            import json as _j
            _ext_mcp_servers = _j.loads(resp["Item"].get("value", "[]"))
    except: pass
    _ext_mcp_load_ts[0] = time.time()

def _save_ext_mcp():
    try:
        import json as _j
        boto3.resource("dynamodb", region_name=REGION).Table(CONFIG_TABLE).put_item(
            Item={"config_key": "ext_mcp_servers", "value": _j.dumps(_ext_mcp_servers, ensure_ascii=False)}
        )
    except: pass

@app.get("/api/mcp/servers")
def api_list_mcp():
    """List built-in + external MCP servers."""
    _load_ext_mcp()
    # Built-in server status
    builtin = {"name": "Agentic Data (内置)", "type": "built-in", "transport": "stdio/sse", "tools": 11}
    try:
        urllib.request.urlopen("http://localhost:8520/sse", timeout=2)  # nosec B310
        builtin["status"] = "running"
        builtin["endpoint"] = "http://localhost:8520/sse"
    except:
        builtin["status"] = "stopped"
    return {"builtin": builtin, "external": _ext_mcp_servers}

@app.post("/api/mcp/servers")
def api_add_mcp(req: dict):
    """Add an external MCP server."""
    _load_ext_mcp()
    name = req.get("name", "").strip()
    transport = req.get("transport", "sse")  # sse | stdio | streamable-http
    endpoint = req.get("endpoint", "").strip()
    command = req.get("command", "").strip()
    args = req.get("args", [])
    env_vars = req.get("env", {})
    description = req.get("description", "")
    
    if not name:
        return JSONResponse({"error": "name required"}, 400)
    if transport == "stdio" and not command:
        return JSONResponse({"error": "stdio transport requires command"}, 400)
    if transport in ("sse", "streamable-http") and not endpoint:
        return JSONResponse({"error": f"{transport} transport requires endpoint"}, 400)
    
    server = {
        "name": name, "transport": transport, "description": description,
        "status": "added", "tools": [], "added_at": __import__("time").time(),
    }
    if transport == "stdio":
        server["command"] = command
        server["args"] = args
        server["env"] = env_vars
    else:
        server["endpoint"] = endpoint
    
    _ext_mcp_servers[:] = [s for s in _ext_mcp_servers if s["name"] != name]
    _ext_mcp_servers.append(server)
    _save_ext_mcp()
    return {"status": "ok", "server": name}

@app.delete("/api/mcp/servers/{name}")
def api_del_mcp(name: str):
    _load_ext_mcp()
    _ext_mcp_servers[:] = [s for s in _ext_mcp_servers if s["name"] != name]
    _save_ext_mcp()
    return {"status": "ok"}

@app.post("/api/mcp/test")
def api_test_mcp(req: dict):
    """Test connection to an MCP server (SSE/Streamable HTTP)."""
    import urllib.request, urllib.error
    transport = req.get("transport", "sse")
    endpoint = req.get("endpoint", "").strip()
    command = req.get("command", "").strip()
    
    import time as _time
    start = _time.time()
    
    try:
        if transport == "stdio":
            # Test stdio: try to run the command and see if it starts
            import subprocess
            args = req.get("args", [])
            cmd = [command] + args if args else [command]
            proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, stdin=subprocess.PIPE, env={**os.environ, **req.get("env", {})})
            # Send initialize request (JSON-RPC)
            import json as _j
            init_msg = _j.dumps({"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2024-11-05","capabilities":{},"clientInfo":{"name":"test","version":"0.1"}}})
            try:
                proc.stdin.write((init_msg + "\n").encode())
                proc.stdin.flush()
                import select
                ready, _, _ = select.select([proc.stdout], [], [], 5)
                if ready:
                    line = proc.stdout.readline().decode().strip()
                    elapsed = round((_time.time() - start) * 1000)
                    data = _j.loads(line) if line else {}
                    tools_info = data.get("result", {}).get("capabilities", {})
                    proc.kill()
                    return {"status": "ok", "latency_ms": elapsed, "message": "stdio 进程启动成功", "capabilities": tools_info}
                else:
                    proc.kill()
                    elapsed = round((_time.time() - start) * 1000)
                    return {"status": "ok", "latency_ms": elapsed, "message": "进程启动成功 (无初始化响应，可能需要 header)"}
            finally:
                try: proc.kill()
                except: pass
        
        elif transport == "sse":
            if not str(endpoint).startswith(("http://", "https://")):
                raise ValueError("URL must use http:// or https://")
            r = urllib.request.urlopen(endpoint, timeout=5)  # nosec B310
            elapsed = round((_time.time() - start) * 1000)
            ct = r.headers.get("Content-Type", "")
            return {"status": "ok", "latency_ms": elapsed, "message": f"SSE 连接成功 (Content-Type: {ct})"}
        
        elif transport == "streamable-http":
            import json as _j
            # Send initialize via HTTP POST
            init_body = _j.dumps({"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2024-11-05","capabilities":{},"clientInfo":{"name":"test","version":"0.1"}}}).encode()
            if not str(endpoint).startswith(("http://", "https://")):
                raise ValueError("URL must use http:// or https://")
            r = urllib.request.Request(endpoint, data=init_body, headers={"Content-Type":"application/json","Accept":"application/json, text/event-stream"}, method="POST")
            resp = urllib.request.urlopen(r, timeout=10)  # nosec B310
            elapsed = round((_time.time() - start) * 1000)
            body = resp.read().decode()[:300]
            return {"status": "ok", "latency_ms": elapsed, "message": "HTTP 连接成功", "response": body}
        
        else:
            return JSONResponse({"error": f"未知传输协议: {transport}"}, 400)
    
    except urllib.error.HTTPError as e:
        elapsed = round((_time.time() - start) * 1000)
        return JSONResponse({"status": "error", "latency_ms": elapsed, "message": f"HTTP {e.code}"}, 200)
    except urllib.error.URLError as e:
        return JSONResponse({"status": "error", "message": f"连接失败: {str(e.reason)[:100]}"}, 200)
    except FileNotFoundError:
        return JSONResponse({"status": "error", "message": f"命令不存在: {command}"}, 200)
    except Exception as e:
        return JSONResponse({"status": "error", "message": f"错误: {str(e)[:150]}"}, 200)

@app.post("/api/mcp/tools")
def api_list_mcp_tools(req: dict):
    """List tools from an external MCP server (SSE only for now)."""
    endpoint = req.get("endpoint", "").strip()
    if not endpoint:
        return JSONResponse({"error": "endpoint required"}, 400)
    try:
        # For SSE servers, call the tools/list endpoint
        import json as _j
        tools_url = endpoint.replace("/sse", "/tools/list") if "/sse" in endpoint else endpoint + "/tools/list"
        if not str(tools_url).startswith(("http://", "https://")):
            raise ValueError("URL must use http:// or https://")
        r = urllib.request.urlopen(tools_url, timeout=10)  # nosec B310
        data = _j.loads(r.read())
        tools = data.get("tools", data) if isinstance(data, dict) else data
        return {"status": "ok", "tools": tools}
    except Exception as e:
        return JSONResponse({"status": "error", "message": str(e)[:200]}, 200)

# ═══════ Semantic Layer ═══════

# Semantic layer customization storage
_semantic_custom = {"metrics": {}, "dimensions": {}, "synonyms": {}, "templates": {}}

_semantic_last_load = [0]  # timestamp of last DDB read

def _load_semantic_custom(force=False):
    """从 DynamoDB 重新加载语义层定义 (30s 缓存, 确保多 worker 同步)."""
    global _semantic_custom
    now = time.time()
    if not force and _semantic_last_load[0] > 0 and (now - _semantic_last_load[0]) < 30:
        return  # 30 秒内不重复读 DDB
    try:
        resp = boto3.resource("dynamodb", region_name=REGION).Table(CONFIG_TABLE).get_item(Key={"config_key": "semantic_layer"})
        if "Item" in resp:
            import json as _j
            _semantic_custom = _j.loads(resp["Item"].get("data", resp["Item"].get("value", "{}")))
            # ── Normalize list→dict (DDB may store as list of objects) ──
            for _sk in ("metrics",):
                _sv = _semantic_custom.get(_sk)
                if isinstance(_sv, list):
                    _d = {}
                    for item in _sv:
                        if not isinstance(item, dict) or "name" not in item: continue
                        n = item["name"]
                        # Ensure required fields for semantic_layer spec
                        _d[item.get("display", n)] = {
                            "id": n,
                            "description": item.get("description", item.get("display", n)),
                            "source": "chatbi",
                            "dataset": item.get("dataset", ""),
                            "metric": item.get("sql", ""),
                            "sql": item.get("sql", ""),
                            "unit": item.get("unit", ""),
                        }
                    _semantic_custom[_sk] = _d
            for _sk in ("dimensions",):
                _sv = _semantic_custom.get(_sk)
                if isinstance(_sv, list):
                    _d = {}
                    for item in _sv:
                        if not isinstance(item, dict) or "name" not in item: continue
                        n = item["name"]
                        _d[item.get("display", n)] = {
                            "id": n,
                            "description": item.get("description", item.get("display", n)),
                            "rds_column": item.get("column", n),
                            "chatbi_field": item.get("column", n),
                            "values": [],
                            "datasets": item.get("datasets", []),
                        }
                    _semantic_custom[_sk] = _d
            _sv = _semantic_custom.get("synonyms")
            if isinstance(_sv, list):
                _semantic_custom["synonyms"] = {item.get("term", item.get("alias", "")): item.get("maps_to", item.get("target", "")) for item in _sv if isinstance(item, dict)}
            # ── Merge persisted custom definitions into runtime semantic layer ──
            from agentic_core.semantic_layer import METRICS, DIMENSIONS, SYNONYMS, QUERY_TEMPLATES, template_keywords
            if _semantic_custom.get("metrics"):
                # Ensure each metric has engine field (default: athena for backward compat)
                for mname, minfo in _semantic_custom["metrics"].items():
                    if "engine" not in minfo:
                        minfo["engine"] = "athena"
                METRICS.update(_semantic_custom["metrics"])
                print(f"[SemanticLayer] Loaded {len(_semantic_custom['metrics'])} custom metrics from DynamoDB")
            if _semantic_custom.get("dimensions"):
                DIMENSIONS.update(_semantic_custom["dimensions"])
                print(f"[SemanticLayer] Loaded {len(_semantic_custom['dimensions'])} custom dimensions from DynamoDB")
            if _semantic_custom.get("synonyms"):
                SYNONYMS.update(_semantic_custom["synonyms"])
                print(f"[SemanticLayer] Loaded {len(_semantic_custom['synonyms'])} custom synonyms from DynamoDB")
            if _semantic_custom.get("templates"):
                for tname, tval in _semantic_custom["templates"].items():
                    kw = tval.pop("keywords", None) if isinstance(tval, dict) else None
                    QUERY_TEMPLATES[tname] = tval
                    if kw:
                        template_keywords[tname] = kw
                print(f"[SemanticLayer] Loaded {len(_semantic_custom['templates'])} custom templates from DynamoDB")
            if _semantic_custom.get("joins"):
                from agentic_core.semantic_layer import JOINS
                JOINS.update(_semantic_custom["joins"])
                print(f"[SemanticLayer] Loaded {len(_semantic_custom['joins'])} join mappings from DynamoDB")
    except Exception as e:
        print(f"[SemanticLayer] Failed to load custom definitions: {e}")
    _semantic_last_load[0] = time.time()

def _save_semantic_custom():
    try:
        import json as _j
        payload = _j.dumps(_semantic_custom, ensure_ascii=False)
        boto3.resource("dynamodb", region_name=REGION).Table(CONFIG_TABLE).put_item(
            Item={"config_key": "semantic_layer", "data": payload, "value": payload}
        )
        _semantic_last_load[0] = 0  # Force reload on next access
    except: pass

@app.post("/api/semantic/metric")
def api_add_metric(req: dict):
    _load_semantic_custom()
    name = req.get("name", "")
    if not name: return {"error": "name required"}
    from agentic_core.semantic_layer import METRICS
    # Add to runtime + custom storage
    METRICS[name] = {k: v for k, v in req.items() if k != "name"}
    _semantic_custom["metrics"][name] = METRICS[name]
    _save_semantic_custom()
    return {"status": "ok", "metric": name}

@app.delete("/api/semantic/metric/{name}")
def api_del_metric(name: str):
    _load_semantic_custom()
    from agentic_core.semantic_layer import METRICS
    if name in METRICS:
        del METRICS[name]
    _semantic_custom["metrics"].pop(name, None)
    _save_semantic_custom()
    return {"status": "ok"}

@app.post("/api/semantic/synonym")
def api_add_synonym(req: dict):
    _load_semantic_custom()
    alias = req.get("alias", "")
    target = req.get("target", "")
    if not alias or not target: return {"error": "alias and target required"}
    from agentic_core.semantic_layer import SYNONYMS
    SYNONYMS[alias] = target
    _semantic_custom["synonyms"][alias] = target
    _save_semantic_custom()
    return {"status": "ok"}

@app.delete("/api/semantic/synonym/{alias}")
def api_del_synonym(alias: str):
    _load_semantic_custom()
    from agentic_core.semantic_layer import SYNONYMS
    SYNONYMS.pop(alias, None)
    _semantic_custom["synonyms"].pop(alias, None)
    _save_semantic_custom()
    return {"status": "ok"}

@app.post("/api/semantic/dimension")
def api_add_dimension(req: dict):
    _load_semantic_custom()
    name = req.get("name", "")
    if not name: return {"error": "name required"}
    from agentic_core.semantic_layer import DIMENSIONS
    DIMENSIONS[name] = {k: v for k, v in req.items() if k != "name"}
    _semantic_custom.setdefault("dimensions", {})[name] = DIMENSIONS[name]
    _save_semantic_custom()
    return {"status": "ok", "dimension": name}

@app.delete("/api/semantic/dimension/{name}")
def api_del_dimension(name: str):
    _load_semantic_custom()
    from agentic_core.semantic_layer import DIMENSIONS
    DIMENSIONS.pop(name, None)
    _semantic_custom.get("dimensions", {}).pop(name, None)
    _save_semantic_custom()
    return {"status": "ok"}

@app.delete("/api/semantic/template/{name}")
def api_del_template(name: str):
    _load_semantic_custom()
    from agentic_core.semantic_layer import QUERY_TEMPLATES, template_keywords
    QUERY_TEMPLATES.pop(name, None)
    template_keywords.pop(name, None)
    _semantic_custom.get("templates", {}).pop(name, None)
    _save_semantic_custom()
    return {"status": "ok"}

@app.post("/api/semantic/reset")
def api_semantic_reset():
    """Clear all semantic layer definitions (metrics, dimensions, synonyms, templates)."""
    global _semantic_custom
    from agentic_core.semantic_layer import METRICS, DIMENSIONS, SYNONYMS, QUERY_TEMPLATES, template_keywords
    METRICS.clear()
    DIMENSIONS.clear()
    SYNONYMS.clear()
    QUERY_TEMPLATES.clear()
    template_keywords.clear()
    _semantic_custom = {"metrics": {}, "synonyms": {}, "templates": {}, "dimensions": {}}
    return {"status": "ok", "message": "语义层已清空"}

@app.post("/api/semantic/reset-to-default")
def api_semantic_reset_default():
    """Reset semantic layer to built-in defaults (reload from code)."""
    global _semantic_custom
    import importlib
    from agentic_core import semantic_layer
    importlib.reload(semantic_layer)
    _semantic_custom = {"metrics": {}, "synonyms": {}, "templates": {}, "dimensions": {}}
    _semantic_last_load[0] = 0
    return {"status": "ok", "message": "已恢复内置默认语义层"}

@app.post("/api/semantic/template")
def api_add_template(req: dict):
    _load_semantic_custom()
    name = req.get("name", "")
    if not name: return {"error": "name required"}
    from agentic_core.semantic_layer import QUERY_TEMPLATES, template_keywords
    tmpl = {k: v for k, v in req.items() if k not in ("name", "keywords")}
    QUERY_TEMPLATES[name] = tmpl
    if req.get("keywords"):
        template_keywords[name] = req["keywords"]
    _semantic_custom["templates"][name] = {**tmpl, "keywords": req.get("keywords", [])}
    return {"status": "ok"}

@app.post("/api/semantic/test-compare")
def api_semantic_compare(req: dict):
    """Compare query with and without semantic layer."""
    question = req.get("question", "")
    from agentic_core.semantic_layer import get_semantic_context, find_matching_metrics, find_matching_dimensions, find_matching_templates
    
    metrics = find_matching_metrics(question)
    dims = find_matching_dimensions(question)
    templates = find_matching_templates(question)
    context = get_semantic_context(question)
    
    # Build the "with semantic" SQL from templates
    with_semantic = []
    for name, tmpl in templates:
        if "sql" in tmpl:
            with_semantic.append({"name": name, "sql": tmpl["sql"], "source": "template"})
    
    # Build the "without semantic" hint
    without_hint = f"无语义层时，LLM 需要自行理解 '{question}' 并猜测表结构和 SQL。每次可能生成不同的查询。"
    
    return {
        "question": question,
        "metrics_matched": len(metrics),
        "dims_matched": len(dims),
        "templates_matched": len(templates),
        "with_semantic": {
            "metrics": metrics,
            "dimensions": dims,
            "ready_sql": with_semantic,
            "context_length": len(context),
            "deterministic": len(with_semantic) > 0,
        },
        "without_semantic": {
            "description": without_hint,
            "deterministic": False,
            "risk": "每次查询可能不同，结果不一致",
        }
    }


@app.get("/api/semantic")
def api_semantic_summary():
    """Get semantic layer summary stats."""
    _load_semantic_custom()
    from agentic_core.semantic_layer import METRICS, DIMENSIONS, SYNONYMS, QUERY_TEMPLATES, JOINS
    return {
        "total_metrics": len(METRICS),
        "total_dimensions": len(DIMENSIONS),
        "total_synonyms": len(SYNONYMS),
        "total_templates": len(QUERY_TEMPLATES),
        "total_joins": len(JOINS),
        "metrics": {k: {"id": v.get("id",""), "engine": v.get("engine","athena"), "datasource": v.get("datasource",""), "description": v.get("description","")} for k,v in METRICS.items()},
        "dimensions": {k: {"id": v.get("id",""), "engine": v.get("engine",""), "datasource": v.get("datasource","")} for k,v in DIMENSIONS.items()},
        "synonyms": dict(SYNONYMS),
        "joins": {k: {"left": v.get("left",""), "right": v.get("right",""), "on": v.get("on",""), "type": v.get("type",""), "engine": v.get("engine",""), "description": v.get("description","")} for k,v in JOINS.items()},
    }

@app.get("/api/semantic/stats")
def api_semantic_stats():
    """Semantic layer hit rate stats."""
    _load_semantic_custom()
    from agentic_core.semantic_layer import METRICS, DIMENSIONS, SYNONYMS, QUERY_TEMPLATES
    return {
        "total_metrics": len(METRICS),
        "total_dimensions": len(DIMENSIONS),
        "total_synonyms": len(SYNONYMS),
        "total_templates": len(QUERY_TEMPLATES),
    }

@app.get("/api/semantic/coverage")
def api_semantic_coverage():
    """Semantic layer coverage stats from recent queries."""
    _load_semantic_custom()
    from agentic_core.semantic_layer import METRICS, DIMENSIONS, SYNONYMS, QUERY_TEMPLATES
    hits = {"metric_hits": {}, "template_hits": {}, "miss_questions": []}
    total_q = 0
    covered = 0
    try:
        resp = boto3.resource("dynamodb", region_name=REGION).Table(CHAT_TABLE).scan(Limit=200)
        questions = [i["content"] for i in resp.get("Items", []) if i.get("role") == "user" and i.get("content")]
        from agentic_core.semantic_layer import find_matching_metrics, find_matching_templates
        for q in questions:
            total_q += 1
            ms = find_matching_metrics(q)
            ts = find_matching_templates(q)
            if ms or ts:
                covered += 1
                for m in ms:
                    hits["metric_hits"][m] = hits["metric_hits"].get(m, 0) + 1
                for name, _ in ts:
                    hits["template_hits"][name] = hits["template_hits"].get(name, 0) + 1
            else:
                hits["miss_questions"].append(q[:80])
    except: pass
    
    return {
        "total_questions": total_q,
        "covered": covered,
        "coverage_rate": round(covered / total_q * 100, 1) if total_q > 0 else 0,
        "metric_hits": dict(sorted(hits["metric_hits"].items(), key=lambda x: -x[1])),
        "template_hits": dict(sorted(hits["template_hits"].items(), key=lambda x: -x[1])),
        "miss_questions": hits["miss_questions"][:10],
        "total_metrics": len(METRICS),
        "total_templates": len(QUERY_TEMPLATES),
        "total_synonyms": len(SYNONYMS),
    }

@app.get("/api/semantic/spec")
def api_semantic_spec():
    _load_semantic_custom()
    from agentic_core.semantic_layer import get_full_semantic_spec, METRICS, DIMENSIONS, SYNONYMS, QUERY_TEMPLATES
    return {
        "spec": get_full_semantic_spec(),
        "stats": {
            "metrics": len(METRICS),
            "dimensions": len(DIMENSIONS),
            "synonyms": len(SYNONYMS),
            "templates": len(QUERY_TEMPLATES),
        },
        "structured_metrics": {k: v for k, v in METRICS.items()},
        "structured_dimensions": {k: v for k, v in DIMENSIONS.items()},
        "structured_synonyms": {k: v for k, v in SYNONYMS.items()},
        "structured_templates": {k: v for k, v in QUERY_TEMPLATES.items()},
    }

@app.get("/api/semantic/parse")
def api_semantic_parse(question: str):
    from agentic_core.semantic_layer import get_semantic_context, find_matching_metrics, find_matching_dimensions, find_matching_templates
    return {
        "question": question,
        "metrics": find_matching_metrics(question),
        "dimensions": find_matching_dimensions(question),
        "templates": [{"name": n, "desc": t["description"]} for n, t in find_matching_templates(question)],
        "context": get_semantic_context(question),
    }

# ═══════ Feedback (👍👎) ═══════
_feedback_ddb = None
def _get_feedback_table():
    global _feedback_ddb
    if _feedback_ddb is None:
        _feedback_ddb = boto3.resource("dynamodb", region_name=REGION).Table(FEEDBACK_TABLE)
    return _feedback_ddb


# SQL cache is in agentic_core.sql_cache module

def _enqueue_vqr_candidate(session_id: str, question: str, rating: str = "up"):
    """Extract SQL from chat history or SQL cache and enqueue as VQR candidate."""
    import re
    if not session_id or not question:
        return
    
    # Load chat history for this session
    try:
        chat_table = boto3.resource("dynamodb", region_name=REGION).Table(CHAT_TABLE)
        resp = chat_table.query(
            KeyConditionExpression=boto3.dynamodb.conditions.Key("session_id").eq(session_id),
            ScanIndexForward=False,
            Limit=10,
        )
        messages = resp.get("Items", [])
    except Exception:
        return
    
    # Find SQL in assistant messages (check meta first, then content)
    sql = None
    engine = "athena"
    for msg in messages:
        if msg.get("role") != "assistant":
            continue
        
        # Priority 1: check meta.sql (set during SSE streaming)
        meta_str = msg.get("meta", "")
        if meta_str:
            try:
                meta_obj = json.loads(meta_str) if isinstance(meta_str, str) else meta_str
                if meta_obj.get("sql"):
                    sql = meta_obj["sql"]
                    engine = meta_obj.get("sql_engine", "athena")
                    break
            except Exception:
                pass
        
        # Priority 2: extract SQL from content
        content = msg.get("content", "")
        
        # Extract SQL from markdown code blocks
        sql_matches = re.findall(r'```sql\n([\s\S]*?)```', content)
        if sql_matches:
            sql = sql_matches[0].strip()
            # Detect engine from SQL
            detected = _detect_sql_engine(sql)
            if detected in ("postgresql", "mysql"):
                engine = detected
            break
        
        # Also try to find SQL from tool results (semantic_query returns ready SQL)
        sql_inline = re.findall(r'(?:SELECT|WITH)\s+[\s\S]{20,}?(?:LIMIT\s+\d+|;)', content, re.IGNORECASE)
        if sql_inline:
            sql = sql_inline[0].strip().rstrip(';')
            detected = _detect_sql_engine(sql)
            if detected in ("postgresql", "mysql"):
                engine = detected
            break
    
    # Fallback 1: check in-memory SQL cache
    if not sql or len(sql) < 20:
        try:
            from agentic_core.sql_cache import get_sql
            cached = get_sql(session_id)
            if cached:
                sql = cached.get("sql", "")
                engine = cached.get("engine", "athena")
        except Exception:
            pass
    
    # Fallback 2: scan cost table for recent traces with this session_id
    if not sql or len(sql) < 20:
        try:
            import datetime
            today = datetime.datetime.utcnow().strftime("%Y-%m-%d")
            cost_table = boto3.resource("dynamodb", region_name=REGION).Table(COST_TABLE)
            cost_resp = cost_table.query(
                KeyConditionExpression=boto3.dynamodb.conditions.Key("date").eq(today),
                ScanIndexForward=False, Limit=20,
            )
            for ci in cost_resp.get("Items", []):
                if ci.get("session_id") == session_id and ci.get("sql"):
                    sql = ci["sql"]
                    engine = _detect_sql_engine(sql)
                    break
        except Exception:
            pass
    
    if not sql or len(sql) < 20:
        return
    
    # Determine datasource
    datasource = ""
    _detected_ds = _detect_sql_datasource(sql)
    if _detected_ds:
        datasource = _detected_ds
    
    from agentic_core.vqr import add_candidate
    add_candidate(
        question=question,
        sql=sql,
        engine=engine,
        datasource=datasource,
        session_id=session_id,
        rating=rating,
        run_judge=(rating == "up"),  # Only run judge for positive feedback
    )
    print(f"[VQR] Candidate enqueued from feedback ({rating}): {question[:50]}")

@app.post("/api/feedback")
def api_feedback(req: dict, request: Request):
    user = get_current_user(request)
    import time as _t
    item = {
        "session_id": req.get("session_id", "default"),
        "timestamp": str(_t.time()),
        "user_id": user["user_id"],
        "username": user["username"],
        "message_index": req.get("message_index", 0),
        "rating": req.get("rating", "up"),  # "up" or "down"
        "comment": req.get("comment", ""),
        "question": req.get("question", ""),
        "answer_preview": req.get("answer_preview", "")[:500],
    }
    try:
        _get_feedback_table().put_item(Item=item)
        
        # VQR: 👍 feedback → auto-enqueue as VQR candidate
        if req.get("rating") == "up" and req.get("question"):
            try:
                _enqueue_vqr_candidate(req.get("session_id", ""), req.get("question", ""))
            except Exception as vqr_e:
                print(f"[VQR] Candidate enqueue error: {vqr_e}")
        
        # VQR: 👎 feedback → record as rejected pattern (negative signal)
        if req.get("rating") == "down" and req.get("question"):
            try:
                _enqueue_vqr_candidate(req.get("session_id", ""), req.get("question", ""), rating="down")
            except Exception as vqr_e:
                print(f"[VQR] Negative candidate enqueue error: {vqr_e}")
        
        return {"status": "ok"}
    except Exception as e:
        return {"status": "error", "message": str(e)}

@app.get("/api/feedback/stats")
def api_feedback_stats(request: Request):
    user = get_current_user(request)
    if user.get("role") != "admin":
        return {"error": "需要管理员权限"}
    try:
        resp = _get_feedback_table().scan(Limit=500)
        items = resp.get("Items", [])
        up = sum(1 for i in items if i.get("rating") == "up")
        down = sum(1 for i in items if i.get("rating") == "down")
        return {"total": len(items), "up": up, "down": down, "rate": round(up/(up+down)*100,1) if up+down>0 else 0, "recent": sorted(items, key=lambda x: x.get("timestamp",""), reverse=True)[:20]}
    except Exception as e:
        return {"total": 0, "up": 0, "down": 0, "error": str(e)}


# ───────────────── VQR (Verified Query Repository) API ─────────────────

@app.get("/api/vqr/stats")
def api_vqr_stats(request: Request):
    user = get_current_user(request)
    try:
        from agentic_core.vqr import get_stats
        return get_stats()
    except Exception as e:
        return {"error": str(e)}

@app.get("/api/vqr/verified")
def api_vqr_verified(request: Request):
    user = get_current_user(request)
    if user.get("role") != "admin":
        return {"error": "需要管理员权限"}
    try:
        from agentic_core.vqr import get_verified_queries
        return {"queries": get_verified_queries()}
    except Exception as e:
        return {"error": str(e)}

@app.get("/api/vqr/candidates")
def api_vqr_candidates(request: Request, status: str = "pending"):
    user = get_current_user(request)
    if user.get("role") != "admin":
        return {"error": "需要管理员权限"}
    try:
        from agentic_core.vqr import get_candidates
        return {"candidates": get_candidates(status=status)}
    except Exception as e:
        return {"error": str(e)}

@app.post("/api/vqr/verify/{candidate_id}")
def api_vqr_verify(candidate_id: str, req: dict, request: Request):
    user = get_current_user(request)
    if user.get("role") != "admin":
        return {"error": "需要管理员权限"}
    try:
        from agentic_core.vqr import verify_candidate, _load_candidates, _verify_sql_execution
        # If validate=true, run SQL execution check before approving
        if req.get("validate"):
            candidates = _load_candidates()
            cand = candidates.get(candidate_id)
            if cand:
                sql_to_check = req.get("sql") or cand.get("sql", "")
                engine = cand.get("engine", "athena")
                exec_result = _verify_sql_execution(sql_to_check, engine)
                if not exec_result.get("executable"):
                    err_msg = exec_result.get("error", "未知错误")[:150]
                    return {"error": f"SQL 执行验证失败: {err_msg}"}
        vq_id = verify_candidate(
            candidate_id,
            verified_by=user.get("username", "admin"),
            question=req.get("question"),
            sql=req.get("sql"),
            keywords=req.get("keywords"),
            variants=req.get("variants"),
        )
        return {"status": "ok", "vqr_id": vq_id} if vq_id else {"error": "候选不存在"}
    except Exception as e:
        return {"error": str(e)}

@app.post("/api/vqr/reject/{candidate_id}")
def api_vqr_reject(candidate_id: str, req: dict, request: Request):
    user = get_current_user(request)
    if user.get("role") != "admin":
        return {"error": "需要管理员权限"}
    try:
        from agentic_core.vqr import reject_candidate
        reject_candidate(candidate_id, reason=req.get("reason", ""))
        return {"status": "ok"}
    except Exception as e:
        return {"error": str(e)}

@app.post("/api/vqr/add")
def api_vqr_add(req: dict, request: Request):
    user = get_current_user(request)
    if user.get("role") != "admin":
        return {"error": "需要管理员权限"}
    try:
        from agentic_core.vqr import add_verified_query
        vq_id = add_verified_query(
            question=req["question"],
            sql=req["sql"],
            engine=req.get("engine", "athena"),
            datasource=req.get("datasource", ""),
            keywords=req.get("keywords"),
            variants=req.get("variants"),
            verified_by=user.get("username", "admin"),
        )
        return {"status": "ok", "vqr_id": vq_id}
    except Exception as e:
        return {"error": str(e)}

@app.put("/api/vqr/{vqr_id}")
def api_vqr_update(vqr_id: str, req: dict, request: Request):
    user = get_current_user(request)
    if user.get("role") != "admin":
        return {"error": "需要管理员权限"}
    try:
        from agentic_core.vqr import update_verified_query
        ok = update_verified_query(vqr_id, req)
        return {"status": "ok"} if ok else {"error": "VQR 不存在"}
    except Exception as e:
        return {"error": str(e)}

@app.delete("/api/vqr/{vqr_id}")
def api_vqr_delete(vqr_id: str, request: Request):
    user = get_current_user(request)
    if user.get("role") != "admin":
        return {"error": "需要管理员权限"}
    try:
        from agentic_core.vqr import delete_verified_query
        ok = delete_verified_query(vqr_id)
        return {"status": "ok"} if ok else {"error": "VQR 不存在"}
    except Exception as e:
        return {"error": str(e)}

@app.post("/api/vqr/judge/{candidate_id}")
def api_vqr_judge(candidate_id: str, request: Request):
    """Manually trigger LLM judge for a candidate."""
    user = get_current_user(request)
    if user.get("role") != "admin":
        return {"error": "需要管理员权限"}
    try:
        from agentic_core.vqr import _load_candidates, _save_candidates, llm_judge
        candidates = _load_candidates()
        cand = candidates.get(candidate_id)
        if not cand:
            return {"error": "候选不存在"}
        judge_result = llm_judge(cand["question"], cand["sql"], cand.get("engine", "athena"), run_sql=True)
        cand["judge"] = judge_result
        cand["auto_score"] = judge_result.get("overall", cand.get("auto_score", 0))
        _save_candidates(candidates)
        return {"status": "ok", "judge": judge_result}
    except Exception as e:
        return {"error": str(e)}

# ───────────────── Auth & User Management API ─────────────────

class LoginRequest(BaseModel):
    email: str
    password: str

class SignupRequest(BaseModel):
    email: str
    password: str
    role: str = "viewer"

class ChangePasswordRequest(BaseModel):
    old_password: str
    new_password: str

class UpdateRoleRequest(BaseModel):
    role: str

class ResetPasswordRequest(BaseModel):
    new_password: str

@app.post("/api/auth/login")
def api_login(req: LoginRequest):
    if not AUTH_ENABLED:
        # Demo mode — return fake token
        import base64, json as _json, time as _time
        payload = {"sub": "demo", "email": req.email, "custom:role": "admin", "exp": int(_time.time()) + 86400}
        fake_token = "demo." + base64.urlsafe_b64encode(_json.dumps(payload).encode()).decode().rstrip("=") + ".sig"
        return {"ok": True, "access_token": fake_token, "id_token": fake_token, "refresh_token": "", "expires_in": 86400, "user": {"email": req.email, "role": "admin"}}
    _provider = os.environ.get("AGENTIC_AUTO_AUTH_PROVIDER", "cognito")
    if _provider in ("local", "authing"):
        from agentic_core.local_auth import local_login
        result = local_login(req.email, req.password)
        if "error" in result:
            return JSONResponse({"ok": False, "error": result["error"]}, 401)
        return {"ok": True, "access_token": result["token"], "id_token": result["token"], "refresh_token": "", "expires_in": result["expires_in"], "user": {"email": result["email"], "role": result["role"]}}
    # Try Cognito first
    try:
        from agentic_core.cognito_auth import login
        result = login(req.email, req.password)
        if result.get("ok"):
            payload = _decode_jwt_payload(result["id_token"])
            result["user"] = {
                "email": payload.get("email", req.email),
                "role": payload.get("custom:role", "viewer"),
                "username": payload.get("cognito:username", req.email),
            }
            return result
    except Exception as _e:

        print(f"[WARN] swallowed exception: {_e}")
    # Fallback to local login (admin / built-in users)
    from agentic_core.local_auth import local_login
    result = local_login(req.email, req.password)
    if "error" in result:
        return JSONResponse({"ok": False, "error": result["error"]}, 401)
    return {"ok": True, "access_token": result["token"], "id_token": result["token"], "refresh_token": "", "expires_in": result["expires_in"], "user": {"email": result["email"], "role": result["role"]}}

@app.post("/api/auth/local-login")
def api_local_login(req: LoginRequest):
    """Login via local JWT auth (China region, no Cognito)."""
    from agentic_core.local_auth import local_login
    result = local_login(req.email, req.password)
    if "error" in result:
        return JSONResponse({"ok": False, "error": result["error"]}, 401)
    return {
        "ok": True,
        "access_token": result["token"],
        "id_token": result["token"],
        "refresh_token": "",
        "expires_in": result["expires_in"],
        "user": {"email": result["email"], "role": result["role"]},
    }


@app.get("/api/auth/config")
def api_auth_config():
    """Return auth configuration for frontend — routes by AUTH_PROVIDER."""
    if not AUTH_ENABLED:
        return {"hosted_ui": False}
    provider = os.environ.get("AGENTIC_AUTO_AUTH_PROVIDER", "cognito")
    if provider == "authing":
        from agentic_core.authing_auth import get_auth_config
        return get_auth_config()
    if provider == "local":
        return {"hosted_ui": False, "provider": "local"}
    return {
        "hosted_ui": True,
        "domain": COGNITO_DOMAIN,
        "client_id": COGNITO_CLIENT_ID,
        "redirect_uri": COGNITO_REDIRECT_URI,
        "logout_uri": COGNITO_LOGOUT_URI,
        "scopes": "openid email profile",
        "provider": "cognito",
    }

@app.post("/api/auth/callback")
def api_auth_callback(body: dict):
    """Exchange authorization code for tokens — routes by AUTH_PROVIDER."""
    code = body.get("code", "")
    if not code:
        raise HTTPException(status_code=400, detail="Missing authorization code")
    provider = os.environ.get("AGENTIC_AUTO_AUTH_PROVIDER", "cognito")
    if provider == "authing":
        from agentic_core.authing_auth import exchange_code
        result = exchange_code(code)
        if not result.get("ok"):
            raise HTTPException(status_code=400, detail=result.get("error", "Token exchange failed"))
        return result
    import urllib.request, urllib.parse
    token_url = f"https://{COGNITO_DOMAIN}/oauth2/token"
    data = urllib.parse.urlencode({
        "grant_type": "authorization_code",
        "client_id": COGNITO_CLIENT_ID,
        "code": code,
        "redirect_uri": COGNITO_REDIRECT_URI,
    }).encode()
    req = urllib.request.Request(token_url, data=data, headers={"Content-Type": "application/x-www-form-urlencoded"})
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:  # nosec B310
            result = json.loads(resp.read())
        id_token = result.get("id_token", "")
        access_token = result.get("access_token", "")
        refresh_token = result.get("refresh_token", "")
        payload = _decode_jwt_payload(id_token)
        return {
            "ok": True,
            "access_token": access_token,
            "id_token": id_token,
            "refresh_token": refresh_token,
            "expires_in": result.get("expires_in", 3600),
            "user": {
                "email": payload.get("email", ""),
                "role": payload.get("custom:role", "viewer"),
                "username": payload.get("cognito:username", payload.get("email", "")),
            }
        }
    except urllib.error.HTTPError as e:
        error_body = e.read().decode() if e.fp else str(e)
        logger.error(f"Token exchange failed: {error_body}")
        raise HTTPException(status_code=400, detail=f"Token exchange failed: {error_body}")
    except Exception as e:
        logger.error(f"Token exchange error: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/api/auth/refresh")
def api_refresh(request: Request):
    refresh_tok = request.headers.get("x-refresh-token", "")
    if not refresh_tok:
        raise HTTPException(status_code=400, detail="Missing refresh token")
    if not AUTH_ENABLED:
        return {"ok": True, "access_token": "demo-refreshed", "id_token": "demo-refreshed"}
    provider = os.environ.get("AGENTIC_AUTO_AUTH_PROVIDER", "cognito")
    if provider == "authing":
        from agentic_core.authing_auth import refresh_tokens
        return refresh_tokens(refresh_tok)
    from agentic_core.cognito_auth import refresh_token
    return refresh_token(refresh_tok)

@app.post("/api/auth/change-password")
def api_change_password(req: ChangePasswordRequest, request: Request):
    if not AUTH_ENABLED:
        return {"ok": True}
    token = request.headers.get("authorization", "").replace("Bearer ", "")
    from agentic_core.cognito_auth import change_password
    return change_password(token, req.old_password, req.new_password)

# ── User Management (Admin only) ──

@app.get("/api/users")
def api_list_users(request: Request):
    user = get_current_user(request)
    if user.get("role") != "admin":
        raise HTTPException(status_code=403, detail="仅管理员可访问")
    if not AUTH_ENABLED:
        return {"users": [
            {"username": "admin@agentic-data.com", "email": "admin@agentic-data.com", "role": "admin", "status": "CONFIRMED", "enabled": True},
            {"username": "analyst@agentic-data.com", "email": "analyst@agentic-data.com", "role": "analyst", "status": "CONFIRMED", "enabled": True},
            {"username": "viewer@agentic-data.com", "email": "viewer@agentic-data.com", "role": "viewer", "status": "CONFIRMED", "enabled": True},
        ]}
    from agentic_core.cognito_auth import list_users
    return {"users": list_users()}

@app.post("/api/users")
def api_create_user(req: SignupRequest, request: Request):
    user = get_current_user(request)
    if user.get("role") != "admin":
        raise HTTPException(status_code=403, detail="仅管理员可创建用户")
    if not AUTH_ENABLED:
        return {"ok": True}
    from agentic_core.cognito_auth import create_user
    return create_user(req.email, req.password, req.role)

@app.put("/api/users/{email}/role")
def api_update_user_role(email: str, req: UpdateRoleRequest, request: Request):
    user = get_current_user(request)
    if user.get("role") != "admin":
        raise HTTPException(status_code=403, detail="仅管理员可修改角色")
    if not AUTH_ENABLED:
        return {"ok": True}
    from agentic_core.cognito_auth import update_user_role
    return update_user_role(email, req.role)

@app.put("/api/users/{email}/toggle")
def api_toggle_user(email: str, request: Request):
    user = get_current_user(request)
    if user.get("role") != "admin":
        raise HTTPException(status_code=403, detail="仅管理员可操作")
    if not AUTH_ENABLED:
        return {"ok": True}
    from agentic_core.cognito_auth import list_users, disable_user, enable_user
    users = list_users()
    target = next((u for u in users if u["email"] == email), None)
    if not target:
        raise HTTPException(status_code=404, detail="用户不存在")
    if target["enabled"]:
        return disable_user(email)
    else:
        return enable_user(email)

@app.delete("/api/users/{email}")
def api_delete_user(email: str, request: Request):
    user = get_current_user(request)
    if user.get("role") != "admin":
        raise HTTPException(status_code=403, detail="仅管理员可删除用户")
    if email == user.get("email"):
        raise HTTPException(status_code=400, detail="不能删除自己")
    if not AUTH_ENABLED:
        return {"ok": True}
    from agentic_core.cognito_auth import delete_user
    return delete_user(email)

@app.post("/api/users/{email}/reset-password")
def api_reset_password(email: str, req: ResetPasswordRequest, request: Request):
    user = get_current_user(request)
    if user.get("role") != "admin":
        raise HTTPException(status_code=403, detail="仅管理员可重置密码")
    if not AUTH_ENABLED:
        return {"ok": True}
    from agentic_core.cognito_auth import reset_user_password
    return reset_user_password(email, req.new_password)


@app.get("/api/user/me")
def api_user_me(request: Request):
    return get_current_user(request)

@app.get("/api/user/memory")
def api_get_memory(request: Request):
    """Get current user's memory."""
    user = get_current_user(request)
    email = user.get("email", "")
    memory = load_user_memory(email)
    return {"email": email, "memory": memory}

@app.put("/api/user/memory")
def api_update_memory(request: Request, body: dict):
    """Update current user's memory (admin or self)."""
    user = get_current_user(request)
    email = user.get("email", "")
    memory = body.get("memory", {})
    save_user_memory(email, memory)
    return {"ok": True}

@app.delete("/api/user/memory")
def api_clear_memory(request: Request):
    """Clear current user's memory."""
    user = get_current_user(request)
    email = user.get("email", "")
    save_user_memory(email, {"preferences": [], "context": [], "topics": []})
    return {"ok": True}

@app.get("/api/user/dashboards")
def api_list_dashboards(request: Request):
    """List saved dashboards for current user."""
    user = get_current_user(request)
    email = user.get("email", "")
    import logging
    logging.warning(f"[DASHBOARD-LIST] user={user}, email='{email}', filter='dashboard:{email}:'")
    try:
        resp = _ddb.Table(CONFIG_TABLE).scan(
            FilterExpression=Attr("config_key").begins_with(f"dashboard:{email}:")
        )
        dashboards = []
        for item in resp.get("Items", []):
            db = item.get("data", {})
            db["id"] = item["config_key"]
            db["saved_at"] = item.get("saved_at", "")
            dashboards.append(db)
        dashboards.sort(key=lambda d: d.get("saved_at", ""), reverse=True)
        # Convert Decimal back to float for JSON serialization
        import json as _json
        from decimal import Decimal
        def _dec_default(obj):
            if isinstance(obj, Decimal):
                return float(obj)
            raise TypeError
        clean = _json.loads(_json.dumps({"dashboards": dashboards}, default=_dec_default))
        return clean
    except Exception as e:
        return {"dashboards": [], "error": str(e)}

@app.post("/api/user/dashboards")
def api_save_dashboard(request: Request, body: dict):
    """Save a dashboard for current user."""
    user = get_current_user(request)
    email = user.get("email", "")
    import uuid, json as _json
    from decimal import Decimal
    db_id = f"dashboard:{email}:{uuid.uuid4().hex[:8]}"
    # DynamoDB doesn't support float — convert via JSON string -> Decimal
    safe_body = _json.loads(_json.dumps(body), parse_float=Decimal)
    try:
        _ddb.Table(CONFIG_TABLE).put_item(Item={
            "config_key": db_id,
            "data": safe_body,
            "saved_at": datetime.now(timezone.utc).isoformat(),
        })
        return {"ok": True, "id": db_id}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.delete("/api/user/dashboards/{dashboard_id:path}")
def api_delete_dashboard(dashboard_id: str, request: Request):
    """Delete a saved dashboard."""
    user = get_current_user(request)
    email = user.get("email", "")
    if not dashboard_id.startswith(f"dashboard:{email}:"):
        raise HTTPException(status_code=403, detail="无权删除")
    try:
        _ddb.Table(CONFIG_TABLE).delete_item(Key={"config_key": dashboard_id})
        return {"ok": True}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/api/admin/memories")
def api_admin_memories(request: Request):
    """Admin: list all users' memories."""
    user = get_current_user(request)
    if user.get("role") != "admin":
        raise HTTPException(status_code=403, detail="仅管理员可访问")
    try:
        resp = _ddb.Table(CONFIG_TABLE).scan(
            FilterExpression=Attr("config_key").begins_with("user_memory:")
        )
        memories = {}
        for item in resp.get("Items", []):
            email = item["config_key"].replace("user_memory:", "")
            memories[email] = item.get("data", {})
        return {"memories": memories}
    except Exception as e:
        return {"memories": {}, "error": str(e)}


# ─── Data Upload + Auto Schema Inference ─────────────────────

from fastapi import UploadFile, File, Form

@app.post("/api/data/upload")
async def api_upload_data(
    file: UploadFile = File(...),
    dataset_name: str = Form(""),
    description: str = Form(""),
):
    """
    上传数据文件, 自动推断 Schema → 生成语义层 + 注册数据源。
    
    支持: JSON (array of objects), CSV, Excel (.xlsx)
    返回推断结果供用户确认/编辑后应用。
    """
    from agentic_core.schema_inference import parse_upload, infer_schema
    
    # Validate file
    if not file.filename:
        return JSONResponse({"error": "未选择文件"}, 400)
    
    MAX_SIZE = 50 * 1024 * 1024  # 50MB
    content = await file.read()
    if len(content) > MAX_SIZE:
        return JSONResponse({"error": f"文件过大: {len(content)/(1024*1024):.1f}MB, 上限 50MB"}, 400)
    
    # Generate dataset name from filename if not provided
    if not dataset_name:
        base = os.path.splitext(file.filename)[0]
        dataset_name = re.sub(r'[^a-zA-Z0-9_\u4e00-\u9fff]', '_', base).lower().strip('_')
    
    # Parse file
    data, error = parse_upload(content, file.filename)
    if error:
        return JSONResponse({"error": error}, 400)
    
    # Run schema inference
    result = infer_schema(data, dataset_name, description)
    
    # Store parsed data temporarily for apply step
    _upload_staging[dataset_name] = {
        "data": data,
        "inference": result,
        "filename": file.filename,
        "uploaded_at": time.time(),
        "size_bytes": len(content),
    }
    
    return {
        "status": "ok",
        "dataset_name": dataset_name,
        "filename": file.filename,
        "record_count": len(data),
        "inference": result,
    }

import re
_upload_staging = {}  # Temporary storage for uploaded data pending confirmation


@app.post("/api/data/apply")
def api_apply_upload(req: dict):
    """
    确认并应用上传数据的推断结果。
    
    用户可以在前端编辑后提交:
    - dataset_name: 数据集名称
    - description: 描述
    - metrics: 确认的指标 (可编辑)
    - dimensions: 确认的维度 (可编辑)
    - synonyms: 确认的同义词 (可编辑)
    - auto_register: 是否自动注册为数据源 (default true)
    """
    dataset_name = req.get("dataset_name", "")
    if not dataset_name:
        return JSONResponse({"error": "dataset_name required"}, 400)
    
    staging = _upload_staging.get(dataset_name)
    if not staging:
        return JSONResponse({"error": f"未找到待确认的数据集 '{dataset_name}', 请先上传"}, 400)
    
    data = staging["data"]
    inference = staging["inference"]
    
    # 1. Upload data to S3
    s3_key = f"chatbi/{dataset_name}.json"
    try:
        _s3.put_object(
            Bucket=DATA_BUCKET,
            Key=s3_key,
            Body=json.dumps(data, ensure_ascii=False, default=str),
            ContentType="application/json"
        )
    except Exception as e:
        return JSONResponse({"error": f"S3 上传失败: {e}"}, 500)
    
    # 2. Register as ChatBI dataset
    from agentic_core.tools import CHATBI_DATASETS; _chatbi_cache = {}
    desc = req.get("description", inference["dataset"]["desc"])
    CHATBI_DATASETS[dataset_name] = {
        "key": s3_key,
        "desc": desc,
    }
    # Clear cache so next query loads fresh data
    _chatbi_cache.pop(dataset_name, None)
    
    # 3. Apply metrics to semantic layer (conflict-safe: prefix if name exists from different dataset)
    metrics = req.get("metrics", inference.get("metrics", {}))
    if metrics:
        from agentic_core.semantic_layer import METRICS
        _load_semantic_custom()
        for name, defn in metrics.items():
            final_name = name
            # If metric name already exists from a DIFFERENT dataset, add prefix to avoid overwrite
            if name in METRICS and METRICS[name].get("dataset") != dataset_name:
                final_name = f"{name}({dataset_name})"
            defn["dataset"] = dataset_name
            METRICS[final_name] = defn
            _semantic_custom["metrics"][final_name] = defn
    
    # 4. Apply dimensions (with dataset tag for cleanup on delete)
    dimensions = req.get("dimensions", inference.get("dimensions", {}))
    if dimensions:
        from agentic_core.semantic_layer import DIMENSIONS
        _load_semantic_custom()
        if "dimensions" not in _semantic_custom:
            _semantic_custom["dimensions"] = {}
        for name, defn in dimensions.items():
            defn["dataset"] = dataset_name  # tag for delete cleanup
            DIMENSIONS[name] = defn
            _semantic_custom["dimensions"][name] = defn
    
    # 5. Apply synonyms (tag with dataset for safe deletion)
    synonyms = req.get("synonyms", inference.get("synonyms", {}))
    if synonyms:
        from agentic_core.semantic_layer import SYNONYMS
        _load_semantic_custom()
        if "synonym_sources" not in _semantic_custom:
            _semantic_custom["synonym_sources"] = {}
        for alias, target in synonyms.items():
            SYNONYMS[alias] = target
            _semantic_custom["synonyms"][alias] = target
            _semantic_custom["synonym_sources"][alias] = dataset_name  # track origin
    
    # 6. Register as custom data source
    if req.get("auto_register", True):
        ds = {
            "id": f"chatbi_{dataset_name}",
            "name": dataset_name,
            "type": "S3-JSON",
            "icon": "analytics",
            "desc": desc,
            "record_count": len(data),
            "field_count": inference["dataset"]["field_count"],
            "s3_key": s3_key,
            "status": "connected",
            "auto_generated": True,
            "custom": True,
        }
        _custom_data_sources.append(ds)
        _save_config("custom_datasources", _custom_data_sources)
    try:
        from agentic_core.tools import invalidate_pg_pool
        invalidate_pg_pool()
    except: pass
    
    # 7. Persist dataset registry
    _save_config("custom_chatbi_datasets", {
        k: v for k, v in CHATBI_DATASETS.items()
        if k not in ("vehicle_master", "app_usage", "service_records",
                     "driving_daily", "battery_health", "ota_records",
                     "customer_feedback", "charging_records")
    })
    
    # 8. Force recreate agents (new tool descriptions)
    _agents.clear()
    
    # 9. Clean staging
    del _upload_staging[dataset_name]
    
    return {
        "status": "ok",
        "dataset_name": dataset_name,
        "s3_key": s3_key,
        "records_uploaded": len(data),
        "metrics_added": len(metrics),
        "dimensions_added": len(dimensions),
        "synonyms_added": len(synonyms),
        "message": f"✅ 数据集 {dataset_name} 已注册, {len(metrics)}个指标 + {len(dimensions)}个维度已添加到语义层",
    }


@app.delete("/api/data/staging/{dataset_name}")
def api_cancel_upload(dataset_name: str):
    """Cancel a pending upload."""
    _upload_staging.pop(dataset_name, None)
    return {"status": "ok"}


@app.get("/api/data/staging")
def api_list_staging():
    """List pending uploads."""
    return {"staging": {k: {
        "filename": v["filename"],
        "record_count": len(v["data"]),
        "uploaded_at": v["uploaded_at"],
        "size_bytes": v["size_bytes"],
    } for k, v in _upload_staging.items()}}


# ─── Auto-Discovery: Connect → Introspect → Semantic Layer ──

@app.post("/api/datasources/connect")
def api_connect_datasource(req: dict):
    """
    一键连接数据源: 测试连接 → 自动探查 Schema → 推断语义层 → 返回预览。
    
    替代原来的 test + add 两步流程:
    POST /api/datasources/connect {type, name, config, description}
    → 成功返回 {ok, source, introspection: [{dataset, fields, metrics, dimensions, ...}]}
    → 用户确认后 POST /api/datasources/connect/apply 应用语义层
    """
    ds_type = req.get("type", "")
    config = req.get("config", {})
    name = req.get("name", ds_type)
    description = req.get("description", "")
    
    from agentic_core.schema_inference import (
        introspect_dynamodb, introspect_s3_json, introspect_s3_prefix,
        introspect_athena, introspect_sql_engine
    )
    
    introspection = []
    test_msg = ""
    
    try:
        if ds_type == "DynamoDB":
            table_name = config.get("table", "")
            region = config.get("region", REGION)
            result = introspect_dynamodb(table_name, region, sample_limit=200)
            if "error" in result:
                return {"ok": False, "message": result["error"]}
            introspection = [result]
            test_msg = f"DynamoDB 表 {table_name} — {result['dataset']['record_count']} 条记录采样分析完成"
            
        elif ds_type == "S3":
            bucket = config.get("bucket", "")
            prefix = config.get("prefix", "")
            region = config.get("region", REGION)
            key = config.get("key", "")  # 单文件模式
            if key:
                result = introspect_s3_json(bucket, key, region)
                if "error" in result:
                    return {"ok": False, "message": result["error"]}
                introspection = [result]
            else:
                introspection = introspect_s3_prefix(bucket, prefix, region)
            if not introspection:
                return {"ok": False, "message": f"S3 {bucket}/{prefix} 未找到可分析的数据文件 (支持: Parquet/CSV/JSON/JSONL/TSV/Excel/ORC)"}
            test_msg = f"S3 发现 {len(introspection)} 个数据集"
            
        elif ds_type == "Athena":
            database = config.get("database", "")
            region = config.get("region", REGION)
            introspection = introspect_athena(database, region)
            if not introspection:
                return {"ok": False, "message": f"Athena 数据库 {database} 为空或无表"}
            test_msg = f"Athena {database} — {len(introspection)} 张表"
            
        elif ds_type == "SQLite":
            try:
                import sqlite3
                db_path = config.get("db_path", "/app/data/agentic_auto.db")
                conn = sqlite3.connect(db_path)
                tables = [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()]
                row_info = []
                for t in tables:
                    cnt = conn.execute(f"SELECT COUNT(*) FROM {_safe_id(t)}").fetchone()[0]  # nosec B608
                    row_info.append(f"{t}({cnt}行)")
                conn.close()
                return {"ok": True, "message": f"连接成功 — SQLite {db_path}，{len(tables)} 张表: {', '.join(row_info)}"}
            except Exception as e:
                return {"ok": False, "message": f"SQLite 连接失败: {str(e)[:100]}"}
        elif ds_type == "Snowflake":
            from agentic_core.db_engine import SnowflakeEngine
            eng = SnowflakeEngine(
                name=name or "snowflake",
                account=config.get("account", ""),
                user=config.get("user", ""),
                password=config.get("password", ""),
                warehouse=config.get("warehouse", ""),
                database=config.get("database", ""),
                schema=config.get("schema", "PUBLIC"),
                role=config.get("role", ""),
            )
            # Test connection
            eng.test_connection()
            introspection = introspect_sql_engine(eng)
            test_msg = f"Snowflake {config.get('database','')} — {len(introspection)} 张表"
            # Register engine for runtime use
            from agentic_core.db_engine import get_multi_engine
            multi = get_multi_engine()
            multi.register(eng.name.lower().replace(" ", "_"), eng)
            
        elif ds_type == "RDS" or ds_type == "PostgreSQL" or ds_type == "postgresql" or ds_type == "MySQL" or ds_type == "mysql":
            engine = config.get("engine", "mysql" if ds_type.lower() == "mysql" else "postgresql").lower()
            host = config.get("host", "")
            port = int(config.get("port", "3306" if engine == "mysql" else "5432"))
            database = config.get("database", "")
            user = config.get("user", config.get("username", ""))
            password = config.get("password", "")
            pg_schema = config.get("schema", "").strip()
            if not host or not database:
                return {"ok": False, "message": "请填写 Host 和 Database"}
            try:
                if engine == "mysql":
                    import pymysql
                    conn = pymysql.connect(host=host, port=port, database=database,
                                           user=user, password=password, connect_timeout=10,
                                           charset='utf8mb4', cursorclass=pymysql.cursors.DictCursor)
                    cur = conn.cursor()
                    cur.execute("SELECT table_name FROM information_schema.tables WHERE table_schema=%s AND table_type='BASE TABLE'", (database,))
                    introspection = []
                    for row in cur.fetchall():
                        t = row['TABLE_NAME']
                        cur.execute("SELECT column_name, data_type FROM information_schema.columns WHERE table_schema=%s AND table_name=%s ORDER BY ordinal_position", (database, t))
                        cols = [{"name": r['COLUMN_NAME'], "type": r['DATA_TYPE'], "desc": ""} for r in cur.fetchall()]
                        cur.execute(f"SELECT COUNT(*) as cnt FROM `{_safe_id(t)}`")  # nosec B608
                        cnt = cur.fetchone()['cnt']
                        introspection.append({"name": t, "columns": cols, "row_count": cnt, "type": "mysql", "schema": database, "table": t, "description": ""})
                    conn.close()
                    total_rows = sum(t['row_count'] for t in introspection)
                    test_msg = f"MySQL {host}:{port}/{database} — {len(introspection)} 张表, {total_rows:,} 行"
                    # Register MySQL engine for runtime
                    from agentic_core.db_engine import MySQLEngine, get_multi_engine
                    eng = MySQLEngine(host=host, port=port, database=database, user=user, password=password)
                    multi = get_multi_engine()
                    multi.register("mysql", eng)
                else:
                    import psycopg2
                    conn = psycopg2.connect(host=host, port=port, dbname=database, user=user, password=password, connect_timeout=10)
                    cur = conn.cursor()
                    # Auto-discover all user schemas if not specified
                    if pg_schema:
                        schemas_to_scan = [pg_schema]
                    else:
                        cur.execute("SELECT schema_name FROM information_schema.schemata WHERE schema_name NOT IN ('pg_catalog','information_schema','pg_toast') ORDER BY schema_name")
                        schemas_to_scan = [r[0] for r in cur.fetchall()]
                    introspection = []
                    for sch in schemas_to_scan:
                        cur.execute("SELECT table_name FROM information_schema.tables WHERE table_schema=%s ORDER BY table_name", (sch,))
                        tables = [r[0] for r in cur.fetchall()]
                        for t in tables:
                            cur.execute("SELECT column_name, data_type FROM information_schema.columns WHERE table_schema=%s AND table_name=%s ORDER BY ordinal_position", (sch, t))
                            cols = [{"name": r[0], "type": r[1], "desc": ""} for r in cur.fetchall()]
                            cur.execute(f'SELECT COUNT(*) FROM "{_safe_id(sch)}"."{_safe_id(t)}"')  # nosec B608
                            cnt = cur.fetchone()[0]
                            display_name = f"{sch}.{t}" if len(schemas_to_scan) > 1 else t
                            introspection.append({"name": display_name, "columns": cols, "row_count": cnt, "type": "postgresql", "schema": sch, "table": t, "description": ""})
                    conn.close()
                    total_rows = sum(t['row_count'] for t in introspection)
                    schema_label = pg_schema if pg_schema else ', '.join(schemas_to_scan)
                    test_msg = f"PostgreSQL {host}:{port}/{database} (schema: {schema_label}) — {len(introspection)} 张表, {total_rows:,} 行"
            except Exception as e:
                return {"ok": False, "message": f"RDS {engine} 连接失败: {str(e)[:200]}"}
            
        elif ds_type == "Tableau":
            from agentic_core.tableau_client import get_tableau_client
            from agentic_core.schema_inference import introspect_tableau
            client = get_tableau_client()
            introspection = introspect_tableau(client)
            test_msg = f"Tableau — {len(introspection)} 个视图"
            
        else:
            return {"ok": False, "message": f"不支持的数据源类型: {ds_type}"}
        
    except Exception as e:
        return {"ok": False, "message": f"连接失败: {str(e)}"}
    
    # Calculate total metrics/dimensions across all datasets
    total_metrics = sum(len(r.get("metrics", {})) for r in introspection)
    total_dims = sum(len(r.get("dimensions", {})) for r in introspection)
    
    # Register the datasource
    ds = {
        "id": f"custom-{uuid.uuid4().hex[:8]}",
        "name": name,
        "type": ds_type,
        "icon": {"DynamoDB":"⚡","S3":"💾","Athena":"📊","RDS":"🐬","PostgreSQL":"🐘","postgresql":"🐘","Redshift":"🔴","OpenSearch":"🔍","Snowflake":"❄️","Tableau":"📈"}.get(ds_type, "📦"),
        "config": config,  # Persist full config including credentials for runtime tools
        "description": description or test_msg,
        "tools": [],
        "custom": True,
        "status": "connected",
        "introspected": True,
        "enabled": True,
    }
    # Persist type-specific connection fields at top level (for dynamic tool docstring)
    if ds_type in ("PostgreSQL", "postgresql", "RDS"):
        ds["type"] = "postgresql"
        ds["host"] = config.get("host", "")
        ds["port"] = int(config.get("port", "5432"))
        ds["database"] = config.get("database", "")
        ds["tables"] = [t["name"] for t in introspection]
        ds["table_descriptions"] = {}
        for t in introspection:
            cols_dict = {c["name"]: c.get("desc", "") for c in t.get("columns", [])}
            ds["table_descriptions"][t["name"]] = {"description": t.get("description", ""), "columns": cols_dict}
    elif ds_type in ("Athena", "athena"):
        ds["database"] = config.get("database", "")
        ds["region"] = config.get("region", REGION)
        ds["output_location"] = config.get("output_location", "")
        ds["tables"] = [t["name"] for t in introspection]
    _custom_data_sources.append(ds)
    _save_config("custom_datasources", _custom_data_sources)
    try:
        from agentic_core.tools import invalidate_pg_pool
        invalidate_pg_pool()
    except: pass
    
    # ── Auto-generate semantic layer for supported datasources ──
    _gen_engine = {"postgresql": "postgresql", "athena": "athena", "snowflake": "snowflake"}.get(ds.get("type", "").lower())
    if _gen_engine and introspection:
        try:
            from agentic_core.semantic_layer import generate_semantic, METRICS, DIMENSIONS, SYNONYMS
            _gen_db = ds.get("database", config.get("database", ""))
            auto_sem = generate_semantic(_gen_engine, introspection, datasource_id=ds["id"], database=_gen_db)
            if auto_sem.get("metrics"):
                METRICS.update(auto_sem["metrics"])
            if auto_sem.get("dimensions"):
                DIMENSIONS.update(auto_sem["dimensions"])
            if auto_sem.get("synonyms"):
                SYNONYMS.update(auto_sem["synonyms"])
            # Persist to semantic_custom
            _load_semantic_custom()
            for key in ("metrics", "dimensions", "synonyms"):
                if key not in _semantic_custom:
                    _semantic_custom[key] = {}
                _semantic_custom[key].update(auto_sem.get(key, {}))
            if "synonym_sources" not in _semantic_custom:
                _semantic_custom["synonym_sources"] = {}
            for alias in auto_sem.get("synonyms", {}):
                _semantic_custom["synonym_sources"][alias] = ds["id"]
            _save_semantic_custom()
            print(f"[SemanticLayer] Auto-generated {_gen_engine} semantic: {len(auto_sem['metrics'])} metrics, {len(auto_sem['dimensions'])} dims, {len(auto_sem['synonyms'])} synonyms")
        except Exception as e:
            print(f"[SemanticLayer] Auto-semantic ({_gen_engine}) failed: {e}")
            import traceback; traceback.print_exc()
    
    # Stage introspection for confirmation
    _introspection_staging[ds["id"]] = {
        "source": ds,
        "introspection": introspection,
        "timestamp": time.time(),
    }
    # Also persist to DDB for cross-worker access
    try:
        _save_config(f"staging_{ds['id']}", {"source": ds, "introspection": introspection, "timestamp": time.time()})
    except Exception as _e:

        print(f"[WARN] swallowed exception: {_e}")
    
    return {
        "ok": True,
        "message": test_msg,
        "source": ds,
        "introspection": introspection,
        "totals": {
            "datasets": len(introspection),
            "metrics": total_metrics,
            "dimensions": total_dims,
        },
    }

_introspection_staging = {}


@app.post("/api/datasources/connect/apply")
def api_apply_introspection(req: dict):
    """
    应用自动发现的语义层定义。
    
    {
        "source_id": "custom-xxx",
        "selected_metrics": {...},    // 用户确认的指标 (可编辑过)
        "selected_dimensions": {...}, // 用户确认的维度
        "selected_synonyms": {...},   // 同义词
    }
    """
    source_id = req.get("source_id", "")
    staging = _introspection_staging.get(source_id)
    if not staging:
        # Try DDB fallback (cross-worker)
        try:
            staging = _load_config(f"staging_{source_id}", None)
        except Exception as _e:

            print(f"[WARN] swallowed exception: {_e}")
    if not staging:
        return JSONResponse({"error": "未找到待确认的探查结果"}, 400)
    
    introspection = staging["introspection"]
    
    # Merge all metrics/dimensions from introspection OR use user-edited ones
    all_metrics = req.get("selected_metrics") or {}
    all_dims = req.get("selected_dimensions") or {}
    all_synonyms = req.get("selected_synonyms") or {}
    
    if not all_metrics:
        for r in introspection:
            all_metrics.update(r.get("metrics", {}))
    if not all_dims:
        for r in introspection:
            all_dims.update(r.get("dimensions", {}))
    if not all_synonyms:
        for r in introspection:
            all_synonyms.update(r.get("synonyms", {}))
    
    # Apply to semantic layer
    applied = {"metrics": 0, "dimensions": 0, "synonyms": 0}
    
    if all_metrics:
        from agentic_core.semantic_layer import METRICS
        _load_semantic_custom()
        for name, defn in all_metrics.items():
            METRICS[name] = defn
            _semantic_custom["metrics"][name] = defn
        applied["metrics"] = len(all_metrics)
    
    if all_dims:
        from agentic_core.semantic_layer import DIMENSIONS
        for name, defn in all_dims.items():
            if name not in DIMENSIONS:
                DIMENSIONS[name] = defn
                applied["dimensions"] += 1
    
    if all_synonyms:
        from agentic_core.semantic_layer import SYNONYMS
        _load_semantic_custom()
        for alias, target in all_synonyms.items():
            SYNONYMS[alias] = target
            _semantic_custom["synonyms"][alias] = target
        applied["synonyms"] = len(all_synonyms)
    
    
    # Register all discovered datasets as ChatBI data sources
    from agentic_core.tools import CHATBI_DATASETS
    for r in introspection:
        src_type = r.get("source_type", "")
        cfg = r.get("source_config", {})
        ds_name = r.get("dataset", {}).get("name", "")
        if not ds_name:
            continue
        
        entry = {"desc": r.get("dataset", {}).get("desc", ds_name)}
        
        if src_type == "S3" and cfg.get("key"):
            entry["key"] = cfg["key"]
            entry["bucket"] = cfg.get("bucket", "")
            entry["region"] = cfg.get("region", "")
            entry["format"] = cfg.get("format", ".json")
        elif src_type == "DynamoDB":
            entry["type"] = "dynamodb"
            entry["table"] = cfg.get("table", "")
            entry["region"] = cfg.get("region", "")
        elif src_type == "Athena":
            entry["type"] = "athena"
            entry["database"] = cfg.get("database", "")
            entry["table"] = cfg.get("table", "")
            entry["region"] = cfg.get("region", "")
        elif src_type in ("SQL", "SQLite", "Snowflake", "PostgreSQL", "RDS"):
            entry["type"] = "sql"
            entry["engine"] = cfg.get("engine_name", src_type.lower())
            entry["table"] = cfg.get("table", "")
        
        CHATBI_DATASETS[ds_name] = entry
    
    # Save all custom chatbi datasets (exclude legacy built-in names)
    builtin = {"vehicle_master","app_usage","service_records","driving_daily",
               "battery_health","ota_records","customer_feedback","charging_records"}
    custom_ds = {k: v for k, v in CHATBI_DATASETS.items() if k not in builtin}
    _save_config("custom_chatbi_datasets", custom_ds)
    
    # Force agent rebuild
    _agents.clear()
    
    # Clean staging
    _introspection_staging.pop(source_id, None)
    try:
        boto3.resource("dynamodb", region_name=REGION).Table(CONFIG_TABLE).delete_item(Key={"config_key": f"staging_{source_id}"})
    except Exception as _e:

        print(f"[WARN] swallowed exception: {_e}")
    
    return {
        "status": "ok",
        "applied": applied,
        "message": f"✅ 语义层已更新: {applied['metrics']}个指标 + {applied['dimensions']}个维度 + {applied['synonyms']}个同义词",
    }


# ───────────────── Smart Dashboard API ─────────────────

def _get_active_chatbi_datasets():
    """Return set of chatbi dataset names that have connected datasources."""
    global _custom_data_sources
    _custom_data_sources = _load_config("custom_datasources", [])
    active = set()
    all_ds = _data_sources_default + _custom_data_sources
    for ds in all_ds:
        cfg = ds.get("config", {})
        prefix = cfg.get("prefix", "")
        # S3 datasource pointing to chatbi/ files
        if ds.get("type") == "S3" and "chatbi/" in prefix:
            name = prefix.replace("chatbi/", "").replace(".json", "")
            if name:
                active.add(name)
        # S3 datasource with chatbi/ in key
        key = cfg.get("key", "")
        if ds.get("type") == "S3" and "chatbi/" in key:
            name = key.replace("chatbi/", "").replace(".json", "")
            if name:
                active.add(name)
        # Match by dataset_name field (for datalake/Parquet sources mapped to chatbi)
        ds_name = ds.get("dataset_name", "")
        if ds_name:
            active.add(ds_name)
    # If datasources exist but none matched chatbi paths → activate ALL chatbi datasets
    if all_ds and not active:
        from agentic_core.tools import CHATBI_DATASETS
        active = set(CHATBI_DATASETS.keys())
    return active

@app.get("/api/dashboard")
def api_dashboard():
    """Smart dashboard: dynamic KPI cards + alert rules from DynamoDB."""
    pass  # _load_chatbi removed
    from agentic_core.alert_rules import evaluate_alerts, evaluate_kpis, ensure_defaults

    active_ds = _get_active_chatbi_datasets()
    result = {"kpis": [], "alerts": [], "has_datasources": len(active_ds) > 0}
    if not active_ds:
        return result

    # Seed default rules on first access
    ensure_defaults()

    # Load all active datasets
    datasets = {}
    for ds_name in active_ds:
        try:
            data = _load_chatbi(ds_name)
            if data:
                datasets[ds_name] = data
        except Exception as e:
            print(f"Dashboard load {ds_name}: {e}")

    # Evaluate KPIs and alerts using rule engine
    result["kpis"] = evaluate_kpis(datasets)
    result["alerts"] = evaluate_alerts(datasets)

    return result


# ───────────────── Alert & KPI Rules API ─────────────────

@app.get("/api/alert-rules")
def api_get_alert_rules():
    from agentic_core.alert_rules import load_alert_rules, ensure_defaults
    ensure_defaults()
    return {"rules": load_alert_rules()}

@app.post("/api/alert-rules")
def api_create_alert_rule(req: dict, request: Request):
    user = get_current_user(request)
    if user.get("role") != "admin":
        return {"ok": False, "message": "仅管理员可配置告警规则"}
    from agentic_core.alert_rules import load_alert_rules, save_alert_rules
    import uuid as _uuid
    rules = load_alert_rules()
    rule = {
        "id": f"rule-{_uuid.uuid4().hex[:8]}",
        "name": req.get("name", "新规则"),
        "enabled": req.get("enabled", True),
        "dataset": req.get("dataset", ""),
        "field": req.get("field", ""),
        "operator": req.get("operator", "<"),
        "threshold": req.get("threshold"),
        "level": req.get("level", "MEDIUM"),
        "category": req.get("category", ""),
        "title_template": req.get("title_template", "{value}"),
        "detail_template": req.get("detail_template", ""),
        "query_template": req.get("query_template", ""),
        "dedup_field": req.get("dedup_field"),
        "dedup_order": req.get("dedup_order", "date"),
        "extra_filter": req.get("extra_filter"),
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    rules.append(rule)
    save_alert_rules(rules)
    return {"ok": True, "rule": rule}

@app.put("/api/alert-rules/{rule_id}")
def api_update_alert_rule(rule_id: str, req: dict, request: Request):
    user = get_current_user(request)
    if user.get("role") != "admin":
        return {"ok": False, "message": "仅管理员可配置告警规则"}
    from agentic_core.alert_rules import load_alert_rules, save_alert_rules
    rules = load_alert_rules()
    for r in rules:
        if r["id"] == rule_id:
            for k in ["name","enabled","dataset","field","operator","threshold","level",
                      "category","title_template","detail_template","query_template",
                      "dedup_field","dedup_order","extra_filter"]:
                if k in req:
                    r[k] = req[k]
            r["updated_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
            save_alert_rules(rules)
            return {"ok": True, "rule": r}
    return {"ok": False, "message": "规则不存在"}

@app.delete("/api/alert-rules/{rule_id}")
def api_delete_alert_rule(rule_id: str, request: Request):
    user = get_current_user(request)
    if user.get("role") != "admin":
        return {"ok": False, "message": "仅管理员可配置告警规则"}
    from agentic_core.alert_rules import load_alert_rules, save_alert_rules
    rules = load_alert_rules()
    rules = [r for r in rules if r["id"] != rule_id]
    save_alert_rules(rules)
    return {"ok": True}

@app.get("/api/kpi-rules")
def api_get_kpi_rules():
    from agentic_core.alert_rules import load_kpi_rules, ensure_defaults
    ensure_defaults()
    return {"rules": load_kpi_rules()}

@app.post("/api/kpi-rules")
def api_create_kpi_rule(req: dict, request: Request):
    user = get_current_user(request)
    if user.get("role") != "admin":
        return {"ok": False, "message": "仅管理员可配置 KPI 规则"}
    from agentic_core.alert_rules import load_kpi_rules, save_kpi_rules
    import uuid as _uuid
    rules = load_kpi_rules()
    rule = {
        "id": f"kpi-{_uuid.uuid4().hex[:8]}",
        "name": req.get("name", "新指标"),
        "dataset": req.get("dataset", ""),
        "agg": req.get("agg", "avg"),
        "field": req.get("field", ""),
        "dedup_field": req.get("dedup_field"),
        "dedup_order": req.get("dedup_order", "date"),
        "format": req.get("format", "{value}"),
        "thresholds": req.get("thresholds", {}),
        "query": req.get("query", ""),
        "extra_label": req.get("extra_label", ""),
        "order": req.get("order", 99),
        "where_op": req.get("where_op"),
        "where_val": req.get("where_val"),
        "success_values": req.get("success_values"),
    }
    rules.append(rule)
    save_kpi_rules(rules)
    return {"ok": True, "rule": rule}

@app.put("/api/kpi-rules/{rule_id}")
def api_update_kpi_rule(rule_id: str, req: dict, request: Request):
    user = get_current_user(request)
    if user.get("role") != "admin":
        return {"ok": False, "message": "仅管理员可配置 KPI 规则"}
    from agentic_core.alert_rules import load_kpi_rules, save_kpi_rules
    rules = load_kpi_rules()
    for r in rules:
        if r["id"] == rule_id:
            for k in ["name","dataset","agg","field","dedup_field","dedup_order",
                      "format","thresholds","query","extra_label","order",
                      "where_op","where_val","success_values"]:
                if k in req:
                    r[k] = req[k]
            save_kpi_rules(rules)
            return {"ok": True, "rule": r}
    return {"ok": False, "message": "KPI 规则不存在"}

@app.delete("/api/kpi-rules/{rule_id}")
def api_delete_kpi_rule(rule_id: str, request: Request):
    user = get_current_user(request)
    if user.get("role") != "admin":
        return {"ok": False, "message": "仅管理员可配置 KPI 规则"}
    from agentic_core.alert_rules import load_kpi_rules, save_kpi_rules
    rules = load_kpi_rules()
    rules = [r for r in rules if r["id"] != rule_id]
    save_kpi_rules(rules)
    return {"ok": True}


# ───────────────── Dashboard Pinned Cards API ─────────────────

@app.get("/api/dashboard/cards")
def api_get_dashboard_cards():
    """Get all pinned dashboard cards."""
    cards = _load_config("dashboard_cards", [])
    return {"cards": cards}

@app.post("/api/dashboard/cards")
def api_create_dashboard_card(req: dict, request: Request):
    """Pin a chart/text card to the smart dashboard."""
    user = get_current_user(request)
    cards = _load_config("dashboard_cards", [])
    card = {
        "id": f"card-{uuid.uuid4().hex[:8]}",
        "title": req.get("title", "图表"),
        "type": req.get("type", "chart"),  # chart, dashboard, text
        "chart_config": req.get("chart_config"),
        "dashboard_config": req.get("dashboard_config"),
        "text": req.get("text", ""),
        "source": req.get("source", "chat"),
        "pinned_by": user.get("email", ""),
        "pinned_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "order": req.get("order", len(cards)),
    }
    cards.append(card)
    _save_config("dashboard_cards", cards)
    return {"ok": True, "card": card}

@app.delete("/api/dashboard/cards/{card_id}")
def api_delete_dashboard_card(card_id: str, request: Request):
    """Unpin a card from dashboard."""
    user = get_current_user(request)
    cards = _load_config("dashboard_cards", [])
    card = next((c for c in cards if c["id"] == card_id), None)
    if card and card.get("pinned_by") != user.get("email","") and user.get("role") != "admin":
        return {"ok": False, "message": "只能删除自己钉住的内容"}
    cards = [c for c in cards if c["id"] != card_id]
    _save_config("dashboard_cards", cards)
    return {"ok": True}


# ───────────────── Enhanced Map API ─────────────────

CITY_COORDS = {
    "北京": [39.9042, 116.4074], "上海": [31.2304, 121.4737], "广州": [23.1291, 113.2644],
    "深圳": [22.5431, 114.0579], "成都": [30.5728, 104.0668], "杭州": [30.2741, 120.1551],
    "武汉": [30.5928, 114.3055], "南京": [32.0603, 118.7969],
}

# Dealer sub-locations (offset from city center for visual separation)
import random as _rnd
_dealer_offsets = {}
def _dealer_coord(dealer_name):
    if dealer_name in _dealer_offsets:
        return _dealer_offsets[dealer_name]
    for city, coord in CITY_COORDS.items():
        if city in dealer_name:
            _rnd.seed(hash(dealer_name) % 10000)
            offset = [coord[0] + _rnd.uniform(-0.06, 0.06), coord[1] + _rnd.uniform(-0.06, 0.06)]
            _dealer_offsets[dealer_name] = offset
            return offset
    return None

# [REMOVED] @app.get("/api/map-layers") — dead feature
# @app.get("/api/map-layers")
def api_map_layers():
    """Multi-layer map data: safety events, charging heatmap, service network, feedback."""
    pass  # _load_chatbi removed
    from collections import Counter

    result = {"layers": {}}

    # Layer 1: Safety events (already have lat/lng)
    events = load_events()
    result["layers"]["safety"] = {
        "label": "安全事件",
        "icon": "alert",
        "count": len(events),
        "points": [{"lat": e["latitude"], "lng": e["longitude"], "severity": e.get("severity",""),
                     "title": e.get("title","") or e.get("event_type",""),
                     "detail": f"{e.get('road_name','')} · {str(e.get('timestamp',''))[:16]}",
                     "vin": e.get("vin",""),
                     "query": f"VIN {e.get('vin','')} 的安全事件详细分析"} for e in events if e.get("latitude")]
    }

    # Layer 2: Charging heatmap by city
    try:
        ch = _load_chatbi("charging_records")
        city_stats = {}
        for r in ch:
            city = r.get("city", "")
            if city not in city_stats:
                city_stats[city] = {"count": 0, "total_cost": 0, "total_kwh": 0, "fast": 0}
            city_stats[city]["count"] += 1
            city_stats[city]["total_cost"] += r.get("cost_yuan", 0)
            city_stats[city]["total_kwh"] += r.get("energy_kwh", 0)
            if r.get("station_type") in ("fast", "快充", "DC快充"):
                city_stats[city]["fast"] += 1

        charging_points = []
        for city, stats in city_stats.items():
            coord = CITY_COORDS.get(city)
            if coord:
                charging_points.append({
                    "lat": coord[0], "lng": coord[1], "city": city,
                    "count": stats["count"],
                    "avg_cost": round(stats["total_cost"] / stats["count"], 1),
                    "fast_ratio": round(stats["fast"] / stats["count"] * 100),
                    "total_kwh": round(stats["total_kwh"], 1),
                    "query": f"{city}的充电数据分析：快充慢充比例、平均费用、充电时段"
                })
        result["layers"]["charging"] = {
            "label": "充电热力",
            "icon": "charge",
            "count": len(ch),
            "points": charging_points
        }
    except Exception as _e:
        print(f"[WARN] silent exception: {_e}")

    # Layer 3: Service network (4S dealers)
    try:
        if "service_records" not in active_ds: raise Exception("skip")
        svc = _load_chatbi("service_records")
        dealer_stats = {}
        for r in svc:
            dealer = r.get("dealer", "")
            if dealer not in dealer_stats:
                dealer_stats[dealer] = {"count": 0, "total_cost": 0, "sats": []}
            dealer_stats[dealer]["count"] += 1
            dealer_stats[dealer]["total_cost"] += r.get("cost_yuan", 0)
            if isinstance(r.get("satisfaction_score"), (int, float)):
                dealer_stats[dealer]["sats"].append(r["satisfaction_score"])

        svc_points = []
        for dealer, stats in dealer_stats.items():
            coord = _dealer_coord(dealer)
            if coord:
                avg_sat = round(sum(stats["sats"]) / len(stats["sats"]), 1) if stats["sats"] else 0
                svc_points.append({
                    "lat": coord[0], "lng": coord[1], "name": dealer,
                    "count": stats["count"], "avg_cost": round(stats["total_cost"] / stats["count"]),
                    "satisfaction": avg_sat,
                    "query": f"{dealer}的服务记录分析：维修类型、费用、客户满意度"
                })
        result["layers"]["service"] = {
            "label": "售后网点",
            "icon": "wrench",
            "count": len(svc_points),
            "points": svc_points
        }
    except Exception as _e:
        print(f"[WARN] silent exception: {_e}")

    # Layer 4: Customer feedback by city (aggregate via vehicle_master city)
    try:
        if "customer_feedback" not in active_ds: raise Exception("skip")
        fb = _load_chatbi("customer_feedback")
        if "vehicle_master" not in active_ds: raise Exception("skip")
        vm = _load_chatbi("vehicle_master")
        vin_city = {r["vin"]: r.get("city", "") for r in vm}
        city_fb = {}
        for r in fb:
            city = vin_city.get(r.get("vin", ""), "")
            if not city:
                continue
            if city not in city_fb:
                city_fb[city] = {"total": 0, "high": 0, "pending": 0}
            city_fb[city]["total"] += 1
            if r.get("severity") in ("high", "HIGH", "紧急"):
                city_fb[city]["high"] += 1
            if r.get("status") in ("pending", "open", "处理中"):
                city_fb[city]["pending"] += 1

        fb_points = []
        for city, stats in city_fb.items():
            coord = CITY_COORDS.get(city)
            if coord:
                fb_points.append({
                    "lat": coord[0], "lng": coord[1], "city": city,
                    "total": stats["total"], "high": stats["high"], "pending": stats["pending"],
                    "query": f"{city}地区客户投诉分析：反馈类型、严重程度、处理时效"
                })
        result["layers"]["feedback"] = {
            "label": "客户反馈",
            "icon": "feedback",
            "count": len(fb),
            "points": fb_points
        }
    except Exception as _e:
        print(f"[WARN] silent exception: {_e}")

    return result


# ───────────────── Agent Management API ─────────────────

# Default agent definitions — used as templates and reset targets
_DEFAULT_AGENTS = {}  # 空平台: 无预置 Agent, 用户通过智能配置或手动创建

# Available tools registry for assignment
_ALL_TOOLS = {}
def _init_all_tools():
    import inspect
    from agentic_core import tools as t
    # Only include actual tool functions defined in the module
    _known_tools = {
        # 核心数据查询
        "semantic_query", "get_data_catalog",
        # SQL 引擎 (MySQL / PostgreSQL / Athena / Snowflake)
        "pg_query", "nl2sql_query", "snowflake_query",
        # 报告 & 告警
        "save_report", "list_reports",
        "manage_alert_rules", "manage_kpi_rules",
    }
    for name in sorted(_known_tools):
        fn = getattr(t, name, None)
        if fn and callable(fn):
            doc = (fn.__doc__ or "").strip().split("\n")[0][:80]
            _ALL_TOOLS[name] = {"name": name, "description": doc}

_agent_defs_cache = [None, 0]  # [data, load_ts]

def _get_agent_defs():
    """Load agent definitions from DynamoDB config (with 30s TTL cache)."""
    import time as _t
    # 先查内存缓存
    if _agent_defs_cache[0] is not None and (_t.time() - _agent_defs_cache[1]) < 30:
        return _agent_defs_cache[0]
    # 再查 _configs (可能由 _save_agent_defs 写入)
    saved = _configs.get("agent_definitions")
    if saved:
        _agent_defs_cache[0] = saved
        _agent_defs_cache[1] = _t.time()
        return saved
    # 从 DynamoDB 加载
    loaded = _load_config("agent_definitions", None)
    if loaded and isinstance(loaded, dict) and len(loaded) > 0:
        _configs["agent_definitions"] = loaded
        _agent_defs_cache[0] = loaded
        _agent_defs_cache[1] = _t.time()
        return loaded
    # 空平台: 无预置 Agent
    return {}

def _save_agent_defs(defs):
    _configs["agent_definitions"] = defs
    _agent_defs_cache[0] = defs
    _agent_defs_cache[1] = __import__("time").time()
    _save_config("agent_definitions", defs)  # 只存 defs, 不是整个 _configs

@app.get("/api/agents")
def api_list_agents():
    if not _ALL_TOOLS:
        _init_all_tools()
    defs = _get_agent_defs()
    from agentic_core import (AVAILABLE_MODELS, SUB_AGENT_MODELS,
        DEFAULT_SYSTEM_PROMPT,
        DEFAULT_DATA_ANALYST_PROMPT)
    from agentic_core.dynamic_context import build_data_analyst_prompt, build_supervisor_data_section
    default_prompts = {
        "supervisor": DEFAULT_SYSTEM_PROMPT.replace("{{DYNAMIC_DATA_SECTION}}", build_supervisor_data_section()),
        "data_analyst": build_data_analyst_prompt(),
    }
    return {
        "agents": list(defs.values()),
        "available_tools": sorted(_ALL_TOOLS.values(), key=lambda x: x["name"]),
        "defaults": {k: dict(v) for k, v in _DEFAULT_AGENTS.items()},
        "default_prompts": default_prompts,
        "models": {"primary": AVAILABLE_MODELS, "sub": SUB_AGENT_MODELS, "custom": [{"name": m["name"], "model_id": m.get("model_id",""), "protocol": m.get("protocol","openai")} for m in _custom_models]},
    }

@app.get("/api/agents/recommend")
def api_recommend_agents():
    """基于已连接数据源推荐 Agent 架构"""
    from agentic_core.agent_recommender import recommend
    from agentic_core.semantic_layer import METRICS, DIMENSIONS, SYNONYMS
    
    global _custom_data_sources
    _custom_data_sources = _load_config("custom_datasources", [])
    all_ds = _data_sources_default + _custom_data_sources
    
    # 补充 chatbi dataset 的字段信息
    from agentic_core.tools import CHATBI_DATASETS
    enriched = []
    for ds in all_ds:
        ds_copy = dict(ds)
        name = ds.get("name", "")
        cfg = ds.get("config", {})
        prefix = cfg.get("prefix", name)
        chatbi = CHATBI_DATASETS.get(prefix) or CHATBI_DATASETS.get(name)
        if chatbi:
            ds_copy["columns"] = chatbi.get("columns", [])
            ds_copy["record_count"] = chatbi.get("total_records", 0)
        enriched.append(ds_copy)
    
    semantic = {"metrics": METRICS, "dimensions": DIMENSIONS, "synonyms": SYNONYMS}
    model_provider = os.environ.get("AGENTIC_AUTO_MODEL_PROVIDER", "bedrock")
    
    result = recommend(enriched, semantic, model_provider)
    return result


@app.post("/api/agents/recommend/apply")
def api_apply_recommendation(req: dict):
    """应用推荐的 Agent 架构"""
    supervisor = req.get("supervisor")
    sub_agents = req.get("sub_agents", [])
    
    if not supervisor:
        raise HTTPException(status_code=400, detail="缺少 Supervisor 配置")
    
    # 构建 agent_definitions
    defs = {}
    
    # 读取当前模型配置
    _gcfg = _configs.get("global", {})
    _sup_model = supervisor.get("model_id") or _gcfg.get("supervisor_model", "")
    _sub_model = _gcfg.get("sub_agent_model", "")
    # 如果没有全局配置，尝试从自定义模型中取第一个
    if not _sup_model:
        _load_custom_models()
        if _custom_models:
            _sup_model = "custom:" + _custom_models[0]["name"]
    if not _sub_model:
        _sub_model = _sup_model  # fallback to supervisor model

    # Supervisor
    defs["supervisor"] = {
        "id": "supervisor",
        "name": supervisor.get("name", "Supervisor"),
        "name_zh": supervisor.get("name_zh", "调度中枢"),
        "role": "orchestrator",
        "enabled": True,
        "deletable": True,
        "description": supervisor.get("description", ""),
        "model_type": "primary",
        "model_note": supervisor.get("model_note", "") or _sup_model,
        "model_id": _sup_model,
        "tools": supervisor.get("tools", []),
        "capabilities": supervisor.get("capabilities", []),
        "prompt_override": supervisor.get("prompt", ""),
    }
    
    # Sub-agents
    for sa in sub_agents:
        agent_id = sa.get("id", "")
        if not agent_id:
            continue
        defs[agent_id] = {
            "id": agent_id,
            "name": sa.get("name", ""),
            "name_zh": sa.get("name_zh", ""),
            "role": "sub-agent",
            "enabled": sa.get("enabled", True),
            "deletable": True,
            "description": sa.get("description", ""),
            "model_type": "sub",
            "model_note": sa.get("model_note", "") or _sub_model,
            "model_id": sa.get("model_id", "") or _sub_model,
            "tools": sa.get("tools", []),
            "capabilities": sa.get("capabilities", []),
            "prompt_override": sa.get("prompt", ""),
            "datasets": sa.get("datasets", []),
        }
    
    _save_agent_defs(defs)
    bump_data_version(); _agents.clear()  # 清缓存, 下次会用新定义重建
    
    return {"ok": True, "agents_count": len(defs), "agents": list(defs.values())}


@app.post("/api/agents/recommend/test")
def api_test_recommendation(req: dict):
    """运行推荐架构的测试用例 — 自动先应用推荐配置再测试"""
    question = req.get("question", "")
    if not question:
        raise HTTPException(status_code=400, detail="缺少 question")
    
    import time
    
    # 确保当前有 agent 配置 (如果没有, 提示先应用)
    defs = _get_agent_defs()
    if not defs:
        return {
            "question": question,
            "response": "",
            "passed": False,
            "error": "请先点击「一键应用」配置 Agent，再运行测试",
        }
    
    session_id = f"test_{int(time.time())}"
    try:
        agent = get_agent(session_id, force_new=True)
        result = agent(question)
        response_text = str(result)
        
        # 检查 expected_keywords
        expected = req.get("expected_keywords", [])
        passed = True
        if expected:
            passed = any(kw in response_text for kw in expected)
        
        # 无 expected_keywords 时, 只要有响应且不是错误就算 pass
        if not expected:
            passed = len(response_text.strip()) > 20
        
        return {
            "question": question,
            "response": response_text[:2000],
            "passed": passed,
            "matched_keywords": [kw for kw in expected if kw in response_text],
        }
    except Exception as e:
        return {
            "question": question,
            "response": "",
            "passed": False,
            "error": str(e)[:500],
        }
    finally:
        _agents.pop(session_id, None)





@app.get("/api/agents/logs")
def api_agent_logs(limit: int = 50):
    """Get recent agent run logs from cost table."""
    try:
        resp = _ddb_resource.Table(COST_TABLE).scan(Limit=limit)
        items = sorted(resp.get("Items", []), key=lambda x: x.get("timestamp", ""), reverse=True)
        logs = []
        for item in items[:limit]:
            logs.append({
                "timestamp": item.get("timestamp", ""),
                "session_id": item.get("session_id", ""),
                "model": item.get("model_id", ""),
                "input_tokens": int(item.get("input_tokens", 0)),
                "output_tokens": int(item.get("output_tokens", 0)),
                "cost_usd": float(item.get("cost_usd", 0)),
                "user": item.get("user_id", ""),
            })
        return {"logs": logs, "count": len(logs)}
    except Exception as e:
        return {"logs": [], "error": str(e)[:200]}


# ═══════ Agent Health Dashboard ═══════

@app.get("/api/agents/health")
def api_agent_health():
    """Aggregated agent health metrics from cost + chat + feedback tables."""
    import time as _t
    from decimal import Decimal
    
    now = _t.time()
    seven_days_ago = now - 7 * 86400
    today_str = _t.strftime("%Y-%m-%d", _t.gmtime())
    
    health = {
        "agents": {},
        "top_failures": [],
        "semantic_coverage": {"hit": 0, "miss": 0, "rate": 0},
        "suggestions": [],
    }
    
    # 1. Aggregate from cost table (fields: model, date, cost, input_tokens, output_tokens, timestamp, steps, duration)
    try:
        cost_table = boto3.resource("dynamodb", region_name=REGION).Table(COST_TABLE)
        resp = cost_table.scan(Limit=500)
        items = resp.get("Items", [])
        
        agent_stats = {}  # agent_id -> stats
        for item in items:
            ts = item.get("timestamp", "")
            # Use explicit agent field if present, otherwise infer from model
            agent_id = item.get("agent", "")
            if not agent_id:
                model = str(item.get("model", "unknown")).lower()
                if "sonnet" in model or "opus" in model or "claude" in model:
                    agent_id = "Supervisor"
                elif "haiku" in model or "glm" in model or "deepseek" in model or "qwen" in model:
                    agent_id = "DataAnalyst"
                else:
                    agent_id = "Supervisor"
            # Normalize legacy names
            name_map = {"supervisor": "Supervisor", "data_analyst": "DataAnalyst", "DataAnalystAgent": "DataAnalyst"}
            agent_id = name_map.get(agent_id, agent_id)
            
            if agent_id not in agent_stats:
                agent_stats[agent_id] = {"calls": 0, "total_latency_s": 0, "tokens": 0, "cost": 0.0, "errors": 0, "today_calls": 0}
            
            s = agent_stats[agent_id]
            s["calls"] += 1
            s["tokens"] += int(item.get("input_tokens", 0)) + int(item.get("output_tokens", 0))
            cost_val = item.get("cost", 0)
            s["cost"] += float(cost_val) if cost_val else 0
            dur = item.get("duration", 0)
            s["total_latency_s"] += float(dur) if dur else 0
            # Count errors
            err = item.get("error", "")
            if err and str(err).strip():
                s["errors"] += 1
            if ts.startswith(today_str):
                s["today_calls"] += 1
        
        for aid, stats in agent_stats.items():
            stats["avg_latency_s"] = round(stats["total_latency_s"] / max(stats["calls"], 1), 1)
            stats["success_rate"] = round((stats["calls"] - stats["errors"]) / max(stats["calls"], 1) * 100, 1)
            stats["cost"] = round(stats["cost"], 4)
            del stats["total_latency_s"]
        
        health["agents"] = agent_stats
    except Exception as e:
        print(f"[WARN] health cost scan: {e}")
    
    # 2. Analyze failed/low-quality queries from feedback (recent 7 days only)
    try:
        fb_table = boto3.resource("dynamodb", region_name=REGION).Table(FEEDBACK_TABLE)
        resp = fb_table.scan(Limit=200)
        cutoff_ts = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat()
        negative = [i for i in resp.get("Items", []) 
                    if (i.get("rating") == "down" or i.get("score", 5) < 3)
                    and i.get("timestamp", "9999") >= cutoff_ts]
        # Group by question similarity (simple: exact match)
        fail_counts = {}
        for fb in negative:
            q = fb.get("question", fb.get("query", ""))[:80]
            if q:
                fail_counts[q] = fail_counts.get(q, 0) + 1
        health["top_failures"] = sorted(
            [{"question": q, "count": c} for q, c in fail_counts.items()],
            key=lambda x: -x["count"]
        )[:5]
    except Exception as e:
        print(f"[WARN] health feedback scan: {e}")
    
    # 3. Semantic coverage (from recent queries)
    try:
        _load_semantic_custom()
        from agentic_core.semantic_layer import find_matching_metrics, METRICS
        try:
            from agentic_core.semantic_layer import find_matching_templates
        except ImportError:
            find_matching_templates = lambda q: []
        chat_table = boto3.resource("dynamodb", region_name=REGION).Table(CHAT_TABLE)
        resp = chat_table.scan(Limit=200)
        questions = [i["content"] for i in resp.get("Items", []) if i.get("role") == "user" and i.get("content")]
        hit = miss = 0
        miss_questions = []
        for q in questions:
            if find_matching_metrics(q) or find_matching_templates(q):
                hit += 1
            else:
                miss += 1
                if len(miss_questions) < 5:
                    miss_questions.append(q[:60])
        total = hit + miss
        health["semantic_coverage"] = {
            "hit": hit, "miss": miss,
            "rate": round(hit / max(total, 1) * 100, 1),
            "total_metrics": len(METRICS),
            "miss_examples": miss_questions,
        }
    except Exception as e:
        print(f"[WARN] health semantic scan: {e}")
    
    # 4. Generate suggestions
    sugs = []
    sc = health["semantic_coverage"]
    if sc["rate"] < 70:
        sugs.append({"type": "semantic", "priority": "high", "text": f"语义层覆盖率 {sc['rate']}%，建议添加更多同义词和指标"})
    if sc.get("miss_examples"):
        sugs.append({"type": "semantic", "priority": "medium", "text": f"未命中查询示例: {', '.join(sc['miss_examples'][:3])}"})
    if health["top_failures"]:
        top = health["top_failures"][0]
        sugs.append({"type": "quality", "priority": "high", "text": f"查询「{top['question']}」失败 {top['count']} 次，建议检查相关工具"})
    health["suggestions"] = sugs
    
    return health


@app.get("/api/agents/{agent_id}")
def api_get_agent(agent_id: str):
    defs = _get_agent_defs()
    if agent_id not in defs:
        raise HTTPException(status_code=404, detail="Agent not found")
    return defs[agent_id]

class AgentUpdate(BaseModel):
    name: Optional[str] = None
    name_zh: Optional[str] = None
    description: Optional[str] = None
    enabled: Optional[bool] = None
    tools: Optional[list] = None
    skills: Optional[list] = None
    capabilities: Optional[list] = None
    prompt_override: Optional[str] = None
    model_id: Optional[str] = None
    model_note: Optional[str] = None

@app.put("/api/agents/{agent_id}")
def api_update_agent(agent_id: str, req: AgentUpdate):
    defs = _get_agent_defs()
    if agent_id not in defs:
        raise HTTPException(status_code=404, detail="Agent not found")
    agent = defs[agent_id]
    if req.name is not None: agent["name"] = req.name
    if req.name_zh is not None: agent["name_zh"] = req.name_zh
    if req.description is not None: agent["description"] = req.description
    if req.enabled is not None: agent["enabled"] = req.enabled
    if req.tools is not None: agent["tools"] = req.tools
    if req.skills is not None: agent["skills"] = req.skills
    if req.capabilities is not None: agent["capabilities"] = req.capabilities
    if req.prompt_override is not None: agent["prompt_override"] = req.prompt_override
    if req.model_id is not None: agent["model_id"] = req.model_id
    if req.model_note is not None: agent["model_note"] = req.model_note
    defs[agent_id] = agent
    _save_agent_defs(defs)
    _agents.clear()
    return {"ok": True, "agent": agent}

class AgentCreate(BaseModel):
    id: str
    name: str
    name_zh: str = ""
    role: str = "sub-agent"
    description: str = ""
    tools: list = []
    skills: list = []
    capabilities: list = []
    prompt_override: str = ""
    model_id: str = ""
    model_note: str = ""

@app.post("/api/agents")
def api_create_agent(req: AgentCreate):
    defs = _get_agent_defs()
    if req.id in defs:
        raise HTTPException(status_code=409, detail="Agent ID already exists")
    new_agent = {
        "id": req.id, "name": req.name, "name_zh": req.name_zh or req.name,
        "role": req.role, "enabled": True, "deletable": True,
        "description": req.description,
        "model_type": "sub", "model_note": req.model_note or "Claude Haiku 4.5",
        "model_id": req.model_id or "",
        "tools": req.tools, "skills": req.skills, "capabilities": req.capabilities,
        "prompt_override": req.prompt_override,
    }
    defs[req.id] = new_agent
    _save_agent_defs(defs)
    _agents.clear()
    return {"ok": True, "agent": new_agent}

@app.delete("/api/agents/{agent_id}")
def api_delete_agent(agent_id: str):
    defs = _get_agent_defs()
    if agent_id not in defs:
        raise HTTPException(status_code=404, detail="Agent not found")
    if not defs[agent_id].get("deletable", True):
        raise HTTPException(status_code=403, detail="Core agent cannot be deleted")
    del defs[agent_id]
    _save_agent_defs(defs)
    _agents.clear()
    return {"ok": True}

@app.post("/api/agents/reset")
def api_reset_agents():
    """清空所有 Agent 配置。"""
    defs = {}
    _save_agent_defs(defs)
    _agents.clear()
    return {"ok": True, "agents": []}


# ───────────────── Notification Channels API ─────────────────

_notification_channels = []

def _load_notification_channels():
    global _notification_channels
    saved = _load_config("notification_channels", {})
    _configs["notification_channels"] = saved
    _notification_channels = saved.get("channels", [])

def _save_notification_channels():
    data = {"channels": _notification_channels}
    _configs["notification_channels"] = data
    _save_config("notification_channels", data)

@app.get("/api/notification-channels")
def api_get_channels():
    _load_notification_channels()
    return {"channels": _notification_channels}

class ChannelCreate(BaseModel):
    type: str  # wecom, dingtalk, feishu, ses
    name: str = ""
    webhook_url: str = ""
    secret: str = ""
    # SES fields
    recipients: str = ""
    sender: str = "noreply@agentic-data.aws"
    region: str = "us-east-1"

@app.post("/api/notification-channels")
def api_create_channel(req: ChannelCreate):
    _load_notification_channels()
    channel = {
        "id": f"ch-{uuid.uuid4().hex[:8]}",
        "type": req.type,
        "name": req.name or {"wecom": "企业微信", "dingtalk": "钉钉", "feishu": "飞书", "ses": "邮件"}.get(req.type, req.type),
        "enabled": True,
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    if req.type in ("wecom", "dingtalk", "feishu"):
        channel["webhook_url"] = req.webhook_url
        if req.secret:
            channel["secret"] = req.secret
    elif req.type == "ses":
        channel["recipients"] = req.recipients
        channel["sender"] = req.sender
        channel["region"] = req.region
    _notification_channels.append(channel)
    _save_notification_channels()
    return {"ok": True, "channel": channel}

@app.delete("/api/notification-channels/{channel_id}")
def api_delete_channel(channel_id: str):
    _load_notification_channels()
    _notification_channels[:] = [c for c in _notification_channels if c["id"] != channel_id]
    _save_notification_channels()
    return {"ok": True}

@app.put("/api/notification-channels/{channel_id}/toggle")
def api_toggle_channel(channel_id: str):
    _load_notification_channels()
    for c in _notification_channels:
        if c["id"] == channel_id:
            c["enabled"] = not c["enabled"]
            break
    _save_notification_channels()
    return {"ok": True}

@app.post("/api/notification-channels/{channel_id}/test")
def api_test_channel(channel_id: str):
    """Send a test message to verify channel configuration."""
    _load_notification_channels()
    channel = next((c for c in _notification_channels if c["id"] == channel_id), None)
    if not channel:
        raise HTTPException(status_code=404, detail="Channel not found")

    from agentic_core.notifications import send_notification
    test_title = "Agentic Data 连接测试"
    test_content = """**推送渠道测试成功！**

此消息由 Agentic Data 智能数据分析平台发送，用于验证推送渠道配置。

- 渠道类型: {type}
- 渠道名称: {name}
- 测试时间: {time}

如果您收到此消息，说明推送渠道已正确配置。""".format(
        type=channel["type"], name=channel["name"],
        time=datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    )

    result = send_notification(channel, test_title, test_content)
    return {"ok": result["success"], "result": result}



# ───────────────── Scheduled Reports API ─────────────────

import threading
from datetime import datetime, timezone, timedelta

# In-memory store (persisted to DynamoDB config)
_scheduled_reports = []
_report_history = []

def _load_scheduled_reports():
    global _scheduled_reports, _report_history
    saved = _load_config("scheduled_reports", {})
    _configs["scheduled_reports"] = saved
    _scheduled_reports = saved.get("schedules", [])
    _report_history = saved.get("history", [])

def _save_scheduled_reports():
    data = {
        "schedules": _scheduled_reports,
        "history": _report_history[-50:],  # Keep last 50
    }
    _configs["scheduled_reports"] = data
    _save_config("scheduled_reports", data)

# Default report templates — empty for new installations
# Users create their own templates from the admin UI
_REPORT_TEMPLATES = []

@app.get("/api/scheduled-reports")
def api_get_scheduled_reports():
    _load_scheduled_reports()
    return {
        "schedules": _scheduled_reports,
        "history": _report_history[-20:],
        "templates": _REPORT_TEMPLATES,
    }

class ScheduleCreate(BaseModel):
    template_id: str = ""
    name: str = ""
    query: str = ""
    cron: str = "daily_09"  # daily_09, daily_18, weekly_mon, weekly_fri, monthly_01
    channel_ids: list = []  # notification channel ids to push to
    recipients: list = []  # legacy
    enabled: bool = True

@app.post("/api/scheduled-reports")
def api_create_scheduled_report(req: ScheduleCreate):
    _load_scheduled_reports()
    # Build schedule from template or custom
    template = next((t for t in _REPORT_TEMPLATES if t["id"] == req.template_id), None)
    schedule = {
        "id": f"sr-{uuid.uuid4().hex[:8]}",
        "name": req.name or (template["name"] if template else "自定义报告"),
        "description": template["description"] if template else "",
        "query": req.query or (template["query"] if template else ""),
        "cron": req.cron,
        "cron_label": {"daily_09":"每天 09:00","daily_18":"每天 18:00","weekly_mon":"每周一 09:00","weekly_fri":"每周五 18:00","monthly_01":"每月1日 09:00"}.get(req.cron, req.cron),
        "channel_ids": req.channel_ids,
        "recipients": req.recipients,
        "enabled": req.enabled,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "last_run": None,
        "run_count": 0,
    }
    _scheduled_reports.append(schedule)
    _save_scheduled_reports()
    return {"ok": True, "schedule": schedule}

@app.delete("/api/scheduled-reports/{schedule_id}")
def api_delete_scheduled_report(schedule_id: str):
    _load_scheduled_reports()
    _scheduled_reports[:] = [s for s in _scheduled_reports if s["id"] != schedule_id]
    _save_scheduled_reports()
    return {"ok": True}

@app.put("/api/scheduled-reports/{schedule_id}/toggle")
def api_toggle_scheduled_report(schedule_id: str):
    _load_scheduled_reports()
    for s in _scheduled_reports:
        if s["id"] == schedule_id:
            s["enabled"] = not s["enabled"]
            break
    _save_scheduled_reports()
    return {"ok": True}

@app.post("/api/scheduled-reports/{schedule_id}/run")
async def api_run_scheduled_report(schedule_id: str, request: Request):
    """Manually trigger a scheduled report — streams SSE like /api/chat."""
    _load_scheduled_reports()
    schedule = next((s for s in _scheduled_reports if s["id"] == schedule_id), None)
    if not schedule:
        raise HTTPException(status_code=404, detail="Schedule not found")

    session_id = f"report-{schedule_id}-{uuid.uuid4().hex[:6]}"
    event_queue = queue.Queue()
    agent_state = {"done": False, "error": None, "steps": 0}

    def run_agent():
        try:
            from agentic_core import create_supervisor
            config = _configs.get("global", {})
            agent = create_supervisor(config)
            result = agent(schedule["query"])
            final_text = str(result)
            event_queue.put(("text", final_text))

            # Push to notification channels
            push_results = []
            channel_ids = schedule.get("channel_ids", [])
            if channel_ids:
                _load_notification_channels()
                selected_channels = [c for c in _notification_channels if c["id"] in channel_ids and c.get("enabled", True)]
                if selected_channels:
                    from agentic_core.notifications import send_to_channels
                    push_results = send_to_channels(selected_channels, schedule["name"], final_text)
                    event_queue.put(("push", push_results))

            # Save to history
            _load_scheduled_reports()
            push_summary = [{"channel": r["channel"], "success": r["success"], "message": r.get("message","")} for r in push_results]
            history_entry = {
                "id": f"rh-{uuid.uuid4().hex[:8]}",
                "schedule_id": schedule_id,
                "schedule_name": schedule["name"],
                "content": final_text[:3000],
                "generated_at": datetime.now(timezone.utc).isoformat(),
                "push_results": push_summary,
                "status": "delivered" if all(r["success"] for r in push_results) else ("partial" if any(r["success"] for r in push_results) else ("generated" if not push_results else "failed")),
            }
            _report_history.append(history_entry)

            # Update schedule last_run
            for s in _scheduled_reports:
                if s["id"] == schedule_id:
                    s["last_run"] = datetime.now(timezone.utc).isoformat()
                    s["run_count"] = s.get("run_count", 0) + 1
            _save_scheduled_reports()

            event_queue.put(("done", history_entry))
        except Exception as e:
            event_queue.put(("error", str(e)))

    threading.Thread(target=run_agent, daemon=True).start()

    from starlette.responses import StreamingResponse
    def event_stream():
        while True:
            try:
                event = event_queue.get(timeout=120)
                if event[0] == "text":
                    import json
                    yield f"data: {json.dumps({'type':'text','content':event[1]}, ensure_ascii=False)}\n\n"
                elif event[0] == "push":
                    import json
                    yield f"data: {json.dumps({'type':'push','results':event[1]}, ensure_ascii=False)}\n\n"
                elif event[0] == "done":
                    import json
                    yield f"data: {json.dumps({'type':'done','report':event[1]}, ensure_ascii=False)}\n\n"
                    break
                elif event[0] == "error":
                    import json
                    yield f"data: {json.dumps({'type':'error','message':event[1]}, ensure_ascii=False)}\n\n"
                    break
            except:
                break

    return StreamingResponse(event_stream(), media_type="text/event-stream",
                           headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

# Initialize on startup
_load_scheduled_reports()


# ═══════ Agent Playground — 调试沙盒 ═══════

@app.post("/api/playground/run")
def api_playground_run(req: dict):
    """Run a single Agent in isolation with full trace capture.
    Body: {agent_id, question, prompt_override?, model_id?, tools?, temperature?}
    Returns: {answer, trace, tokens, latency_ms, model}
    """
    import time as _t
    agent_id = req.get("agent_id", "data_analyst")
    question = req.get("question", "")
    if not question:
        return JSONResponse({"error": "question required"}, 400)

    from agentic_core.agents import _resolve_model, _run_sub_agent, set_trace_callback
    from agentic_core.agents import DEFAULT_SYSTEM_PROMPT, DEFAULT_DATA_ANALYST_PROMPT
    from agentic_core.dynamic_context import build_data_analyst_prompt, build_supervisor_data_section
    from agentic_core.semantic_layer import get_semantic_context
    from agentic_core.tools import (
        semantic_query, get_data_catalog, pg_query, nl2sql_query,
    )
    from strands import Agent
    from strands.agent.conversation_manager import SlidingWindowConversationManager
    from strands.hooks import BeforeToolCallEvent, AfterToolCallEvent

    # Resolve prompt
    default_prompts = {
        "supervisor": DEFAULT_SYSTEM_PROMPT.replace("{{DYNAMIC_DATA_SECTION}}", build_supervisor_data_section()),
        "data_analyst": build_data_analyst_prompt(),
    }
    prompt = req.get("prompt_override") or default_prompts.get(agent_id, default_prompts["data_analyst"])

    # Augment DataAnalyst prompt with schema context
    if agent_id == "data_analyst":
        try:
            schema_info = get_data_catalog()
            sem_ctx = get_semantic_context(question)
            prompt += f"\n\n## 数据字典\n{schema_info[:1500]}\n\n{sem_ctx}"
        except Exception as _e:
            print(f"[WARN] silent exception: {_e}")

    # Resolve tools
    tool_map = {
        "supervisor": [get_data_catalog, semantic_query, nl2sql_query, pg_query],
        "data_analyst": [semantic_query, nl2sql_query, pg_query, get_data_catalog],
    }
    tools = tool_map.get(agent_id, tool_map["data_analyst"])

    # Resolve model
    from config import DEFAULT_SUPERVISOR_MODEL, DEFAULT_SUB_AGENT_MODEL
    model_id = req.get("model_id") or (DEFAULT_SUPERVISOR_MODEL if agent_id == "supervisor" else DEFAULT_SUB_AGENT_MODEL)
    temperature = req.get("temperature", 0.1)
    from agentic_core.agents import _resolve_model
    model = _resolve_model(model_id, max_tokens=4096, guardrail_enabled=False)

    # Capture trace
    trace_events = []
    tool_times = {}
    token_counts = {"input": 0, "output": 0}

    def on_before(event: BeforeToolCallEvent):
        name = event.tool_use.get("name", "") if isinstance(event.tool_use, dict) else getattr(event.tool_use, "name", "")
        tool_input = event.tool_use.get("input", {}) if isinstance(event.tool_use, dict) else {}
        tool_times[name] = _t.time()
        trace_events.append({"type": "tool_call", "tool": name, "input": str(tool_input)[:500], "ts": _t.time()})

    def on_after(event: AfterToolCallEvent):
        name = event.tool_use.get("name", "") if isinstance(event.tool_use, dict) else getattr(event.tool_use, "name", "")
        ms = int((_t.time() - tool_times.get(name, _t.time())) * 1000)
        result = str(event.tool_result)[:1000] if hasattr(event, "tool_result") else ""
        trace_events.append({"type": "tool_result", "tool": name, "latency_ms": ms, "result_preview": result[:500], "ts": _t.time()})

    agent = Agent(
        model=model, tools=tools, system_prompt=prompt,
        conversation_manager=SlidingWindowConversationManager(window_size=4),
    )
    agent.add_hook(on_before, BeforeToolCallEvent)
    agent.add_hook(on_after, AfterToolCallEvent)

    start = _t.time()
    try:
        result = agent(question)
        answer = str(result)
    except Exception as e:
        answer = f"Agent 执行失败: {str(e)[:300]}"

    latency_ms = int((_t.time() - start) * 1000)

    # Extract token usage from agent metrics if available
    try:
        if hasattr(result, 'metrics') and result.metrics and result.metrics.agent_invocations:
            inv = result.metrics.agent_invocations[-1]
            u = inv.usage
            token_counts["input"] = u.get("inputTokens", 0)
            token_counts["output"] = u.get("outputTokens", 0)
    except Exception as _e:
        print(f"[WARN] playground token extraction: {_e}")

    return {
        "answer": answer,
        "trace": trace_events,
        "tokens": token_counts,
        "latency_ms": latency_ms,
        "model": model_id,
        "agent_id": agent_id,
        "question": question,
    }


# ═══════ Prompt Version Management ═══════

@app.get("/api/agents/{agent_id}/prompt-history")
def api_prompt_history(agent_id: str):
    """Get prompt edit history for an agent."""
    key = f"prompt_history:{agent_id}"
    try:
        resp = _ddb.Table(CONFIG_TABLE).get_item(Key={"config_key": key})
        history = json.loads(resp.get("Item", {}).get("data", "[]"))
    except:
        history = []
    return {"agent_id": agent_id, "history": history}


@app.post("/api/agents/{agent_id}/prompt-history")
def api_save_prompt_version(agent_id: str, req: dict):
    """Save a prompt version snapshot."""
    prompt_text = req.get("prompt", "")
    label = req.get("label", "")
    if not prompt_text:
        return JSONResponse({"error": "prompt required"}, 400)

    key = f"prompt_history:{agent_id}"
    try:
        resp = _ddb.Table(CONFIG_TABLE).get_item(Key={"config_key": key})
        history = json.loads(resp.get("Item", {}).get("data", "[]"))
    except:
        history = []

    import hashlib
    version = {
        "version": len(history) + 1,
        "label": label or f"v{len(history)+1}",
        "prompt": prompt_text,
        "char_count": len(prompt_text),
        "hash": hashlib.md5(prompt_text.encode(), usedforsecurity=False).hexdigest()[:8],
        "saved_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "saved_by": _get_current_user_email() or "admin",
    }
    history.append(version)
    # Keep last 20 versions
    history = history[-20:]

    _ddb.Table(CONFIG_TABLE).put_item(Item={"config_key": key, "data": json.dumps(history, ensure_ascii=False), "updated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())})
    return {"ok": True, "version": version}


@app.post("/api/agents/{agent_id}/prompt-rollback")
def api_prompt_rollback(agent_id: str, req: dict):
    """Rollback agent prompt to a specific version."""
    version_num = req.get("version", 0)
    key = f"prompt_history:{agent_id}"
    try:
        resp = _ddb.Table(CONFIG_TABLE).get_item(Key={"config_key": key})
        history = json.loads(resp.get("Item", {}).get("data", "[]"))
    except:
        return JSONResponse({"error": "no history found"}, 404)

    target = next((v for v in history if v["version"] == version_num), None)
    if not target:
        return JSONResponse({"error": f"version {version_num} not found"}, 404)

    # Apply to agent def
    defs = _get_agent_defs()
    if agent_id in defs:
        defs[agent_id]["prompt_override"] = target["prompt"]
        _save_agent_defs(defs)
        _agents.clear()

    return {"ok": True, "rolled_back_to": version_num, "prompt_preview": target["prompt"][:200]}


def _get_current_user_email():
    """Try to get current user email from request context."""
    try:
        from starlette.requests import Request
        # This is a simplified version; in production use request context
        return "admin"
    except:
        return "admin"


# ═══════ Test Cases — Agent 回归测试 ═══════

@app.get("/api/agents/{agent_id}/test-cases")
def api_list_test_cases(agent_id: str):
    key = f"test_cases:{agent_id}"
    try:
        resp = _ddb.Table(CONFIG_TABLE).get_item(Key={"config_key": key})
        cases = json.loads(resp.get("Item", {}).get("data", "[]"))
    except:
        cases = []
    return {"agent_id": agent_id, "test_cases": cases, "count": len(cases)}


@app.post("/api/agents/{agent_id}/test-cases")
def api_add_test_case(agent_id: str, req: dict):
    question = req.get("question", "")
    expected_answer = req.get("expected_answer", "")
    tags = req.get("tags", [])
    if not question:
        return JSONResponse({"error": "question required"}, 400)

    key = f"test_cases:{agent_id}"
    try:
        resp = _ddb.Table(CONFIG_TABLE).get_item(Key={"config_key": key})
        cases = json.loads(resp.get("Item", {}).get("data", "[]"))
    except:
        cases = []

    case = {
        "id": f"tc-{uuid.uuid4().hex[:8]}",
        "question": question,
        "expected_answer": expected_answer,
        "tags": tags,
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    cases.append(case)
    _ddb.Table(CONFIG_TABLE).put_item(Item={"config_key": key, "data": json.dumps(cases, ensure_ascii=False), "updated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())})
    return {"ok": True, "test_case": case}


@app.delete("/api/agents/{agent_id}/test-cases/{case_id}")
def api_delete_test_case(agent_id: str, case_id: str):
    key = f"test_cases:{agent_id}"
    try:
        resp = _ddb.Table(CONFIG_TABLE).get_item(Key={"config_key": key})
        cases = json.loads(resp.get("Item", {}).get("data", "[]"))
    except:
        cases = []
    cases = [c for c in cases if c["id"] != case_id]
    _ddb.Table(CONFIG_TABLE).put_item(Item={"config_key": key, "data": json.dumps(cases, ensure_ascii=False), "updated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())})
    return {"ok": True}


@app.post("/api/agents/{agent_id}/test-run")
def api_run_test_cases(agent_id: str, req: dict = {}):
    """Run all test cases for an agent, return pass/fail results."""
    key = f"test_cases:{agent_id}"
    try:
        resp = _ddb.Table(CONFIG_TABLE).get_item(Key={"config_key": key})
        cases = json.loads(resp.get("Item", {}).get("data", "[]"))
    except:
        cases = []

    if not cases:
        return {"results": [], "summary": {"total": 0, "passed": 0, "failed": 0}}

    results = []
    for case in cases[:10]:  # Limit to 10 to avoid timeout
        try:
            run_result = api_playground_run({
                "agent_id": agent_id,
                "question": case["question"],
            })
            # Keyword matching: check if expected keywords appear in answer
            answer = run_result.get("answer", "")
            expected = case.get("expected_answer", "")
            if expected:
                # Split by comma, space, semicolons; keep all non-empty terms (supports Chinese)
                import re as _re
                terms = [t.strip() for t in _re.split(r'[,;，；\s]+', expected) if t.strip()]
                if terms:
                    matched = sum(1 for t in terms if t in answer)
                    score = matched / len(terms)
                    passed = score >= 0.3  # At least 30% keywords found
                else:
                    passed = len(answer) > 20
                    score = 1.0 if passed else 0.0
            else:
                passed = len(answer) > 20  # At least got a non-trivial answer
                score = 1.0 if passed else 0.0

            results.append({
                "case_id": case["id"],
                "question": case["question"],
                "expected": expected[:200] if expected else "",
                "actual": answer[:500],
                "passed": passed,
                "score": round(score, 2),
                "latency_ms": run_result.get("latency_ms", 0),
                "tokens": run_result.get("tokens", {}),
            })
        except Exception as e:
            results.append({
                "case_id": case["id"],
                "question": case["question"],
                "passed": False,
                "error": str(e)[:200],
            })

    passed = sum(1 for r in results if r.get("passed"))
    return {
        "results": results,
        "summary": {"total": len(results), "passed": passed, "failed": len(results) - passed},
    }


# ═══════ Agent Run Logs — 运行审计日志 ═══════



# ═══════ Prompt 模板市场 ═══════

# Built-in prompt templates by category
_PROMPT_TEMPLATES = [
    # ── 数据分析类 ──
    {
        "id": "tpl-data-analyst",
        "name": "数据分析师",
        "name_en": "Data Analyst",
        "category": "数据分析",
        "description": "通用数据分析 Agent，擅长 SQL 查询、统计分析、趋势洞察",
        "icon": "chart",
        "tags": ["SQL", "统计", "可视化"],
        "recommended_tools": ["semantic_query", "nl2sql_query", "pg_query", "get_data_catalog"],
        "recommended_model": "Claude Haiku 4.5",
        "prompt": """你是一个专业的数据分析师。你的职责是：

## 核心能力
1. **精确查询** — 使用 SQL 和 ChatBI 工具从数据源获取真实数据，绝不编造数据
2. **统计分析** — 对查询结果进行描述性统计、对比分析、趋势分析
3. **洞察总结** — 用业务语言总结发现，给出可操作的建议

## 工作流程
1. 理解用户问题，确定需要查询的数据源和字段
2. 构造精确的查询（优先使用语义层，其次 SQL）
3. 分析查询结果，计算关键指标
4. 用清晰的格式呈现结果（表格、排名、对比）
5. 给出业务洞察和建议

## 输出规范
- 所有数字必须来自真实查询结果
- 金额统一使用人民币(¥)，保留整数
- 百分比保留1位小数
- 排名类结果用有序列表呈现
- 每次回答结尾给出 1-2 条洞察""",
    },
    {
        "id": "tpl-cross-source",
        "name": "跨源分析师",
        "name_en": "Cross-Source Analyst",
        "category": "数据分析",
        "description": "擅长跨多个数据源进行关联分析和综合报告",
        "icon": "merge",
        "tags": ["跨源", "关联分析", "综合报告"],
        "recommended_tools": ["chatbi_query", "chatbi_cross_analysis", "sql_db_query", "semantic_query", "get_data_catalog"],
        "recommended_model": "Claude Sonnet 4.6",
        "prompt": """你是一个跨数据源分析专家。你可以同时查询多个数据源，进行关联分析。

## 核心能力
1. **多源查询** — 从 S3 数据集、RDS 数据库、DynamoDB 等多个源获取数据
2. **关联分析** — 将不同数据源的结果按公共维度（如车型、城市、时间）进行关联
3. **综合报告** — 生成跨数据源的综合分析报告

## 分析模式
- **对比模式**: 将两个数据源的同一指标进行对比
- **补充模式**: 用一个数据源的数据补充另一个数据源缺失的维度
- **验证模式**: 用多个数据源交叉验证同一结论

## 输出格式
- 标注每条数据的来源（数据集名称）
- 跨源关联时说明关联键
- 结论中标注置信度（高/中/低）""",
    },
    # ── 业务场景类 ──
    {
        "id": "tpl-sales-advisor",
        "name": "销售顾问",
        "name_en": "Sales Advisor",
        "category": "业务场景",
        "description": "销售数据分析与策略建议，适合销售团队使用",
        "icon": "trending",
        "tags": ["销售", "营收", "客户"],
        "recommended_tools": ["chatbi_query", "sql_db_query", "semantic_query"],
        "recommended_model": "Claude Haiku 4.5",
        "prompt": """你是一个资深销售数据顾问。帮助销售团队理解业绩、发现机会、制定策略。

## 关注指标
- 销量、营收、客单价、转化率
- 区域/车型/渠道/销售员维度的业绩对比
- 同比/环比增长趋势
- 客户画像与购买偏好

## 分析框架
1. **现状**: 当前关键指标的数值和排名
2. **趋势**: 时间维度的变化方向
3. **对比**: 与目标/历史/同行的差距
4. **归因**: 数据变化的可能原因
5. **建议**: 可操作的下一步行动

## 沟通风格
- 直接给结论，再补数据支撑
- 用业务语言，避免技术术语
- 坏消息先说，好消息后说（管理预期）
- 每次给出 2-3 条可执行建议""",
    },
    {
        "id": "tpl-quality-inspector",
        "name": "质量分析师",
        "name_en": "Quality Inspector",
        "category": "业务场景",
        "description": "产品质量和安全数据分析，适合质量/安全团队",
        "icon": "shield",
        "tags": ["质量", "安全", "缺陷"],
        "recommended_tools": ["semantic_query", "nl2sql_query", "pg_query", "get_data_catalog"],
        "recommended_model": "Claude Haiku 4.5",
        "prompt": """你是一个严谨的产品质量与安全分析师。负责监控质量指标、发现异常、评估风险。

## 核心职责
1. **质量监控** — 追踪缺陷率、返修率、客户投诉等质量指标
2. **异常检测** — 识别异常数据模式，评估严重程度
3. **根因分析** — 从数据中推断质量问题的可能根因
4. **风险评估** — 评估风险等级，建议应对措施

## 分析原则
- 数据驱动，不做主观臆断
- 区分"关联"和"因果"
- 按严重程度排序（安全 > 功能 > 外观）
- 给出量化的风险评估

## 输出格式
- 问题描述 + 影响范围 + 严重等级(P0-P3)
- 相关数据证据（含数据源标注）
- 建议行动 + 优先级""",
    },
    {
        "id": "tpl-customer-insight",
        "name": "客户洞察",
        "name_en": "Customer Insight",
        "category": "业务场景",
        "description": "客户行为分析、满意度追踪、流失预警",
        "icon": "users",
        "tags": ["NPS", "客户画像", "满意度"],
        "recommended_tools": ["chatbi_query", "sql_db_query", "semantic_query", "get_data_catalog"],
        "recommended_model": "Claude Haiku 4.5",
        "prompt": """你是客户洞察分析专家。通过数据理解客户行为、满意度和需求。

## 分析维度
- **满意度**: NPS 分布、评分趋势、关键驱动因素
- **行为**: 购买偏好、使用频率、功能偏好
- **画像**: 客户细分、高价值客户特征、流失预警信号
- **生命周期**: 获客 → 转化 → 留存 → 增购

## 方法论
1. 先看全局指标（NPS、满意度均值）
2. 再按维度下钻（城市、车型、购买时间）
3. 找出异常群体（特别高/特别低的细分）
4. 分析差异原因
5. 给出改善建议

## 输出规范
- NPS = 推荐者(9-10) - 贬损者(0-6) 的百分比
- 客户分群用清晰的标签命名
- 建议按短期(1周)/中期(1月)/长期(1季)分类""",
    },
    # ── 运营管理类 ──
    {
        "id": "tpl-ops-monitor",
        "name": "运营监控",
        "name_en": "Operations Monitor",
        "category": "运营管理",
        "description": "运营指标监控、异常告警、日报周报生成",
        "icon": "activity",
        "tags": ["监控", "告警", "报告"],
        "recommended_tools": ["semantic_query", "nl2sql_query", "pg_query", "get_data_catalog"],
        "recommended_model": "Claude Haiku 4.5",
        "prompt": """你是运营监控 Agent。负责实时监控关键运营指标，发现异常并生成报告。

## 监控指标
- 业务指标: 日活、订单量、营收、转化率
- 质量指标: 故障率、响应时间、解决率
- 效率指标: 人效、设备利用率、周转率

## 工作模式
### 即时查询
- 用户问什么查什么，快速返回数据

### 异常分析
- 发现指标偏离基线时，自动下钻分析
- 给出: 异常指标 + 偏离幅度 + 开始时间 + 影响范围

### 报告生成
- 日报: 当日关键指标 + 环比变化 + 异常事项
- 周报: 本周趋势 + TOP问题 + 下周关注点

## 输出格式
- 关键数字加粗
- 上升用 ↑，下降用 ↓，持平用 →
- 异常用 [!] 标注""",
    },
    {
        "id": "tpl-report-writer",
        "name": "报告撰写",
        "name_en": "Report Writer",
        "category": "运营管理",
        "description": "自动生成结构化分析报告，支持多种格式",
        "icon": "file-text",
        "tags": ["报告", "摘要", "PPT"],
        "recommended_tools": ["chatbi_query", "chatbi_cross_analysis", "sql_db_query", "get_data_catalog", "semantic_query"],
        "recommended_model": "Claude Sonnet 4.6",
        "prompt": """你是一个专业的数据报告撰写 Agent。将数据分析结果转化为结构化的商业报告。

## 报告结构
1. **摘要** — 3-5 句话概括核心发现
2. **关键指标** — 表格展示 KPI 及其变化
3. **详细分析** — 按主题分节，每节有数据+解读
4. **趋势与预测** — 基于历史数据的趋势判断
5. **建议与行动项** — 可执行的下一步

## 写作规范
- 标题简洁有力，不超过 10 个字
- 每段不超过 4 句话
- 数据精确，注明来源
- 结论先行，论据跟进
- 避免模糊词汇（"大约"→ 给具体数字）

## 格式要求
- 使用 Markdown 格式
- 表格对齐
- 重要数字加粗
- 建议用有序列表""",
    },
    # ── 通用类 ──
    {
        "id": "tpl-supervisor",
        "name": "调度中枢",
        "name_en": "Supervisor",
        "category": "通用",
        "description": "多 Agent 协调调度，适合作为 Orchestrator 使用",
        "icon": "cpu",
        "tags": ["调度", "路由", "编排"],
        "recommended_tools": ["semantic_query", "nl2sql_query", "pg_query", "get_data_catalog", "get_data_catalog"],
        "recommended_model": "Claude Sonnet 4.6",
        "prompt": """你是多 Agent 系统的调度中枢（Supervisor）。你的职责是理解用户意图，将任务分发给最合适的专业 Agent。

## 路由规则
- 数据查询/分析类 → DataAnalystAgent
- 安全/事故/风险类 → SafetyAgent
- 驾驶行为/评分类 → BehaviorAgent
- 复杂问题可以串联多个 Agent

## 调度原则
1. **单一职责** — 每个子任务只分给一个 Agent
2. **最小权限** — 选工具最匹配的 Agent
3. **结果整合** — 汇总多个 Agent 的结果，给用户统一回复
4. **降级处理** — Agent 失败时，尝试自己直接处理或换一个 Agent

## 回复规范
- 综合所有 Agent 的结果后统一回答
- 不暴露内部调度过程（除非用户要求）
- 回答要完整、有条理""",
    },
    {
        "id": "tpl-minimal",
        "name": "极简问答",
        "name_en": "Minimal QA",
        "category": "通用",
        "description": "最简单的问答模板，适合快速原型",
        "icon": "zap",
        "tags": ["简单", "快速", "原型"],
        "recommended_tools": ["chatbi_query", "get_data_catalog"],
        "recommended_model": "Claude Haiku 4.5",
        "prompt": """你是一个数据问答助手。用户问什么，你查什么，简洁回答。

规则：
- 只用查询结果回答，不编造数据
- 回答控制在 3-5 句话
- 不确定就说"数据中未找到"
""",
    },
    {
        "id": "tpl-bilingual",
        "name": "双语分析师",
        "name_en": "Bilingual Analyst",
        "category": "通用",
        "description": "中英双语输出，适合跨国团队或外资企业",
        "icon": "globe",
        "tags": ["双语", "中英", "国际化"],
        "recommended_tools": ["chatbi_query", "sql_db_query", "semantic_query", "get_data_catalog"],
        "recommended_model": "Claude Sonnet 4.6",
        "prompt": """You are a bilingual data analyst. Respond in both Chinese and English.

## Output Format
Always structure your response as:

### 中文摘要
(Chinese summary of findings, 3-5 sentences)

### English Summary
(English summary of findings, 3-5 sentences)

### Data / 数据
(Tables and numbers — language-neutral)

### Insights / 洞察
(Bilingual insights and recommendations)

## Rules
- Query data using available tools — never fabricate numbers
- Use ¥ for CNY amounts, $ for USD
- Technical terms keep English (e.g., NPS, ROI, CAGR)
- Charts/tables use English headers for international readability""",
    },
]


@app.get("/api/prompt-templates")
def api_list_prompt_templates():
    """List all prompt templates grouped by category."""
    # Load custom templates from DynamoDB
    custom = []
    try:
        resp = _ddb.Table(CONFIG_TABLE).get_item(Key={"config_key": "custom_prompt_templates"})
        custom = json.loads(resp.get("Item", {}).get("data", "[]"))
    except Exception as _e:
        print(f"[WARN] silent exception: {_e}")

    all_templates = _PROMPT_TEMPLATES + custom
    categories = {}
    for t in all_templates:
        cat = t.get("category", "其他")
        if cat not in categories:
            categories[cat] = []
        categories[cat].append(t)

    return {
        "templates": all_templates,
        "categories": categories,
        "total": len(all_templates),
    }


@app.get("/api/prompt-templates/{template_id}")
def api_get_prompt_template(template_id: str):
    all_templates = _PROMPT_TEMPLATES[:]
    try:
        resp = _ddb.Table(CONFIG_TABLE).get_item(Key={"config_key": "custom_prompt_templates"})
        all_templates += json.loads(resp.get("Item", {}).get("data", "[]"))
    except Exception as _e:
        print(f"[WARN] silent exception: {_e}")
    tpl = next((t for t in all_templates if t["id"] == template_id), None)
    if not tpl:
        raise HTTPException(status_code=404, detail="Template not found")
    return tpl


@app.post("/api/prompt-templates")
def api_create_prompt_template(req: dict):
    """Save a custom prompt template."""
    required = ["name", "category", "prompt"]
    for f in required:
        if not req.get(f):
            return JSONResponse({"error": f"{f} required"}, 400)

    tpl = {
        "id": f"tpl-custom-{uuid.uuid4().hex[:8]}",
        "name": req["name"],
        "name_en": req.get("name_en", req["name"]),
        "category": req["category"],
        "description": req.get("description", ""),
        "icon": req.get("icon", "star"),
        "tags": req.get("tags", []),
        "recommended_tools": req.get("recommended_tools", []),
        "recommended_model": req.get("recommended_model", "Claude Haiku 4.5"),
        "prompt": req["prompt"],
        "custom": True,
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }

    try:
        resp = _ddb.Table(CONFIG_TABLE).get_item(Key={"config_key": "custom_prompt_templates"})
        custom = json.loads(resp.get("Item", {}).get("data", "[]"))
    except:
        custom = []
    custom.append(tpl)
    _ddb.Table(CONFIG_TABLE).put_item(Item={"config_key": "custom_prompt_templates", "data": json.dumps(custom, ensure_ascii=False), "updated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())})
    return {"ok": True, "template": tpl}


@app.delete("/api/prompt-templates/{template_id}")
def api_delete_prompt_template(template_id: str):
    """Delete a custom prompt template (built-in templates cannot be deleted)."""
    # Check if it's a built-in template
    if any(t["id"] == template_id for t in _PROMPT_TEMPLATES):
        return JSONResponse({"error": "内置模板不可删除"}, 400)

    try:
        resp = _ddb.Table(CONFIG_TABLE).get_item(Key={"config_key": "custom_prompt_templates"})
        custom = json.loads(resp.get("Item", {}).get("data", "[]"))
    except:
        custom = []
    custom = [t for t in custom if t["id"] != template_id]
    _ddb.Table(CONFIG_TABLE).put_item(Item={"config_key": "custom_prompt_templates", "data": json.dumps(custom, ensure_ascii=False), "updated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())})
    return {"ok": True}


@app.post("/api/prompt-templates/{template_id}/apply/{agent_id}")
def api_apply_template_to_agent(template_id: str, agent_id: str):
    """Apply a prompt template to an agent."""
    # Find template
    all_templates = _PROMPT_TEMPLATES[:]
    try:
        resp = _ddb.Table(CONFIG_TABLE).get_item(Key={"config_key": "custom_prompt_templates"})
        all_templates += json.loads(resp.get("Item", {}).get("data", "[]"))
    except Exception as _e:
        print(f"[WARN] silent exception: {_e}")
    tpl = next((t for t in all_templates if t["id"] == template_id), None)
    if not tpl:
        raise HTTPException(status_code=404, detail="Template not found")

    # Apply to agent
    defs = _get_agent_defs()
    if agent_id not in defs:
        raise HTTPException(status_code=404, detail="Agent not found")

    defs[agent_id]["prompt_override"] = tpl["prompt"]
    if tpl.get("recommended_tools"):
        defs[agent_id]["tools"] = tpl["recommended_tools"]
    _save_agent_defs(defs)
    _agents.clear()

    return {"ok": True, "agent_id": agent_id, "template_applied": tpl["name"]}


# ═══════ Agent 运行指标大盘 ═══════

@app.get("/api/dashboard/agent-metrics")
def api_agent_metrics(days: int = 7):
    """Aggregate agent metrics for dashboard charts."""
    from collections import defaultdict
    from decimal import Decimal

    # Scan cost table
    try:
        resp = _ddb.Table(COST_TABLE).scan(Limit=500)
        items = resp.get("Items", [])
        while "LastEvaluatedKey" in resp and len(items) < 2000:
            resp = _ddb.Table(COST_TABLE).scan(ExclusiveStartKey=resp["LastEvaluatedKey"], Limit=500)
            items.extend(resp.get("Items", []))
    except Exception as e:
        return {"error": str(e)[:200]}

    # Parse
    records = []
    for item in items:
        try:
            records.append({
                "ts": item.get("timestamp", ""),
                "date": item.get("date", ""),
                "model": item.get("model", "unknown"),
                "input_tokens": int(item.get("input_tokens", 0) if not isinstance(item.get("input_tokens"), Decimal) else item.get("input_tokens", 0)),
                "output_tokens": int(item.get("output_tokens", 0) if not isinstance(item.get("output_tokens"), Decimal) else item.get("output_tokens", 0)),
                "cost": float(item.get("cost", 0) if not isinstance(item.get("cost"), Decimal) else item.get("cost", 0)),
                "latency_ms": int(item.get("latency_ms", 0) or (float(item.get("duration", 0)) * 1000) or 0),
                "agent_id": item.get("agent", "") or item.get("agent_id", ""),
            })
        except Exception as _e:
            print(f"[WARN] silent exception: {_e}")

    if not records:
        return {"kpi": {}, "daily": [], "model_dist": [], "token_trend": [], "total_records": 0}

    # Filter by date range
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%d")
    recent = [r for r in records if r["date"] >= cutoff] if cutoff else records
    if not recent:
        recent = records  # fallback to all data

    # KPI cards
    total_calls = len(recent)
    total_cost = sum(r["cost"] for r in recent)
    total_input = sum(r["input_tokens"] for r in recent)
    total_output = sum(r["output_tokens"] for r in recent)
    latencies = [r["latency_ms"] for r in recent if r["latency_ms"] > 0]
    avg_latency = int(sum(latencies) / len(latencies)) if latencies else 0
    non_zero = [r for r in recent if not (r.get("error") and str(r["error"]).strip())]
    success_rate = round(len(non_zero) / max(total_calls, 1) * 100, 1)

    # Today's stats
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    today_records = [r for r in recent if r["date"] == today]
    today_calls = len(today_records)
    today_cost = sum(r["cost"] for r in today_records)

    kpi = {
        "total_calls": total_calls,
        "today_calls": today_calls,
        "total_cost": round(total_cost, 4),
        "today_cost": round(today_cost, 4),
        "total_tokens": total_input + total_output,
        "avg_latency_ms": avg_latency,
        "success_rate": success_rate,
        "avg_cost_per_call": round(total_cost / max(total_calls, 1), 4),
    }

    # Daily trend (calls + cost + tokens)
    daily_agg = defaultdict(lambda: {"calls": 0, "cost": 0.0, "input": 0, "output": 0})
    for r in recent:
        d = r["date"]
        daily_agg[d]["calls"] += 1
        daily_agg[d]["cost"] += r["cost"]
        daily_agg[d]["input"] += r["input_tokens"]
        daily_agg[d]["output"] += r["output_tokens"]
    daily = sorted([{"date": k, **v} for k, v in daily_agg.items()], key=lambda x: x["date"])

    # Model distribution
    model_agg = defaultdict(lambda: {"calls": 0, "cost": 0.0, "tokens": 0})
    for r in recent:
        m = r["model"].split(".")[-1] if "." in r["model"] else r["model"]
        model_agg[m]["calls"] += 1
        model_agg[m]["cost"] += r["cost"]
        model_agg[m]["tokens"] += r["input_tokens"] + r["output_tokens"]
    model_dist = sorted([{"model": k, **v} for k, v in model_agg.items()], key=lambda x: -x["calls"])

    # Hourly distribution (for today or most recent day with data)
    target_day = today if today_records else (daily[-1]["date"] if daily else today)
    hourly_agg = defaultdict(int)
    for r in recent:
        if r["date"] == target_day and r["ts"]:
            try:
                h = int(r["ts"][11:13])
                hourly_agg[h] += 1
            except Exception as _e:
                print(f"[WARN] silent exception: {_e}")
    hourly = [{"hour": h, "calls": hourly_agg.get(h, 0)} for h in range(24)]

    # Latency percentiles
    latency_dist = {}
    if latencies:
        sl = sorted(latencies)
        latency_dist = {
            "p50": sl[len(sl)//2],
            "p90": sl[int(len(sl)*0.9)],
            "p99": sl[int(len(sl)*0.99)],
            "max": sl[-1],
            "min": sl[0],
        }

    # Per-agent breakdown
    agent_agg = defaultdict(lambda: {"calls": 0, "cost": 0.0, "tokens": 0, "today_calls": 0, "errors": 0})
    name_map = {"supervisor": "Supervisor", "data_analyst": "DataAnalyst", "DataAnalystAgent": "DataAnalyst"}
    for r in recent:
        aid = r.get("agent_id", "")
        if not aid:
            m = r["model"].lower()
            if "sonnet" in m or "opus" in m or "claude" in m: aid = "Supervisor"
            elif "haiku" in m or "glm" in m or "deepseek" in m or "qwen" in m: aid = "DataAnalyst"
            else: aid = "Supervisor"
        aid = name_map.get(aid, aid)
        a = agent_agg[aid]
        a["calls"] += 1
        a["cost"] += r["cost"]
        a["tokens"] += r["input_tokens"] + r["output_tokens"]
        err = r.get("error", "")
        if err and str(err).strip():
            a["errors"] += 1
        if r["date"] == today: a["today_calls"] += 1
    for aid, a in agent_agg.items():
        a["cost"] = round(a["cost"], 4)
        a["success_rate"] = round((a["calls"] - a["errors"]) / max(a["calls"], 1) * 100, 1)

    return {
        "kpi": kpi,
        "daily": daily,
        "model_dist": model_dist,
        "hourly": hourly,
        "latency_dist": latency_dist,
        "agents": dict(agent_agg),
        "total_records": len(records),
        "date_range": {"from": cutoff, "to": today},
    }


# ═══════ Trace Viewer ═══════

@app.get("/api/traces")
def api_list_traces(limit: int = 20, date: str = ""):
    """List recent traces."""
    if not date:
        date = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    try:
        resp = _ddb.Table(COST_TABLE).query(
            KeyConditionExpression=Key("date").eq(date),
            ScanIndexForward=False,
            Limit=limit * 3,  # Over-fetch since we filter
        )
        traces = []
        for item in resp.get("Items", []):
            if item.get("timestamp", "").startswith("trace_"):
                traces.append({
                    "trace_id": item.get("trace_id", ""),
                    "session_id": item.get("session_id", ""),
                    "question": item.get("question", ""),
                    "total_ms": int(item.get("total_ms", 0)),
                    "steps": int(item.get("steps", 0)),
                    "error": item.get("error", ""),
                    "timestamp": item.get("timestamp", "").replace("trace_", ""),
                })
        return {"traces": traces[:limit]}
    except Exception as e:
        return {"traces": [], "error": str(e)[:200]}

@app.get("/api/traces/{trace_id}")
def api_get_trace(trace_id: str):
    """Get full trace detail."""
    date = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    # Try today first, then scan recent days
    for days_back in range(7):
        d = (datetime.now(timezone.utc) - timedelta(days=days_back)).strftime("%Y-%m-%d")
        try:
            resp = _ddb.Table(COST_TABLE).get_item(Key={"date": d, "timestamp": f"trace_{trace_id}"})
            if "Item" in resp:
                item = resp["Item"]
                events = json.loads(item.get("events", "[]"))
                return {
                    "trace_id": trace_id,
                    "session_id": item.get("session_id", ""),
                    "question": item.get("question", ""),
                    "total_ms": int(item.get("total_ms", 0)),
                    "steps": int(item.get("steps", 0)),
                    "error": item.get("error", ""),
                    "events": events,
                }
        except Exception:
            continue
    return {"error": "Trace not found"}

# ═══════ Setup Wizard ═══════

@app.get("/api/setup/status")
def api_setup_status():
    """Returns setup wizard progress — which steps are complete."""
    # Step 1: Datasources
    ds_list = _load_config("custom_datasources") or []
    has_datasources = len(ds_list) > 0
    ds_count = len(ds_list)
    
    # Step 2: Semantic layer
    _load_semantic_custom()
    from agentic_core.semantic_layer import METRICS, DIMENSIONS, SYNONYMS
    has_semantic = len(METRICS) > 0
    semantic_count = {"metrics": len(METRICS), "dimensions": len(DIMENSIONS), "synonyms": len(SYNONYMS)}
    
    # Step 3: Agents configured (beyond defaults)
    agent_defs = _load_config("agent_definitions") or {}
    has_agents = bool(agent_defs.get("agents")) or bool(agent_defs.get("supervisor"))
    agent_count = len(agent_defs.get("agents", []))
    
    # Step 4: Scenarios
    scenarios = _load_config("scenarios") or {}
    has_scenarios = len(scenarios) > 0
    
    # Step 5: Tested (at least 1 chat session exists)
    try:
        resp = boto3.resource("dynamodb", region_name=REGION).Table(CHAT_TABLE).scan(Limit=1, Select="COUNT")
        has_tested = resp.get("Count", 0) > 0
    except Exception as _e:
        print(f"[WARN] setup status chat check: {_e}")
        has_tested = False
    
    steps = [
        {"id": "datasource", "name": "连接数据源", "done": has_datasources, "detail": f"{ds_count} 个数据源"},
        {"id": "semantic", "name": "语义层配置", "done": has_semantic, "detail": f"{semantic_count['metrics']} 指标, {semantic_count['dimensions']} 维度"},
        {"id": "agents", "name": "Agent 配置", "done": has_agents, "detail": f"{agent_count} 个 Agent"},
        {"id": "scenarios", "name": "场景配置", "done": has_scenarios, "detail": f"{len(scenarios)} 个场景"},
        {"id": "test", "name": "测试验证", "done": has_tested, "detail": "已有对话记录" if has_tested else "尚未测试"},
    ]
    
    completed = sum(1 for s in steps if s["done"])
    return {
        "steps": steps,
        "completed": completed,
        "total": len(steps),
        "all_done": completed == len(steps),
        "progress_pct": round(completed / len(steps) * 100),
    }




# ═══════ Config Impact Analysis ═══════

@app.post("/api/agents/{agent_id}/impact")
def api_agent_impact(agent_id: str):
    """Analyze the impact of modifying an agent's configuration."""
    import time as _t
    
    # 1. Which scenarios use this agent?
    scenarios = _load_config("scenarios") or {}
    affected_scenarios = []
    for sid, scfg in scenarios.items():
        if agent_id in scfg.get("agents", []):
            affected_scenarios.append({"id": sid, "name": scfg.get("name", sid)})
    
    # 2. Recent usage stats
    recent_calls = 0
    recent_days = 7
    try:
        cost_table = boto3.resource("dynamodb", region_name=REGION).Table(COST_TABLE)
        resp = cost_table.scan(Limit=500)
        for item in resp.get("Items", []):
            recent_calls += 1  # simplified: all calls relate to our 2-agent system
    except Exception as _e:
        print(f"[WARN] impact cost scan: {_e}")
    
    # 3. Related components
    agent_defs = _load_config("agent_definitions") or {}
    agents = agent_defs.get("agents", [])
    target = next((a for a in agents if a.get("id") == agent_id), None)
    
    related_tools = target.get("tools", []) if target else []
    related_skills = target.get("skills", []) if target else []
    
    # 4. Active sessions that might be affected
    active_sessions = len(_agents)  # sessions with cached agents
    
    return {
        "agent_id": agent_id,
        "affected_scenarios": affected_scenarios,
        "affected_scenario_count": len(affected_scenarios),
        "recent_calls_7d": recent_calls,
        "related_tools": related_tools,
        "related_skills": related_skills,
        "active_sessions": active_sessions,
        "warnings": [
            f"修改将影响 {len(affected_scenarios)} 个场景" if affected_scenarios else None,
            f"最近7天有 {recent_calls} 次调用" if recent_calls > 10 else None,
            f"有 {active_sessions} 个活跃会话将使用新配置" if active_sessions > 0 else None,
        ],
        "recommendation": "建议先在 Playground 测试" if recent_calls > 10 else "可直接应用",
    }

# ── Docstring inspection API ──
@app.get("/api/tools/{tool_name}/docstring")
def api_tool_docstring(tool_name: str):
    """Return the dynamically generated docstring for a tool."""
    from agentic_core.tools import _build_pg_docstring, _build_nl2sql_docstring, _build_snowflake_docstring
    from fastapi.responses import PlainTextResponse
    docstrings = {
        "pg_query": _build_pg_docstring,
        "nl2sql_query": _build_nl2sql_docstring,
        "snowflake_query": _build_snowflake_docstring,
    }
    builder = docstrings.get(tool_name)
    if not builder:
        return PlainTextResponse(f"Unknown tool: {tool_name}. Available: {list(docstrings.keys())}")
    try:
        text = builder()
        return PlainTextResponse(text, media_type="text/plain; charset=utf-8")
    except Exception as e:
        return {"error": str(e)}
