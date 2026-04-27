"""
Agent Registry — Agent 独立化存储与管理

Agent 是独立实体，有自己的模型/prompt/tools/skills/数据源。
DDB 存储: PK=AGENT#{id}

设计文档: docs/agent-scene-redesign-v2.md
"""
import json
import os
import time
import uuid
import copy
import boto3

CONFIG_TABLE = os.environ.get("AGENTIC_AUTO_CONFIG_TABLE", "agentic-auto-config")
REGION = os.environ.get("AGENTIC_AUTO_REGION") or os.environ.get("AWS_DEFAULT_REGION", "us-east-1")

_ddb = boto3.resource("dynamodb", region_name=REGION)

# ─── Agent Definition Schema ───

DEFAULT_AGENT_TEMPLATE = {
    "id": "",
    "name": "",
    "description": "",
    "type": "data_analyst",       # data_analyst | safety | custom
    
    # Model
    "model_id": "",
    "max_tokens": 4096,
    "temperature": 0.1,
    "fallback_model": "",
    
    # Prompt
    "system_prompt": "",
    
    # Capabilities
    "tools": [],                  # builtin tool names
    "mcp_servers": [],            # future: agent-level MCP
    "skills": [],                 # skill IDs
    
    # Data scope
    "datasources": [],            # datasource IDs
    
    # Behavior
    "max_tool_calls": 5,
    "timeout_seconds": 120,
    "output": {
        "require_chart": True,
        "require_drill": True,
        "language": "zh-CN",
    },
    
    # Quality
    "vqr_enabled": True,
    "insight_enabled": False,
    
    # Metadata
    "version": 1,
    "created_by": "system",
    "created_at": "",
    "updated_at": "",
    "tags": [],
}

# ─── CRUD ───

def _agent_key(agent_id: str) -> dict:
    return {"config_key": f"AGENT#{agent_id}"}


def list_agents() -> dict:
    """Return all agents as {id: definition}."""
    table = _ddb.Table(CONFIG_TABLE)
    # Scan for AGENT# keys
    resp = table.scan(
        FilterExpression="begins_with(config_key, :prefix)",
        ExpressionAttributeValues={":prefix": "AGENT#"},
    )
    agents = {}
    for item in resp.get("Items", []):
        data = json.loads(item.get("data", "{}"))
        if data.get("id"):
            agents[data["id"]] = data
    return agents


def get_agent_def(agent_id: str) -> dict | None:
    """Get a single agent definition."""
    table = _ddb.Table(CONFIG_TABLE)
    resp = table.get_item(Key=_agent_key(agent_id))
    item = resp.get("Item")
    if not item:
        return None
    return json.loads(item.get("data", "{}"))


def save_agent(agent_def: dict) -> dict:
    """Create or update an agent definition. Saves version history."""
    now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    
    if not agent_def.get("id"):
        agent_def["id"] = f"agent_{uuid.uuid4().hex[:8]}"
    
    # Merge with template for defaults
    merged = copy.deepcopy(DEFAULT_AGENT_TEMPLATE)
    merged.update(agent_def)
    
    # Bump version if updating existing
    existing = get_agent_def(merged["id"])
    if existing:
        merged["version"] = existing.get("version", 0) + 1
        merged["created_at"] = existing.get("created_at", now)
        # Save version history (keep last 20 versions)
        _save_version_history(merged["id"], existing)
    else:
        merged["created_at"] = now
    merged["updated_at"] = now
    
    # Persist
    table = _ddb.Table(CONFIG_TABLE)
    data_str = json.dumps(merged, ensure_ascii=False, default=str)
    table.put_item(Item={
        "config_key": f"AGENT#{merged['id']}",
        "data": data_str,
        "value": data_str,  # 兼容双字段
    })
    return merged


def delete_agent(agent_id: str) -> bool:
    """Delete an agent definition."""
    table = _ddb.Table(CONFIG_TABLE)
    table.delete_item(Key=_agent_key(agent_id))
    return True


# ─── Version History ───

