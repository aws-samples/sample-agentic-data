"""
Dynamic Context Builder — 根据当前已注册的数据源动态生成:
1. Agent Prompt 中的数据集列表
2. Tool Docstring
3. DATA_CATALOG
4. 快捷问题

所有 Agent 上下文从这里生成,不再硬编码。
"""
from agentic_core.tools import CHATBI_DATASETS, DATA_CATALOG
from agentic_core.semantic_layer import METRICS, DIMENSIONS, SYNONYMS, QUERY_TEMPLATES


def _resolve_engine_types(datasource_ids):
    """Map datasource IDs to engine type keys (postgresql, athena, sqlite, snowflake).
    
    Datasource configs in DDB have 'id' and 'type' fields.
    Engine registry uses type as key (e.g., 'postgresql', 'sqlite').
    This bridges the gap.
    """
    if not datasource_ids:
        return None
    try:
        import boto3, json, os
        region = os.environ.get("AGENTIC_AUTO_REGION", os.environ.get("AWS_DEFAULT_REGION", "us-east-1"))
        table_name = os.environ.get("AGENTIC_AUTO_CONFIG_TABLE", "agentic-auto-config")
        t = boto3.resource("dynamodb", region_name=region).Table(table_name)
        r = t.get_item(Key={"config_key": "custom_datasources"})
        ds_list = json.loads(r.get("Item", {}).get("data", r.get("Item", {}).get("value", "[]")))
        # Build id → type mapping
        id_to_type = {d["id"]: d.get("type", "").lower() for d in ds_list if d.get("id")}
        engine_types = set()
        for ds_id in datasource_ids:
            ds_type = id_to_type.get(ds_id)
            if ds_type:
                engine_types.add(ds_type)
        return engine_types if engine_types else None
    except Exception:
        return None


def build_chatbi_dataset_list():
    """生成 ChatBI 数据集的描述列表 (用于 tool docstring 和 prompt)"""
    lines = []
    for name, info in CHATBI_DATASETS.items():
        desc = info.get("desc", name)
        lines.append(f"    - {name}: {desc}")
    return "\n".join(lines) if lines else "    (暂无已连接数据集)"


def build_chatbi_query_docstring():
    """动态生成 chatbi_query 的完整 tool description"""
    ds_list = build_chatbi_dataset_list()
    return f"""ChatBI: Query datasets with natural language.

Available datasets:
{ds_list}

Args:
    dataset: Dataset name from above list
    question: Original user question
    filters: Optional filter expression, e.g. "city=北京" or "severity=CRITICAL"
    group_by: Field to group by, e.g. "city", "model", "driver"
    metric: Aggregation: count, sum, avg, max, min + optional field, e.g. "avg:cost_yuan"
"""


def build_chatbi_cross_docstring():
    """动态生成 chatbi_cross_analysis 的 tool description"""
    names = ", ".join(CHATBI_DATASETS.keys())
    return f"""Cross-dataset analysis: JOIN multiple datasets by VIN for correlation analysis.

Available datasets: {names if names else "暂无"}

Args:
    question: Original user question
    datasets: Comma-separated dataset names to join (use list_datasets to see available names)
"""


