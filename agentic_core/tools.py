"""
Agentic Auto — Tool definitions (replacing 4 Lambdas)
Direct boto3 calls, no Lambda needed.

All table/field metadata is loaded dynamically from DDB custom_datasources.
No hardcoded table names or field dictionaries.
"""
import json, time, boto3
from agentic_core.db_engine import _safe_identifier

# Legacy field dictionaries removed — all metadata now in DDB custom_datasources.table_descriptions
# Migration script moved rich descriptions + JOIN info into DDB on 2026-03-30.
ATHENA_FIELD_DICT = {}  # Kept as empty dict for backward compat; code uses DDB table_descriptions
PG_FIELD_DICT = {}       # Same

from decimal import Decimal
from strands.tools import tool
from botocore.config import Config

import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from config import REGION, EVENTS_TABLE, DATA_BUCKET, REPORTS_BUCKET, ATHENA_DATABASE, CONFIG_TABLE


def _emit_artifact(data):
    """Emit artifact event via whichever callback is active (trace or sub_agent)."""
    from agentic_core import agents as _ag
    tc = getattr(_ag, '_trace_callback', None)
    sc = getattr(_ag, '_sub_agent_callback', None)
    if tc:
        tc("DataAnalystAgent", "artifact", data)
    elif sc:
        sc("DataAnalystAgent", "artifact", data)

# ── Dynamic datasource resolution (replaces all hardcoded DB/table/S3 references) ──
_ds_cache = {"ts": 0, "data": []}

def _get_datasources():
    """Load datasources from DDB with 30s TTL cache."""
    if time.time() - _ds_cache["ts"] < 30 and _ds_cache["data"]:
        return _ds_cache["data"]
    try:
        resp = boto3.resource("dynamodb", region_name=REGION).Table(CONFIG_TABLE).get_item(Key={"config_key": "custom_datasources"})
        if "Item" in resp:
            _ds_cache["data"] = json.loads(resp["Item"].get("data", resp["Item"].get("value", "[]")))
        else:
            _ds_cache["data"] = []
    except Exception:
        pass
    _ds_cache["ts"] = time.time()
    return _ds_cache["data"]

def _get_athena_ds():
    """Get the first connected Athena datasource config, or None."""
    return next((ds for ds in _get_datasources() if ds.get("type", "").lower() == "athena" and ds.get("enabled", True)), None)

def _get_pg_ds():
    """Get the first connected PostgreSQL or RDS/MySQL datasource config, or None."""
    for ds in _get_datasources():
        if not ds.get("enabled", True):
            continue
        t = ds.get("type", "").lower()
        if t in ("postgresql", "mysql"):
            return ds
        if t == "rds":
            return ds
    return None

def _get_snowflake_ds():
    """Get the first connected Snowflake datasource config, or None."""
    return next((ds for ds in _get_datasources() if ds.get("type", "").lower() == "snowflake" and ds.get("enabled", True)), None)

def _athena_db():
    ds = _get_athena_ds()
    return ds["database"] if ds else (ATHENA_DATABASE or "agentic_auto")

def _athena_output():
    ds = _get_athena_ds()
    if ds and ds.get("output_location"):
        return ds["output_location"]
    return os.environ.get("ATHENA_OUTPUT", f"s3://agentic-data-{boto3.client('sts').get_caller_identity()['Account']}-{REGION}/athena-results/")

def _build_table_section(db_prefix, table_key, td):
    """Build a docstring section for one table from DDB table_descriptions. Used by both Athena and PG."""
    if not isinstance(td, dict):
        return f"    - {db_prefix}{table_key}"
    desc = td.get("description", "")
    join_info = td.get("join_info", "")
    cols = td.get("columns", {})
    lines = []
    # Table header
    header = f"    📋 {db_prefix}{table_key}"
    if desc:
        header += f" — {desc}"
    lines.append(header)
    # JOIN info
    if join_info:
        lines.append(f"       JOIN: {join_info}")
    # Columns
    if cols:
        col_parts = []
        for col_name, col_info in cols.items():
            if isinstance(col_info, dict):
                col_desc = col_info.get("description", col_info.get("type", ""))
            else:
                col_desc = str(col_info)
            col_parts.append(f"{col_name}: {col_desc}" if col_desc else col_name)
        lines.append(f"       字段: {'; '.join(col_parts)}")
    return "\n".join(lines)


def _build_nl2sql_docstring():
    """Build nl2sql_query docstring dynamically from DDB table_descriptions."""
    ds = _get_athena_ds()
    if not ds:
        return "Athena SQL 查询工具（当前无 Athena 数据源连接）。\n\n    Args:\n        question: 用户原始问题\n        sql: SQL 语句"
    db = ds.get("database", "unknown")
    tables = ds.get("tables", [])
    table_descs = ds.get("table_descriptions", {})
    lines = [f"通过 Athena (Trino SQL) 执行查询。\n\n    数据库: {db}\n    ⚠️ Trino语法: date_diff()不是DATEDIFF(), timestamp列不要DATE_PARSE()!\n"]
    for t in tables:
        t_short = t.split(".")[-1] if "." in t else t
        td = table_descs.get(t, table_descs.get(t_short, {}))
        lines.append(_build_table_section(f"{db}.", t_short, td))
    lines.append(f"\n    ⚠️ 只能使用上面列出的字段名！不存在的字段会导致查询失败。")
    lines.append(f"\n    Args:\n        question: 用户原始问题\n        sql: Trino SQL (完整表名如 {db}.{tables[0].split('.')[-1] if tables else 'table_name'})")
    lines.append("    Returns: 查询结果 (最多500行)")
    return "\n".join(lines)