def _save_version_history(agent_id: str, old_def: dict):
    """Save a version snapshot. Keep last 20 versions."""
    table = _ddb.Table(CONFIG_TABLE)
    key = f"AGENT_HISTORY#{agent_id}"
    
    try:
        resp = table.get_item(Key={"config_key": key})
        history = json.loads(resp.get("Item", {}).get("data", "[]"))
    except Exception:
        history = []
    
    # Add snapshot
    snapshot = {
        "version": old_def.get("version", 1),
        "saved_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "system_prompt": old_def.get("system_prompt", ""),
        "tools": old_def.get("tools", []),
        "datasources": old_def.get("datasources", []),
        "skills": old_def.get("skills", []),
        "model_id": old_def.get("model_id", ""),
        "temperature": old_def.get("temperature", 0.1),
    }
    history.append(snapshot)
    
    # Keep last 20
    if len(history) > 20:
        history = history[-20:]
    
    data_str = json.dumps(history, ensure_ascii=False, default=str)
    table.put_item(Item={"config_key": key, "data": data_str, "value": data_str})


def get_version_history(agent_id: str) -> list:
    """Get version history for an agent."""
    table = _ddb.Table(CONFIG_TABLE)
    resp = table.get_item(Key={"config_key": f"AGENT_HISTORY#{agent_id}"})
    return json.loads(resp.get("Item", {}).get("data", "[]"))


def rollback_agent(agent_id: str, target_version: int) -> dict | None:
    """Rollback an agent to a specific version."""
    history = get_version_history(agent_id)
    target = None
    for h in history:
        if h.get("version") == target_version:
            target = h
            break
    
    if not target:
        return None
    
    current = get_agent_def(agent_id)
    if not current:
        return None
    
    # Apply rollback fields
    for field in ["system_prompt", "tools", "datasources", "skills", "model_id", "temperature"]:
        if field in target:
            current[field] = target[field]
    
    return save_agent(current)


# ─── Agent Templates ───

AGENT_TEMPLATES = {
    "data_analyst_athena": {
        "name": "Athena 数据分析师",
        "description": "AWS Athena / S3 数据湖分析专家",
        "type": "data_analyst",
        "tools": ["semantic_query", "nl2sql_query", "get_data_catalog"],
        "skills": ["sql-optimization"],
        "system_prompt": "你是数据分析师，擅长通过 Athena 查询 S3 数据湖。优先使用语义层匹配，减少全表扫描。",
        "vqr_enabled": True,
        "insight_enabled": False,
    },
    "data_analyst_pg": {
        "name": "PostgreSQL 数据分析师",
        "description": "PostgreSQL 关系型数据库分析专家",
        "type": "data_analyst",
        "tools": ["pg_query", "get_data_catalog"],
        "skills": ["sql-optimization"],
        "system_prompt": "你是数据分析师，擅长 PostgreSQL 查询。注意使用索引，避免全表扫描。",
        "vqr_enabled": True,
        "insight_enabled": True,
    },
    "data_analyst_full": {
        "name": "全栈数据分析师",
        "description": "支持多数据源的全能分析师（Athena + PostgreSQL）",
        "type": "data_analyst",
        "tools": ["semantic_query", "nl2sql_query", "pg_query", "get_data_catalog"],
        "skills": ["sql-optimization"],
        "system_prompt": "你是全栈数据分析师，根据问题自动选择合适的数据源和查询方式。",
        "vqr_enabled": True,
        "insight_enabled": True,
    },
    "manufacturing_analyst": {
        "name": "生产制造分析师",
        "description": "专注生产线效率、质量检测、设备状态分析",
        "type": "data_analyst",
        "tools": ["pg_query", "get_data_catalog"],
        "skills": ["manufacturing-domain", "sql-optimization"],
        "system_prompt": "你是生产制造数据分析师，专注产线良品率、设备OEE、质量检测等分析。",
        "vqr_enabled": True,
        "insight_enabled": True,
    },
    "vehicle_analyst": {
        "name": "车联网分析师",
        "description": "车辆数据分析专家，覆盖车型分布、行驶行为、能源趋势",
        "type": "data_analyst",
        "tools": ["semantic_query", "nl2sql_query", "get_data_catalog"],
        "skills": ["vehicle-domain", "sql-optimization"],
        "system_prompt": "你是车联网数据分析师，专注车型分布、行驶行为分析、能源类型趋势等。",
        "vqr_enabled": True,
        "insight_enabled": False,
    },
}


def list_templates() -> dict:
    """Return available agent templates."""
    return AGENT_TEMPLATES