def build_data_analyst_prompt(datasource_ids=None):
    """动态生成 DataAnalystAgent 的 system prompt
    
    Args:
        datasource_ids: 场景限定的数据源ID列表，None=所有数据源
    """
    # Resolve datasource IDs to engine types for filtering
    allowed_engine_types = _resolve_engine_types(datasource_ids) if datasource_ids else None
    
    # ChatBI datasets
    chatbi_names = ", ".join(CHATBI_DATASETS.keys())
    
    # SQL tables (from engines) — filtered by scenario datasources
    sql_tables = []
    try:
        from agentic_core.db_engine import get_multi_engine
        multi = get_multi_engine()
        for key in multi.engine_names:
            # Filter by engine type (postgresql, sqlite, snowflake, etc.)
            if allowed_engine_types is not None and key not in allowed_engine_types:
                continue
            eng = multi.get(key)
            schema = eng.get_schema()
            for tname, tinfo in schema.items():
                cols_count = len(tinfo.get("columns", []))
                row_count = tinfo.get("row_count", "?")
                sql_tables.append(f"{tname} ({row_count}行, {cols_count}列)")
    except Exception:
        sql_tables = []
    sql_info = ", ".join(sql_tables) if sql_tables else "无"
    
    # Semantic layer stats
    metric_names = ", ".join(list(METRICS.keys())[:8])
    if len(METRICS) > 8:
        metric_names += f" 等{len(METRICS)}个"
    
    return f"""你是数据分析师。用最少的工具调用回答数据问题。

## ⛔ 数据真实性（最高优先级！）
- **所有数字必须来自工具返回结果，绝不允许编造**
- **只能使用数据集中真实存在的字段名** — 工具返回什么字段就用什么字段
- **不能自创指标**（如"渗透率""热度评级""活跃度指数"等数据集中不存在的概念）
- **工具返回空或失败 → 回答"该数据集暂无相关数据"，不能编**
- **排名必须严格按工具返回的数值排序，不能调整**

## ⛔ SQL 表名规则（重要！）
- **Athena**: 表名格式 `database.table`
  - Athena 用 Trino/Presto SQL 语法，不是 MySQL/SQL Server！
  - timestamp 类型字段不要用 DATE_PARSE()！直接用 CAST 或比较运算
  - 日期差值: date_diff('day', start, end)（不是 DATEDIFF！）
  - 正确: WHERE col >= TIMESTAMP '2025-01-01'
  - 正确: date_diff('day', col, current_timestamp)
  - 错误: DATEDIFF('day', ...) / DATE_PARSE(col, ...) / DATEADD(...)
- **PostgreSQL**: 直接写表名，不要加数据库名前缀
  - 如果表在非 public schema，使用 schema.table 格式

## 工具选择 (严格按优先级)
1. semantic_query → 先查语义层,命中则直接用返回的SQL/参数 ({len(METRICS)}个指标: {metric_names})
2. nl2sql_query → Athena SQL查询 (工具描述中包含实际数据库和表信息)
3. pg_query → PostgreSQL 查询 (工具描述中包含实际数据库和表信息)
4. get_data_catalog → 查看表结构和列描述
5. chatbi_cross_analysis → 跨数据集JOIN

## 规则（严格遵守，违反即失败！）
- ⛔ **工具调用 ≤ 3次** — 超过3次系统会强制终止你！查完立即写答案！
- ⛔ **同一工具同一数据集只调1次** — chatbi_query("battery_health") 调过一次就不许再调！
- ⛔ **工具返回了数据就直接用** — 不需要"验证"或"换个参数再查一遍"
- **chatbi_query 用 group_by + metric 一次拿全**，不要对同一数据集调多次
  ✅ chatbi_query(dataset="battery_health", question="电池SOH分布", group_by="vin", metric="avg:soh_pct")
  ❌ chatbi_query("battery_health",...) → chatbi_query("battery_health",...) → chatbi_query("battery_health",...)
- 跨数据集 → 1次 chatbi_cross_analysis 搞定
## 输出格式

⚠️ 以下输出格式是**强制要求**，每次回答**必须包含**图表和追问建议，缺一不可！

**【强制】图表 — 每次回答必须带 ```chart 代码块：**
任何涉及数字对比、排名、分布的回答，**必须**在文字之后附加图表。格式：

饼图(分布): ```chart
{{"type":"pie","items":[{{"name":"A6","value":673956}},{{"name":"A4","value":595407}}]}}
```

柱状图(对比/排名): ```chart
{{"type":"bar","items":[{{"name":"A6","value":673956}},{{"name":"A4","value":595407}}]}}
```

折线图(趋势): ```chart
{{"type":"line","xAxis":["1月","2月"],"series":[{{"name":"里程","data":[12,15]}}]}}
```

规则:
- 分布 → 饼图; 排名/对比 → 柱状图; 趋势 → 折线图
- 饼图和柱状图用 items 格式，折线图用 xAxis/series 格式
- value 必须是真实数值，不要缩写
- 一次回答可以多个图表
- **没有图表的回答是不合格的！**

**【强制】追问建议 — 每次回答必须带 ```drill 代码块：**
```drill
[{{"title":"按车型细分","desc":"查看各车型详情","query":"各车型的平均行驶里程和速度对比"}},{{"title":"时间趋势","desc":"按月分析变化","query":"最近6个月的行驶里程趋势"}}]
```
- 2-3 个追问，每个有 title/desc/query
- query 是可直接发送的完整问题
- **没有追问建议的回答是不合格的！**

**正文格式：**
- 先 Markdown 表格(紧凑)+关键分析，再图表，最后追问
- 简洁专业，不要emoji，不要重复数据
- 标题用 ## 二级标题
"""