def _build_pg_docstring():
    """Build pg_query docstring dynamically from DDB table_descriptions. Supports PostgreSQL and MySQL."""
    # Collect ALL PG/RDS/MySQL datasources
    all_sql = []
    for ds in _get_datasources():
        if not ds.get("enabled", True):
            continue
        t = ds.get("type", "").lower()
        if t in ("postgresql", "mysql", "rds"):
            all_sql.append(ds)
    if not all_sql:
        return "SQL 查询工具（当前无 RDS 数据源连接）。\n\n    Args:\n        question: 用户原始问题\n        sql: SQL 语句"
    ds0 = all_sql[0]
    engine = ds0.get("config", {}).get("engine", "mysql" if ds0.get("type", "").lower() == "rds" else "postgresql").lower()
    db = ds0.get("database", ds0.get("config", {}).get("database", "unknown"))
    dialect = "MySQL" if engine == "mysql" else "PostgreSQL"
    if engine == "mysql":
        syntax_note = "❗用反引号 ` 包裹表名和字段名，MySQL 语法"
    else:
        syntax_note = f"❗直接写表名，不要写 {db}.表名！"
    lines = [f"通过 {dialect} 执行 SQL 查询。\n\n    数据库: {db}\n    {syntax_note}\n"]
    for ds in all_sql:
        table_descs = ds.get("table_descriptions", {})
        tables = ds.get("tables", list(table_descs.keys()))
        for t in tables:
            td = table_descs.get(t, {})
            lines.append(_build_table_section("", t, td))
    lines.append(f"\n    ⚠️ 只能使用上面列出的字段名！")
    lines.append(f"\n    Args:\n        question: 用户原始问题\n        sql: {dialect} 语法")
    lines.append("    Returns: 查询结果 (最多500行)")
    return "\n".join(lines)


def _build_snowflake_docstring():
    """Build snowflake_query docstring dynamically from DDB table_descriptions."""
    all_sf = [ds for ds in _get_datasources() if ds.get("type", "").lower() == "snowflake" and ds.get("enabled", True)]
    if not all_sf:
        return "Snowflake SQL 查询工具（当前无 Snowflake 数据源连接）。\n\n    Args:\n        question: 用户原始问题\n        sql: SQL 语句"
    ds = all_sf[0]
    config = ds.get("config", {})
    db = config.get("database", ds.get("database", "unknown"))
    schema = config.get("schema", "PUBLIC")
    warehouse = config.get("warehouse", "")
    lines = [f"通过 Snowflake 执行 SQL 查询。\n\n    数据库: {db}, Schema: {schema}" +
             (f", Warehouse: {warehouse}" if warehouse else "") +
             f"\n    ⚠️ 表名格式: {db}.{schema}.TABLE 或直接写 TABLE（默认 schema）\n"]
    for sf_ds in all_sf:
        table_descs = sf_ds.get("table_descriptions", {})
        tables = sf_ds.get("tables", list(table_descs.keys()))
        for t in tables:
            td = table_descs.get(t, {})
            lines.append(_build_table_section("", t, td))
    lines.append(f"\n    ⚠️ 只能使用上面列出的字段名！")
    lines.append(f"\n    Args:\n        question: 用户原始问题\n        sql: Snowflake SQL")
    lines.append("    Returns: 查询结果 (最多500行)")
    return "\n".join(lines)


def _pg_introspect_columns(table_name, db_name=""):
    """Introspect PG table columns via live connection. Returns comma-separated column names."""
    try:
        conn = _get_pg_conn()
        if not conn:
            return ""
        cur = conn.cursor()
        cur.execute(f"SELECT column_name FROM information_schema.columns WHERE table_schema='public' AND table_name='{_safe_identifier(table_name)}' ORDER BY ordinal_position LIMIT 8")  # nosec B608  # nosemgrep: sqlalchemy-execute-raw-query  # nosemgrep: formatted-sql-query
        cols = [r[0] for r in cur.fetchall()]
        cur.close()
        _release_pg_conn(conn)
        if len(cols) > 6:
            return ", ".join(cols[:6]) + f" +{len(cols)-6}列"
        return ", ".join(cols)
    except Exception:
        return ""

_ddb = boto3.resource("dynamodb", region_name=REGION)
_s3 = boto3.client("s3", region_name=REGION)
_athena = boto3.client("athena", region_name=REGION)


class DecimalEncoder(json.JSONEncoder):
    def default(self, o):
        if isinstance(o, Decimal): return float(o)
        return super().default(o)

def _j(items):
    return json.loads(json.dumps(items, cls=DecimalEncoder, ensure_ascii=False))