def create_from_template(template_id: str, overrides: dict = None) -> dict:
    """Create a new agent from a template."""
    template = AGENT_TEMPLATES.get(template_id)
    if not template:
        raise ValueError(f"Template '{template_id}' not found")
    
    agent_def = copy.deepcopy(template)
    if overrides:
        agent_def.update(overrides)
    
    return save_agent(agent_def)


def clone_agent(agent_id: str, new_name: str = "") -> dict:
    """Clone an existing agent with a new ID."""
    original = get_agent_def(agent_id)
    if not original:
        raise ValueError(f"Agent {agent_id} not found")
    
    cloned = copy.deepcopy(original)
    cloned["id"] = f"agent_{uuid.uuid4().hex[:8]}"
    cloned["name"] = new_name or f"{original['name']} (Copy)"
    cloned["version"] = 1
    cloned["created_by"] = "system"
    return save_agent(cloned)


# ─── Migration: V1 (agent_definitions) → V2 (AGENT#) ───

def migrate_v1_to_v2():
    """
    One-time migration: read old `agent_definitions` config key,
    convert to individual AGENT# records.
    Returns list of migrated agent IDs.
    """
    table = _ddb.Table(CONFIG_TABLE)
    
    # Check if already migrated
    existing = list_agents()
    if existing:
        return []  # Already have V2 agents
    
    # Read V1
    resp = table.get_item(Key={"config_key": "agent_definitions"})
    item = resp.get("Item")
    if not item:
        return []
    
    v1_data = json.loads(item.get("data", item.get("value", "{}")))
    if "agent_definitions" in v1_data:
        v1_data = v1_data["agent_definitions"]
    
    # Read scenarios for tools/prompt/datasources
    s_resp = table.get_item(Key={"config_key": "scenarios"})
    scenarios = json.loads(s_resp.get("Item", {}).get("data", "{}"))
    
    # Read datasource configs for type mapping
    ds_resp = table.get_item(Key={"config_key": "custom_datasources"})
    datasources = json.loads(ds_resp.get("Item", {}).get("data", "{}"))
    
    migrated = []
    
    for key, v1_def in v1_data.items():
        if key == "supervisor":
            continue  # Supervisor becomes system component
        
        # Find matching scenario for this agent
        agent_scenarios = []
        for sid, scfg in scenarios.items():
            if key in scfg.get("agents", []):
                agent_scenarios.append((sid, scfg))
        
        if not agent_scenarios:
            # Agent with no scenario — create one generic Agent
            agent = {
                "id": f"agent_{key}",
                "name": v1_def.get("name", key),
                "description": v1_def.get("description", ""),
                "type": "data_analyst",
                "model_id": v1_def.get("model_id", ""),
                "system_prompt": "",
                "tools": [],
                "skills": v1_def.get("skills", []),
                "datasources": [],
            }
            save_agent(agent)
            migrated.append(agent["id"])
        else:
            # Create one Agent per scenario that references this agent
            # (or one Agent with merged config if agent appears in multiple scenarios)
            for sid, scfg in agent_scenarios:
                if sid == "cross_domain":
                    continue  # Cross-domain uses multiple agents, skip
                
                tools = [t["name"] if isinstance(t, dict) else t for t in scfg.get("tools", [])]
                ds_ids = scfg.get("datasources", scfg.get("datasource_ids", []))
                
                agent = {
                    "id": f"agent_{sid}",
                    "name": scfg.get("name", sid),
                    "description": scfg.get("description", ""),
                    "type": "data_analyst",
                    "model_id": v1_def.get("model_id", ""),
                    "system_prompt": scfg.get("prompt_context", ""),
                    "tools": tools,
                    "skills": v1_def.get("skills", []),
                    "datasources": ds_ids,
                }
                save_agent(agent)
                migrated.append(agent["id"])
    
    print(f"[Migration] V1→V2: migrated {len(migrated)} agents: {migrated}")
    return migrated


# ─── Scene Definition ───