def build_supervisor_data_section(datasource_ids=None):
    """动态生成 Supervisor prompt 中的数据集路由和使用说明
    
    Args:
        datasource_ids: 场景限定的数据源ID列表，None=所有数据源
    """
    # Build dataset routing table
    ds_lines = []
    for name, info in CHATBI_DATASETS.items():
        desc = info.get("desc", "")
        # Extract short description
        short = desc.split("—")[1].strip() if "—" in desc else desc
        short = short[:40] + "..." if len(short) > 40 else short
        ds_lines.append(f"  - {name}: {short}")
    ds_section = "\n".join(ds_lines)
    
    # SQL tables — filtered by scenario datasources
    sql_lines = []
    allowed_engine_types = _resolve_engine_types(datasource_ids) if datasource_ids else None
    try:
        from agentic_core.db_engine import get_multi_engine
        multi = get_multi_engine()
        for key in multi.engine_names:
            if allowed_engine_types is not None and key not in allowed_engine_types:
                continue
            eng = multi.get(key)
            schema = eng.get_schema()
            for tname in schema:
                sql_lines.append(f"  - {tname} ({eng.dialect})")
    except Exception:
        sql_lines = []
    sql_section = "\n".join(sql_lines) if sql_lines else "  无"
    
    # Semantic layer summary
    metric_sample = ", ".join(list(METRICS.keys())[:6])
    dim_sample = ", ".join(list(DIMENSIONS.keys())[:6])
    
    has_data = bool(CHATBI_DATASETS) or bool(METRICS)
    
    if not has_data:
        return """## 数据状态
⚠️ **当前平台暂无已连接的数据源。**

用户询问"有什么数据"时，直接回答：
> 当前平台暂未连接任何数据源。请在管理后台的"数据源"页面添加数据源（支持 S3、DynamoDB、Athena、RDS、Snowflake 等），连接后系统会自动生成语义层并启用数据分析能力。

**绝对不要：**
- 不要列举工具名称来"推断"有什么数据
- 不要说"基于工具能力推断"
- 不要调用 get_fleet_overview / get_vehicle_info 等工具去探测数据
- 不要生成任何数据表格或列表

**简洁回答，引导用户去添加数据源。**
"""
    
    return f"""## 可用数据源

**ChatBI 数据集 (S3 JSON, 通过 deep_data_analysis 查询):**
{ds_section if ds_lines else "  暂无"}

**SQL 数据库 (通过 deep_data_analysis 查询):**
{sql_section}

**语义层 ({len(METRICS)} 指标 / {len(DIMENSIONS)} 维度 / {len(SYNONYMS)} 同义词):**
  指标: {metric_sample if metric_sample else "暂无"}
  维度: {dim_sample if dim_sample else "暂无"}

## 数据查询路由规则
- 所有数据统计/分析/查询/排名/分布/趋势的问题 → 委派 deep_data_analysis
- ⚠️ 每个用户问题最多调用 deep_data_analysis **1次**
- ⚠️ 你没有 chatbi_query / sql_db_query / semantic_query 工具，DataAnalystAgent 有
- 用户提到 "报表"/"仪表盘"/"Dashboard"/"Tableau" → 一律委派 deep_data_analysis
"""


def build_data_catalog(datasource_ids=None):
    """动态构建 DATA_CATALOG, 合并内置 + ChatBI + SQL 引擎
    
    Args:
        datasource_ids: 场景限定的数据源ID列表，None=所有数据源
    """
    tables = []
    
    # 1. Keep existing static tables (Athena telemetry, DynamoDB events, S3 vehicle_info)
    for t in DATA_CATALOG.get("tables", []):
        tables.append(t)
    
    # 2. Add ChatBI datasets that aren't already in DATA_CATALOG
    existing_names = {t["name"].lower() for t in tables}
    for name, info in CHATBI_DATASETS.items():
        check_name = f"chatbi/{name}"
        if not any(check_name in n.lower() for n in existing_names):
            # Generate columns from first record
            try:
                pass  # _load_chatbi removed
                data = _load_chatbi(name)
                if data:
                    cols = [{"name": k, "type": type(v).__name__, "desc": k} 
                            for k, v in data[0].items()]
                    tables.append({
                        "name": f"chatbi/{name} (S3 JSON)",
                        "description": info.get("desc", name),
                        "type": "S3 (ChatBI)",
                        "columns": cols[:15],  # Limit columns
                    })
            except Exception:
                tables.append({
                    "name": f"chatbi/{name} (S3 JSON)",
                    "description": info.get("desc", name),
                    "type": "S3 (ChatBI)",
                    "columns": [],
                })
    
    return tables


def update_tool_descriptions():
    """更新所有工具的 description (在 Agent 创建前调用)"""
    pass  # chatbi/tableau tools removed, no dynamic descriptions to update