def _run_athena(sql, max_wait=55):
    resp = _athena.start_query_execution(
        QueryString=sql, QueryExecutionContext={"Database": _athena_db()},
        ResultConfiguration={"OutputLocation": _athena_output()})
    qid = resp["QueryExecutionId"]
    for _ in range(max_wait):
        st = _athena.get_query_execution(QueryExecutionId=qid)
        state = st["QueryExecution"]["Status"]["State"]
        if state == "SUCCEEDED": break
        if state in ("FAILED","CANCELLED"):
            return {"error": st["QueryExecution"]["Status"].get("StateChangeReason","")}
        time.sleep(1)  # nosemgrep: arbitrary-sleep
    else:
        return {"error": "timeout"}
    result = _athena.get_query_results(QueryExecutionId=qid, MaxResults=500)
    cols = [c["Name"] for c in result["ResultSet"]["ResultSetMetadata"]["ColumnInfo"]]
    rows = [{c: d.get("VarCharValue","") for c,d in zip(cols,row["Data"])}
            for row in result["ResultSet"]["Rows"][1:]]
    return {"columns": cols, "rows": rows, "count": len(rows)}


# ═══════ Data Query Tools ═══════

def _truncate_result(result: str, max_chars: int = 4000) -> str:
    if len(result) > max_chars:
        return result[:max_chars] + f"\n...[截断,共{len(result)}字符]"
    return result


# ═══════ Safety Tools ═══════


# ═══════ Behavior Tools ═══════


# ═══════ Report Tools ═══════

@tool
def save_report(report_name: str, content: str) -> str:
    """Save analysis report to S3."""
    key = f"reports/{int(time.time())}_{report_name}.md"
    _s3.put_object(Bucket=REPORTS_BUCKET, Key=key, Body=content.encode(), ContentType="text/markdown; charset=utf-8")
    return json.dumps({"saved": key})

@tool
def list_reports() -> str:
    """List saved reports from S3."""
    resp = _s3.list_objects_v2(Bucket=REPORTS_BUCKET, Prefix="reports/", MaxKeys=20)
    return json.dumps({"reports": [{"key":o["Key"],"size_kb":round(o["Size"]/1024,1),
        "date":o["LastModified"].isoformat()} for o in resp.get("Contents",[])]})


# ═══════ ChatBI Tools (通用 NL2SQL) ═══════

# Data catalog — Agent uses this to understand available data
# Dynamic — populated by build_data_catalog() from connected datasources
DATA_CATALOG = {"tables": []}


@tool
def get_data_catalog() -> str:
    """Get the data catalog (data dictionary) of all available tables, columns, and descriptions.
    Use this to understand what data is available before writing SQL queries.
    Returns table schemas, column types, descriptions, and query tips."""
    from agentic_core.dynamic_context import build_data_catalog
    # Get scenario datasource filter from agent config if available
    ds_ids = None
    try:
        from agentic_core import agents as _ag
        scenario_cfg = getattr(_ag, '_sub_agent_config', {}).get("_scenario_cfg")
        if scenario_cfg:
            ds_ids = scenario_cfg.get("datasources", [])
    except Exception:
        pass
    tables = build_data_catalog(datasource_ids=ds_ids)
    result = []
    for t in tables:
        cols = "\n".join([f"  - {c['name']} ({c['type']}): {c['desc']}" for c in t.get("columns", [])])
        tips = t.get("tips", "")
        result.append(f"### {t['name']}\n{t.get('description','')}\n类型: {t.get('type','')}\n字段:\n{cols}" + (f"\n提示: {tips}" if tips else ""))
    return "\n\n".join(result)


@tool
def nl2sql_query(question: str, sql: str) -> str:
    """通过 Athena 执行 SQL 查询（动态加载数据源配置）。

    Args:
        question: 用户原始问题
        sql: 生成的 SQL

    Returns: 查询结果 (最多500行)"""
    # Emit artifact: SQL being executed
    from agentic_core import agents as _ag
    _emit_artifact({
            "type": "sql",
            "sql": sql,
            "question": question,
            "database": _athena_db(),
            "status": "executing",
        })

    t0 = time.time()
    result = _run_athena(sql)
    elapsed_ms = int((time.time() - t0) * 1000)
    result["question"] = question
    result["sql"] = sql

    # Cache SQL for VQR feedback (keyed by session via thread-local config)
    if not result.get("error"):
        try:
            cfg = _get_sub_agent_config()
            sid = cfg.get("session_id", "") if cfg else ""
            if sid:
                from agentic_core.sql_cache import set_sql
                set_sql(sid, sql, "athena", question)
        except Exception:
            pass

    # VQR: auto-record SQL errors as negative signal
    if result.get("error"):
        try:
            from agentic_core.vqr import add_candidate
            _ds = _get_athena_ds()
            _ds_id = _ds.get("id", "") if _ds else ""
            add_candidate(question=question, sql=sql, engine="athena",
                         datasource=_ds_id, rating="down", run_judge=False)
            print(f"[VQR] Negative signal from nl2sql error: {result['error'][:80]}")
        except Exception:
            pass

    # Emit artifact: query results
    preview_rows = result.get("rows", [])[:20]
    _emit_artifact({
            "type": "query_result",
            "sql": sql,
            "columns": result.get("columns", []),
            "rows": preview_rows,
            "total_rows": result.get("count", 0),
            "elapsed_ms": elapsed_ms,
            "error": result.get("error"),
        })

    return json.dumps(result, ensure_ascii=False)