def list_scenes() -> dict:
    """Return all scenes. Reads from `scenarios` key (V1 compat) + SCENE# keys."""
    table = _ddb.Table(CONFIG_TABLE)
    scenes = {}
    
    # V2: SCENE# keys
    resp = table.scan(
        FilterExpression="begins_with(config_key, :prefix)",
        ExpressionAttributeValues={":prefix": "SCENE#"},
    )
    for item in resp.get("Items", []):
        data = json.loads(item.get("data", "{}"))
        if data.get("id"):
            scenes[data["id"]] = data
    
    # V1 fallback: scenarios key (auto-adapt)
    if not scenes:
        resp = table.get_item(Key={"config_key": "scenarios"})
        v1 = json.loads(resp.get("Item", {}).get("data", "{}"))
        for sid, scfg in v1.items():
            scenes[sid] = _adapt_v1_scene(sid, scfg)
    
    return scenes


def get_scene(scene_id: str) -> dict | None:
    """Get a single scene."""
    table = _ddb.Table(CONFIG_TABLE)
    resp = table.get_item(Key={"config_key": f"SCENE#{scene_id}"})
    item = resp.get("Item")
    if item:
        return json.loads(item.get("data", "{}"))
    
    # V1 fallback
    resp = table.get_item(Key={"config_key": "scenarios"})
    v1 = json.loads(resp.get("Item", {}).get("data", "{}"))
    if scene_id in v1:
        return _adapt_v1_scene(scene_id, v1[scene_id])
    return None


def save_scene(scene_def: dict) -> dict:
    """Create or update a scene."""
    now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    if not scene_def.get("id"):
        scene_def["id"] = f"scene_{uuid.uuid4().hex[:8]}"
    
    scene_def.setdefault("created_at", now)
    scene_def["updated_at"] = now
    
    table = _ddb.Table(CONFIG_TABLE)
    data_str = json.dumps(scene_def, ensure_ascii=False, default=str)
    table.put_item(Item={
        "config_key": f"SCENE#{scene_def['id']}",
        "data": data_str,
        "value": data_str,
    })
    return scene_def


def delete_scene(scene_id: str) -> bool:
    table = _ddb.Table(CONFIG_TABLE)
    table.delete_item(Key={"config_key": f"SCENE#{scene_id}"})
    return True


def _adapt_v1_scene(sid: str, v1: dict) -> dict:
    """Convert V1 scenario to V2 scene format (in-memory adapter)."""
    # Determine mode from agent count and datasources
    agents_list = v1.get("agents", [])
    ds_list = v1.get("datasources", v1.get("datasource_ids", []))
    
    if len(ds_list) > 1 or sid == "cross_domain":
        mode = "parallel"
    else:
        mode = "direct"
    
    # Build agent references
    agent_refs = []
    if sid == "cross_domain":
        # Cross-domain always uses both V2 agents
        agent_refs = [
            {"id": "agent_vehicle_analytics", "role": "primary"},
            {"id": "agent_manufacturing", "role": "support"},
        ]
    else:
        for i, aid in enumerate(agents_list):
            agent_refs.append({
                "id": f"agent_{sid}" if mode == "direct" else f"agent_{aid}",
                "role": "primary" if i == 0 else "support",
            })
    
    return {
        "id": sid,
        "name": v1.get("name", sid),
        "description": v1.get("description", ""),
        "icon": v1.get("icon", "📊"),
        "orchestration": {
            "mode": mode,
            "agents": agent_refs,
        },
        "welcome": v1.get("welcome_message", ""),
        "suggestions": {
            "auto": True,
            "manual": v1.get("suggested_questions", []),
        },
        "access": {
            "roles": ["admin", "analyst"],
        },
        "enabled": v1.get("enabled", True),
        # Keep V1 fields for backward compat during transition
        "_v1": v1,
    }


def migrate_scenes_v1_to_v2():
    """Convert V1 scenarios to V2 SCENE# records."""
    table = _ddb.Table(CONFIG_TABLE)
    
    # Check if already migrated
    resp = table.scan(
        FilterExpression="begins_with(config_key, :prefix)",
        ExpressionAttributeValues={":prefix": "SCENE#"},
    )
    if resp.get("Items"):
        return []
    
    # Read V1 scenarios
    resp = table.get_item(Key={"config_key": "scenarios"})
    v1_scenarios = json.loads(resp.get("Item", {}).get("data", "{}"))
    
    migrated = []
    for sid, scfg in v1_scenarios.items():
        scene = _adapt_v1_scene(sid, scfg)
        save_scene(scene)
        migrated.append(sid)
    
    print(f"[Migration] Scenes V1→V2: migrated {len(migrated)} scenes: {migrated}")
    return migrated