# ═══════ ChatBI — Multi-Department Data Tools ═══════


# ── Alert & KPI Rule Management Tools (NL-configurable) ──

@tool
def manage_alert_rules(action: str, rule_config: str = "") -> str:
    """管理告警规则: 创建、查看、修改、删除告警规则。用户可用自然语言描述告警条件。

    Args:
        action: 操作类型 — list(查看所有), create(创建), update(修改), delete(删除), toggle(启用/禁用)
        rule_config: JSON 格式的规则配置。创建/修改时必填。
            创建示例: {"name":"SOH预警","dataset":"battery_health","field":"soh_pct","operator":"<","threshold":85,"level":"HIGH","category":"电池","title_template":"{vin_short} SOH {value}%","detail_template":"需关注电池状态"}
            修改示例: {"id":"rule-xxx","threshold":90}
            删除示例: {"id":"rule-xxx"}
            启用/禁用: {"id":"rule-xxx","enabled":false}
    """
    from agentic_core.alert_rules import load_alert_rules, save_alert_rules, ensure_defaults
    ensure_defaults()
    rules = load_alert_rules()

    if action == "list":
        if not rules:
            return json.dumps({"message": "暂无告警规则"}, ensure_ascii=False)
        summary = []
        for r in rules:
            summary.append(f"[{r.get('level')}] {r.get('name')} — {r.get('field')} {r.get('operator')} {r.get('threshold')} ({'启用' if r.get('enabled',True) else '禁用'}) id={r['id']}")
        return json.dumps({"count": len(rules), "rules": summary}, ensure_ascii=False)

    config = json.loads(rule_config) if rule_config else {}

    if action == "create":
        import uuid as _uuid
        rule = {
            "id": f"rule-{_uuid.uuid4().hex[:8]}",
            "name": config.get("name", "自定义规则"),
            "enabled": config.get("enabled", True),
            "dataset": config.get("dataset", ""),
            "field": config.get("field", ""),
            "operator": config.get("operator", "<"),
            "threshold": config.get("threshold"),
            "level": config.get("level", "MEDIUM"),
            "category": config.get("category", "自定义"),
            "title_template": config.get("title_template", "{vin_short} {field} = {value}"),
            "detail_template": config.get("detail_template", ""),
            "query_template": config.get("query_template", ""),
            "dedup_field": config.get("dedup_field", "vin"),
            "dedup_order": config.get("dedup_order", "date"),
        }
        if config.get("extra_filter"):
            rule["extra_filter"] = config["extra_filter"]
        rules.append(rule)
        save_alert_rules(rules)
        return json.dumps({"ok": True, "message": f"告警规则已创建: {rule['name']}", "rule_id": rule["id"]}, ensure_ascii=False)

    elif action == "update":
        rule_id = config.get("id", "")
        for r in rules:
            if r["id"] == rule_id:
                for k, v in config.items():
                    if k != "id":
                        r[k] = v
                save_alert_rules(rules)
                return json.dumps({"ok": True, "message": f"规则 {r['name']} 已更新"}, ensure_ascii=False)
        return json.dumps({"ok": False, "message": f"规则 {rule_id} 不存在"}, ensure_ascii=False)

    elif action == "delete":
        rule_id = config.get("id", "")
        before = len(rules)
        rules = [r for r in rules if r["id"] != rule_id]
        save_alert_rules(rules)
        return json.dumps({"ok": True, "message": f"已删除 {before - len(rules)} 条规则"}, ensure_ascii=False)

    elif action == "toggle":
        rule_id = config.get("id", "")
        enabled = config.get("enabled")
        for r in rules:
            if r["id"] == rule_id:
                r["enabled"] = enabled if enabled is not None else not r.get("enabled", True)
                save_alert_rules(rules)
                state = "启用" if r["enabled"] else "禁用"
                return json.dumps({"ok": True, "message": f"规则 {r['name']} 已{state}"}, ensure_ascii=False)
        return json.dumps({"ok": False, "message": f"规则 {rule_id} 不存在"}, ensure_ascii=False)

    return json.dumps({"error": f"不支持的操作: {action}"}, ensure_ascii=False)


@tool
def manage_kpi_rules(action: str, rule_config: str = "") -> str:
    """管理 KPI 卡片规则: 创建、查看、修改、删除看板上的 KPI 指标卡片。

    Args:
        action: 操作类型 — list(查看所有), create(创建), update(修改), delete(删除)
        rule_config: JSON 格式的 KPI 规则配置。
            创建示例: {"name":"平均续航","dataset":"driving_daily","agg":"avg","field":"range_km","format":"{value}km","thresholds":{"good":300,"warning":200},"query":"续航分析","order":10}
            聚合类型: avg(平均), sum(求和), count(计数), min, max, count_where(条件计数), success_rate(成功率)
            count_where 需要: where_op("<"等) + where_val(阈值)
    """
    from agentic_core.alert_rules import load_kpi_rules, save_kpi_rules, ensure_defaults
    ensure_defaults()
    rules = load_kpi_rules()

    if action == "list":
        if not rules:
            return json.dumps({"message": "暂无 KPI 规则"}, ensure_ascii=False)
        summary = []
        for r in rules:
            summary.append(f"{r.get('name')} — {r.get('agg')}({r.get('field')}) from {r.get('dataset')} [{r.get('format')}] id={r['id']}")
        return json.dumps({"count": len(rules), "rules": summary}, ensure_ascii=False)

    config = json.loads(rule_config) if rule_config else {}

    if action == "create":
        import uuid as _uuid
        rule = {
            "id": f"kpi-{_uuid.uuid4().hex[:8]}",
            "name": config.get("name", "新指标"),
            "dataset": config.get("dataset", ""),
            "agg": config.get("agg", "avg"),
            "field": config.get("field", ""),
            "dedup_field": config.get("dedup_field"),
            "dedup_order": config.get("dedup_order", "date"),
            "format": config.get("format", "{value}"),
            "thresholds": config.get("thresholds", {}),
            "query": config.get("query", ""),
            "extra_label": config.get("extra_label", ""),
            "order": config.get("order", 99),
        }
        if config.get("where_op"):
            rule["where_op"] = config["where_op"]
            rule["where_val"] = config["where_val"]
        if config.get("success_values"):
            rule["success_values"] = config["success_values"]
        rules.append(rule)
        save_kpi_rules(rules)
        return json.dumps({"ok": True, "message": f"KPI 规则已创建: {rule['name']}", "rule_id": rule["id"]}, ensure_ascii=False)

    elif action == "update":
        rule_id = config.get("id", "")
        for r in rules:
            if r["id"] == rule_id:
                for k, v in config.items():
                    if k != "id":
                        r[k] = v
                save_kpi_rules(rules)
                return json.dumps({"ok": True, "message": f"KPI {r['name']} 已更新"}, ensure_ascii=False)
        return json.dumps({"ok": False, "message": f"KPI 规则 {rule_id} 不存在"}, ensure_ascii=False)

    elif action == "delete":
        rule_id = config.get("id", "")
        before = len(rules)
        rules = [r for r in rules if r["id"] != rule_id]
        save_kpi_rules(rules)
        return json.dumps({"ok": True, "message": f"已删除 {before - len(rules)} 条 KPI 规则"}, ensure_ascii=False)

    return json.dumps({"error": f"不支持的操作: {action}"}, ensure_ascii=False)


# Dynamic — populated from connected datasources at startup + runtime
CHATBI_DATASETS = {}


def _chatbi_query_doc():
    """Generate dynamic docstring based on currently connected datasets."""
    if not CHATBI_DATASETS:
        return "ChatBI: 暂无已连接的数据集。请先在管理后台添加数据源。"
    lines = ["ChatBI: Query connected datasets with natural language.", "", "Available datasets:"]
    for name, info in CHATBI_DATASETS.items():
        lines.append(f"    - {name}: {info.get('desc', '')}")
    lines.extend(["", "Args:", "    dataset: Dataset name from above list",
        "    question: Original user question",
        "    filters: Optional filter expression, e.g. 'city=北京'",
        "    group_by: Field to group by, e.g. 'city', 'model'",
        "    metric: Aggregation: count, sum, avg, max, min + optional field, e.g. 'avg:cost_yuan'"])
    return "\n".join(lines)


# ═══════ SQL Database Engine (SQLite / Snowflake / PostgreSQL) ═══════

from agentic_core.db_engine import get_engine, get_multi_engine

def _run_sql(sql, max_rows=200, engine_key=None):
    """Execute SQL via the configured engine (default or specified)."""
    return get_engine(engine_key).execute(sql, max_rows)



# ═══════ Tableau BI 报表工具 ═══════



# ═══════ 语义层查询工具 ═══════

@tool
def semantic_query(question: str) -> str:
    """Query the semantic layer to get pre-defined metrics, dimensions, and SQL templates.
    
    ALWAYS call this FIRST before writing any SQL or chatbi query.
    It returns:
    - Verified queries (highest priority — pre-approved SQL)
    - Matched metrics with exact SQL expressions
    - Matched dimensions with column names
    - Pre-built SQL templates you can use directly
    
    Args:
        question: The user's question in natural language
    """
    from agentic_core.semantic_layer import get_semantic_context, find_matching_templates, QUERY_TEMPLATES, METRICS, SYNONYMS, DIMENSIONS
    
    # Ensure semantic layer is loaded (may be empty in fresh worker)
    if not METRICS:
        try:
            import boto3, json, os
            _region = os.environ.get("AGENTIC_AUTO_REGION", os.environ.get("AWS_DEFAULT_REGION", "us-east-1"))
            _cfg_table = os.environ.get("CONFIG_TABLE", "agentic-auto-config")
            _tbl = boto3.resource("dynamodb", region_name=_region).Table(_cfg_table)
            _resp = _tbl.get_item(Key={"config_key": "semantic_layer"})
            if "Item" in _resp:
                _raw = _resp["Item"].get("data") or _resp["Item"].get("value") or "{}"
                _sc = json.loads(_raw) if isinstance(_raw, str) else _raw
                if isinstance(_sc.get("metrics"), dict):
                    METRICS.update(_sc["metrics"])
                if isinstance(_sc.get("dimensions"), dict):
                    DIMENSIONS.update(_sc["dimensions"])
                if isinstance(_sc.get("synonyms"), dict):
                    SYNONYMS.update(_sc["synonyms"])
                if isinstance(_sc.get("templates"), dict):
                    QUERY_TEMPLATES.update(_sc["templates"])
                print(f"[semantic_query] Force-loaded {len(METRICS)} metrics, {len(SYNONYMS)} synonyms from DDB")
        except Exception as e:
            print(f"[semantic_query] Failed to force-load semantic layer: {e}")
    
    # Get datasource filter from current scenario config
    import agentic_core.agents as _ag
    ds_filter = None
    if hasattr(_ag, '_sub_agent_config'):
        scenario_cfg = _ag._sub_agent_config.get("_scenario_cfg") or {}
        ds_filter = scenario_cfg.get("datasources")
    
    # Step 0: VQR matching (highest priority)
    vqr_match = None
    try:
        from agentic_core.vqr import match_verified_query, record_hit
        vqr_match = match_verified_query(question, datasource_filter=ds_filter)
        if vqr_match:
            record_hit(vqr_match["id"])
            print(f"[VQR] HIT: {vqr_match['match_type']} (score={vqr_match['match_score']}) → {vqr_match.get('question','')[:50]}")
    except Exception as e:
        print(f"[VQR] Match error: {e}")
    
    # Step 1: Semantic layer matching
    from agentic_core.semantic_layer import find_matching_metrics, find_matching_dimensions
    context = get_semantic_context(question, datasource_filter=ds_filter)
    templates = find_matching_templates(question)
    
    # Get matched names directly from matching functions (context is a string, not dict)
    metrics_list = find_matching_metrics(question)
    dims_list = find_matching_dimensions(question)
    # Apply datasource filter
    if ds_filter:
        ds_set = set(ds_filter)
        metrics_list = [m for m in metrics_list if not METRICS.get(m, {}).get('datasource') or METRICS.get(m, {}).get('datasource') in ds_set]
        dims_list = [d for d in dims_list if not DIMENSIONS.get(d, {}).get('datasource') or DIMENSIONS.get(d, {}).get('datasource') in ds_set]
    
    # Emit artifact: semantic matching results
    from agentic_core import agents as _ag
    sem_art = {
            "type": "semantic",
            "question": question,
            "vqr_hit": bool(vqr_match),
            "vqr_match_type": vqr_match.get("match_type") if vqr_match else None,
            "matched_metrics": metrics_list,
            "matched_dimensions": dims_list,
            "matched_templates": [name for name, _ in templates] if templates else [],
        }
    _emit_artifact(sem_art)
    
    result = {"semantic_context": context}
    
    # VQR match: execute verified SQL directly (no LLM generation needed)
    if vqr_match:
        vqr_sql = vqr_match.get("sql", "")
        vqr_engine = vqr_match.get("engine", "athena")
        result["verified_query"] = {
            "question": vqr_match.get("question"),
            "sql": vqr_sql,
            "engine": vqr_engine,
            "match_type": vqr_match.get("match_type"),
            "match_score": vqr_match.get("match_score"),
            "source": "vqr",
        }
        # Execute the verified SQL directly
        if vqr_sql:
            t0 = time.time()
            if vqr_engine == "postgresql":
                exec_result = _run_pg(vqr_sql)
            else:
                exec_result = _run_athena(vqr_sql)
            elapsed_ms = int((time.time() - t0) * 1000)
            _emit_artifact({
                "type": "query_result",
                "sql": vqr_sql,
                "columns": exec_result.get("columns", []),
                "rows": exec_result.get("rows", [])[:20],
                "total_rows": exec_result.get("count", 0),
                "elapsed_ms": elapsed_ms,
                "error": exec_result.get("error"),
                "vqr_source": True,
            })
            result["vqr_executed"] = True
            result["query_result"] = {
                "columns": exec_result.get("columns", []),
                "rows": exec_result.get("rows", [])[:10],
                "total_rows": exec_result.get("count", 0),
                "elapsed_ms": elapsed_ms,
                "error": exec_result.get("error"),
            }
            result["instruction"] = "VQR已命中并执行了验证过的SQL，数据结果在query_result中。请直接基于这些数据回答用户问题，不要再调用pg_query或nl2sql_query。"
    
    if templates:
        result["ready_queries"] = []
        for name, tmpl in templates:
            q = {"name": name, "description": tmpl["description"], "source": tmpl["source"]}
            if "sql" in tmpl:
                q["sql"] = tmpl["sql"]
            if "params" in tmpl:
                q["tool"] = tmpl["tool"]
                q["params"] = tmpl["params"]
            result["ready_queries"].append(q)
    
    return json.dumps(result, ensure_ascii=False)


# ═══════ PostgreSQL (Manufacturing) Tools ═══════

_pg_pool = None

def invalidate_pg_pool():
    """Reset PG connection pool when datasource config changes."""
    global _pg_pool
    if _pg_pool:
        try: _pg_pool.closeall()
        except: pass
    _pg_pool = None

_mysql_conn = None

def _get_rds_engine():
    """Detect if RDS datasource is mysql or postgresql."""
    ds = _get_pg_ds()
    if not ds:
        return "postgresql"  # default
    t = ds.get("type", "").lower()
    if t == "mysql":
        return "mysql"
    if t == "rds":
        return ds.get("config", {}).get("engine", "mysql").lower()
    # 即使 type 是 postgresql，也检查 config.engine（用户可能选错了类型）
    cfg_engine = ds.get("config", {}).get("engine", "").lower()
    if cfg_engine == "mysql":
        return "mysql"
    # 检查端口：3306 → mysql, 5432 → postgresql
    port = str(ds.get("config", {}).get("port", ds.get("port", "")))
    if port == "3306":
        return "mysql"
    return "postgresql"

def _get_mysql_conn():
    """Get a MySQL connection (lazy). Priority: env vars > DDB datasource config."""
    global _mysql_conn
    mysql_host = os.environ.get("MYSQL_HOST", "")
    mysql_port = int(os.environ.get("MYSQL_PORT", "3306"))
    mysql_db = os.environ.get("MYSQL_DATABASE", "")
    mysql_user = os.environ.get("MYSQL_USER", "admin")
    mysql_pass = os.environ.get("MYSQL_PASSWORD", "")
    
    if not mysql_host:
        ds = _get_pg_ds()
        if ds:
            cfg = ds.get("config", ds)
            mysql_host = cfg.get("host", "")
            mysql_port = int(cfg.get("port", 3306))
            mysql_db = cfg.get("database", "")
            mysql_user = cfg.get("username", cfg.get("user", "admin"))
            mysql_pass = cfg.get("password", "")
    
    if not mysql_host:
        return None
    
    import pymysql
    if _mysql_conn:
        try:
            _mysql_conn.ping(reconnect=True)
            return _mysql_conn
        except Exception:
            _mysql_conn = None
    _mysql_conn = pymysql.connect(
        host=mysql_host, port=mysql_port, database=mysql_db,
        user=mysql_user, password=mysql_pass,
        connect_timeout=10, charset='utf8mb4',
        cursorclass=pymysql.cursors.DictCursor
    )
    return _mysql_conn

def _release_mysql_conn(conn):
    return None  # keep alive, reuse

def _run_mysql(sql, max_rows=500):
    """Execute SQL on MySQL and return results."""
    sql_upper = sql.upper().strip()
    for forbidden in ["DROP", "DELETE", "TRUNCATE", "ALTER", "INSERT", "UPDATE", "CREATE", "GRANT"]:
        if sql_upper.startswith(forbidden):
            return {"error": f"\u5b89\u5168\u62e6\u622a: {forbidden} \u8bed\u53e5\u4e0d\u5141\u8bb8\u6267\u884c"}
    conn = _get_mysql_conn()
    if not conn:
        return {"error": "MySQL \u672a\u914d\u7f6e (MYSQL_HOST \u4e3a\u7a7a)"}
    try:
        with conn.cursor() as cur:
            cur.execute(sql)
            if cur.description is None:
                return {"error": "\u67e5\u8be2\u65e0\u8fd4\u56de\u7ed3\u679c"}
            cols = [desc[0] for desc in cur.description]
            rows = []
            for row in cur.fetchmany(max_rows):
                rows.append({c: (str(v) if v is not None else "") for c, v in row.items()})
            total = cur.rowcount if cur.rowcount >= 0 else len(rows)
        return {"columns": cols, "rows": rows, "count": len(rows), "total": total}
    except Exception as e:
        return {"error": str(e)[:500]}


def _get_pg_conn():
    """Get a PostgreSQL connection (lazy pool). 
    Priority: env vars > DDB datasource config."""
    global _pg_pool
    from config import POSTGRES_HOST, POSTGRES_PORT, POSTGRES_DATABASE, POSTGRES_USER, POSTGRES_PASSWORD
    
    pg_host = POSTGRES_HOST
    pg_port = POSTGRES_PORT
    pg_db = POSTGRES_DATABASE
    pg_user = POSTGRES_USER
    pg_pass = POSTGRES_PASSWORD
    
    # Fallback: read from DDB datasource config if env vars not set
    if not pg_host:
        ds = _get_pg_ds()
        if ds:
            cfg = ds.get("config", ds)
            pg_host = cfg.get("host", "")
            pg_port = int(cfg.get("port", 5432))
            pg_db = cfg.get("database", "postgres")
            pg_user = cfg.get("user", "")
            pg_pass = cfg.get("password", "")
    
    if not pg_host:
        return None
    if _pg_pool is None:
        import psycopg2, psycopg2.pool
        _pg_pool = psycopg2.pool.SimpleConnectionPool(
            1, 3,
            host=pg_host, port=pg_port,
            dbname=pg_db, user=pg_user, password=pg_pass,
            connect_timeout=10
        )
    return _pg_pool.getconn()

def _release_pg_conn(conn):
    if _pg_pool and conn:
        _pg_pool.putconn(conn)

def _run_pg(sql, max_rows=500):
    """Execute SQL on PostgreSQL or MySQL (auto-detect) and return results."""
    # Auto-detect: if RDS engine is mysql, delegate to _run_mysql
    if _get_rds_engine() == "mysql":
        return _run_mysql(sql, max_rows)
    sql_upper = sql.upper().strip()
    for forbidden in ["DROP", "DELETE", "TRUNCATE", "ALTER", "INSERT", "UPDATE", "CREATE", "GRANT"]:
        if sql_upper.startswith(forbidden):
            return {"error": f"安全拦截: {forbidden} 语句不允许执行"}
    conn = _get_pg_conn()
    if not conn:
        return {"error": "PostgreSQL 未配置 (POSTGRES_HOST 为空)"}
    try:
        cur = conn.cursor()
        cur.execute(sql)
        if cur.description is None:
            return {"error": "查询无返回结果"}
        cols = [desc[0] for desc in cur.description]
        rows = []
        for row in cur.fetchmany(max_rows):
            rows.append({c: (str(v) if v is not None else "") for c, v in zip(cols, row)})
        total = cur.rowcount if cur.rowcount >= 0 else len(rows)
        cur.close()
        return {"columns": cols, "rows": rows, "count": len(rows), "total": total}
    except Exception as e:
        conn.rollback()
        return {"error": str(e)[:500]}
    finally:
        _release_pg_conn(conn)


@tool
def pg_query(question: str, sql: str) -> str:
    """通过 PostgreSQL 执行 SQL 查询（动态加载数据源配置）。

    Args:
        question: 用户原始问题
        sql: 生成的 SQL (PostgreSQL 语法)

    Returns: 查询结果 (最多500行)"""
    from agentic_core import agents as _ag
    _emit_artifact({
            "type": "sql", "sql": sql, "question": question,
            "database": "manufacturing (PostgreSQL)", "status": "executing",
        })
    t0 = time.time()
    result = _run_pg(sql)
    elapsed_ms = int((time.time() - t0) * 1000)
    result["question"] = question
    result["sql"] = sql

    # Cache SQL for VQR feedback (keyed by session via thread-local config)
    if not result.get("error"):
        try:
            cfg = _get_sub_agent_config()
            sid = cfg.get("session_id", "") if cfg else ""
            if sid:
                from agentic_core.sql_cache import set_sql
                set_sql(sid, sql, "postgresql", question)
        except Exception:
            pass

    # VQR: auto-record SQL errors as negative signal
    if result.get("error"):
        try:
            from agentic_core.vqr import add_candidate
            add_candidate(question=question, sql=sql, engine="postgresql",
                         rating="down", run_judge=False)
            print(f"[VQR] Negative signal from pg_query error: {result['error'][:80]}")
        except Exception:
            pass
    _emit_artifact({
            "type": "query_result", "sql": sql,
            "columns": result.get("columns", []),
            "rows": result.get("rows", [])[:20],
            "total_rows": result.get("count", 0),
            "elapsed_ms": elapsed_ms, "error": result.get("error"),
        })

    return json.dumps(result, ensure_ascii=False)


# ═══════ Snowflake Query Tool ═══════

@tool
def snowflake_query(question: str, sql: str) -> str:
    """Snowflake SQL query tool — docstring dynamically generated at Agent creation time."""
    import time as _t
    t0 = _t.time()

    # Emit SQL artifact for frontend trace
    _emit_artifact({"type": "sql", "sql": sql, "engine": "snowflake"})

    # Safety: block destructive SQL
    sql_upper = sql.strip().upper()
    blocked = ["DROP ", "DELETE ", "TRUNCATE ", "ALTER ", "INSERT ", "UPDATE ", "CREATE ", "GRANT ", "REVOKE "]
    if any(sql_upper.startswith(b) for b in blocked):
        return json.dumps({"error": f"⛔ 安全拦截: {sql_upper.split()[0]} 语句不允许执行", "sql": sql})

    # Add LIMIT if missing
    if "LIMIT" not in sql_upper:
        sql = sql.rstrip(";") + " LIMIT 500"

    # Get Snowflake connection from db_engine
    try:
        from agentic_core.db_engine import SnowflakeEngine
        ds = _get_snowflake_ds()
        if not ds:
            return json.dumps({"error": "未配置 Snowflake 数据源"})
        config = ds.get("config", {})
        engine = SnowflakeEngine(
            account=config.get("account", ""),
            user=config.get("user", ""),
            password=config.get("password", ""),
            warehouse=config.get("warehouse", ""),
            database=config.get("database", ""),
            schema=config.get("schema", "PUBLIC"),
            role=config.get("role", ""),
        )
        result = engine.execute(sql, max_rows=500)
    except Exception as e:
        result = {"error": str(e), "sql": sql}

    elapsed_ms = int((_t.time() - t0) * 1000)
    print(f"[SQL_AUDIT] engine=snowflake elapsed={elapsed_ms}ms sql={sql[:200]}")

    # VQR: auto-record SQL errors as negative signal
    if result.get("error"):
        try:
            from agentic_core.vqr import add_candidate
            _ds = _get_snowflake_ds()
            _ds_id = _ds.get("id", "") if _ds else ""
            add_candidate(question=question, sql=sql, engine="snowflake",
                         datasource=_ds_id, rating="down", run_judge=False)
            print(f"[VQR] Negative signal from snowflake error: {result['error'][:80]}")
        except Exception:
            pass

    _emit_artifact({
        "type": "query_result", "sql": sql,
        "columns": result.get("columns", []),
        "rows": result.get("rows", [])[:20],
        "total_rows": result.get("count", 0),
        "elapsed_ms": elapsed_ms, "error": result.get("error"),
    })

    return json.dumps(result, ensure_ascii=False)
