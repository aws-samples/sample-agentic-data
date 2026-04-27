"""
Semantic Layer — 业务语义到数据查询的确定性映射。
将指标定义、维度关系、同义词、计算规则从 LLM prompt 中分离出来，
确保相同问题每次生成一致的查询。
"""
from agentic_core.db_engine import _safe_identifier as _safe_id

# ═══════ 指标定义 (Metrics) ═══════
# Dynamic — populated from connected datasources
METRICS = {}

# ═══════ 维度定义 (Dimensions) ═══════
# Dynamic — populated from connected datasources
DIMENSIONS = {}

# ═══════ 同义词 / 别名 ═══════
# Dynamic — populated from connected datasources
SYNONYMS = {}

# ═══════ JOIN 关系 ═══════
# 表关联关系 — 动态从数据源推断, 不硬编码
JOINS = {}

# ═══════ 预定义查询模板 (Query Templates) ═══════
# Dynamic — populated from connected datasources
QUERY_TEMPLATES = {}
JOINS = {}  # Table join mappings: {"name": {"left": ..., "right": ..., "on": ..., "type": ..., "engine": ...}}


# ═══════ 语义解析器 ═══════
def resolve_synonym(term: str) -> str:
    """将用户用语解析为标准术语。"""
    return SYNONYMS.get(term, term)


def find_matching_metrics(question: str) -> list:
    """从问题中识别涉及的指标。"""
    found = []
    # Check all metric names and synonyms
    all_terms = list(METRICS.keys()) + list(SYNONYMS.keys())
    for term in all_terms:
        if term in question:
            canonical = resolve_synonym(term)
            if canonical in METRICS and canonical not in found:
                found.append(canonical)
    return found


def find_matching_dimensions(question: str) -> list:
    """从问题中识别涉及的维度。"""
    found = []
    all_terms = list(DIMENSIONS.keys()) + [k for k, v in SYNONYMS.items() if v in DIMENSIONS]
    for term in all_terms:
        if term in question:
            canonical = resolve_synonym(term)
            if canonical in DIMENSIONS and canonical not in found:
                found.append(canonical)
    return found


# Template keyword rules (module level for external access)
template_keywords = {
        "各车型销量": ["车型", "销量"],
        "各车型NPS": ["车型", "NPS"],
        "各车型售后成本": ["车型", "售后"],
        "各城市销量": ["城市", "销量"],
        "客户画像": ["客户", "画像"],
        "充电习惯": ["充电"],
        "OTA成功率": ["OTA"],
}

def find_matching_templates(question: str) -> list:
    """匹配预定义查询模板。"""
    matches = []
    for name, keywords in template_keywords.items():
        if name in QUERY_TEMPLATES and all(kw.lower() in question.lower() for kw in keywords):
            matches.append((name, QUERY_TEMPLATES[name]))
    return matches


def get_semantic_context(question: str, datasource_filter=None) -> str:
    """生成语义层上下文，注入到 DataAnalystAgent prompt 中。
    
    Args:
        question: 用户问题
        datasource_filter: 可选的数据源ID列表，只匹配这些数据源的指标/维度
    """
    metrics = find_matching_metrics(question)
    dims = find_matching_dimensions(question)
    templates = find_matching_templates(question)

    # Filter by datasource if specified
    if datasource_filter:
        ds_set = set(datasource_filter)
        metrics = [m for m in metrics if not METRICS[m].get('datasource') or METRICS[m].get('datasource') in ds_set]
        dims = [d for d in dims if not DIMENSIONS[d].get('datasource') or DIMENSIONS[d].get('datasource') in ds_set]

    parts = ["## 语义层解析结果\n"]

    if metrics:
        parts.append("### 识别到的指标")
        for m in metrics:
            info = METRICS[m]
            engine = info.get('engine', 'athena')
            tool_hint = "nl2sql_query" if engine == "athena" else "pg_query"
            line = f"- **{m}** ({info['id']}): {info['description']}"
            if 'sql' in info:
                line += f" → SQL: `{info['sql']}`"
            line += f" → 执行工具: **{tool_hint}**"
            if 'unit' in info:
                line += f" [{info['unit']}]"
            if info.get('datasource'):
                line += f" [数据源: {info['datasource']}]"
            parts.append(line)

    if dims:
        parts.append("\n### 识别到的维度")
        for d in dims:
            info = DIMENSIONS[d]
            line = f"- **{d}** ({info['id']})"
            if 'rds_column' in info:
                line += f" → RDS column: `{info['rds_column']}`"
            if 'chatbi_field' in info:
                line += f" → ChatBI field: `{info['chatbi_field']}`"
            parts.append(line)

    if templates:
        parts.append("\n### 匹配的预定义查询（优先使用）")
        for name, tmpl in templates:
            parts.append(f"- **{name}**: {tmpl['description']}")
            if 'sql' in tmpl:
                parts.append(f"  ```sql\n  {tmpl['sql']}\n  ```")
            elif 'params' in tmpl:
                parts.append(f"  → {tmpl['tool']}({tmpl['params']})")

    # Add relevant join mappings
    if metrics or dims:
        relevant_joins = []
        matched_tables = set()
        for m in metrics:
            tbl = METRICS[m].get('table', '')
            if tbl: matched_tables.add(tbl)
        for j_name, j_info in JOINS.items():
            engine = j_info.get('engine', '')
            if datasource_filter:
                # Check engine compatibility
                pass  # joins are engine-tagged, filtering happens at query time
            if matched_tables:
                if any(t in j_info.get('left','') or t in j_info.get('right','') for t in matched_tables):
                    relevant_joins.append((j_name, j_info))
            else:
                relevant_joins.append((j_name, j_info))
        if relevant_joins:
            parts.append("\n### 表关联映射（跨表查询时使用）")
            for j_name, j_info in relevant_joins:
                parts.append(f"- **{j_name}**: {j_info.get('description', '')} → `{j_info['left']} {j_info['type']} {j_info['right']} ON {j_info['on']}`")

    if not metrics and not dims and not templates:
        parts.append("未匹配到预定义指标/维度，请根据数据字典自由查询。")

    return "\n".join(parts)


def get_full_semantic_spec() -> str:
    """返回完整的语义层规范（给管理后台展示）。"""
    lines = ["# 语义层定义\n"]

    lines.append("## 指标 (Metrics)\n")
    lines.append("| 指标名 | ID | 描述 | 数据源 | SQL/查询 | 单位 |")
    lines.append("|--------|------|------|--------|---------|------|")
    for name, m in METRICS.items():
        sql = m.get('sql', m.get('sql_note', f"chatbi:{m.get('dataset','')}"))
        lines.append(f"| {name} | {m.get('id', name)} | {m.get('description', '')} | {m.get('source', m.get('table', '-'))} | `{sql[:50]}` | {m.get('unit','')} |")

    lines.append(f"\n## 维度 (Dimensions)\n")
    lines.append("| 维度名 | ID | RDS字段 | ChatBI字段 | 可选值 |")
    lines.append("|--------|------|---------|-----------|--------|")
    for name, d in DIMENSIONS.items():
        lines.append(f"| {name} | {d.get('id', name)} | {d.get('rds_column','-')} | {d.get('chatbi_field','-')} | {', '.join(str(v) for v in d.get('values',[])[:5])} |")

    lines.append(f"\n## 同义词 ({len(SYNONYMS)} 条)\n")
    by_target = {}
    for syn, target in SYNONYMS.items():
        by_target.setdefault(target, []).append(syn)
    for target, syns in sorted(by_target.items()):
        lines.append(f"- **{target}**: {', '.join(syns)}")

    lines.append(f"\n## 预定义查询模板 ({len(QUERY_TEMPLATES)} 个)\n")
    for name, tmpl in QUERY_TEMPLATES.items():
        lines.append(f"### {name}\n{tmpl['description']}")
        if 'sql' in tmpl:
            lines.append(f"```sql\n{tmpl['sql']}\n```")

    if JOINS:
        lines.append(f"\n## 表关联映射 ({len(JOINS)} 个)\n")
        for j_name, j_info in JOINS.items():
            lines.append(f"- **{j_name}** [{j_info.get('engine','')}]: {j_info.get('description','')}")
            lines.append(f"  `{j_info['left']} {j_info['type']} {j_info['right']} ON {j_info['on']}`")

    return "\n".join(lines)


# ═══════ PG Semantic Layer Generator ═══════

def generate_pg_semantic(tables_info, datasource_id="", database=""):
    """Generate semantic layer metrics/dimensions/synonyms for PostgreSQL tables.
    
    Args:
        tables_info: list of {"name": str, "columns": [{"name": str, "type": str}], "row_count": int}
        datasource_id: datasource identifier for filtering
        database: database name
    
    Returns:
        dict with metrics, dimensions, synonyms, templates
    """
    metrics = {}
    dimensions = {}
    synonyms = {}
    templates = {}
    
    # Common numeric aggregation types
    numeric_types = {"integer", "bigint", "numeric", "real", "double precision", "smallint", "decimal", "float"}
    # Common dimension types  
    dim_types = {"character varying", "text", "varchar", "date", "timestamp", "timestamp without time zone", "timestamp with time zone", "boolean"}
    
    for table in tables_info:
        tname = table["name"]
        cols = table.get("columns", [])
        row_count = table.get("row_count", 0)
        
        numeric_cols = [c for c in cols if c.get("type", "").lower() in numeric_types]
        text_cols = [c for c in cols if c.get("type", "").lower() in dim_types or "char" in c.get("type", "").lower() or "text" in c.get("type", "").lower()]
        date_cols = [c for c in cols if "date" in c.get("type", "").lower() or "timestamp" in c.get("type", "").lower()]
        
        # Generate metrics for numeric columns
        for col in numeric_cols:
            cname = col["name"]
            # Skip ID/FK columns
            if cname.endswith("_id") or cname == "id":
                continue
            
            # SUM metric
            display_name = f"{tname}_{cname}_总计"
            metrics[display_name] = {
                "id": f"{tname}.{cname}.sum",
                "description": f"{tname}表 {cname} 的总计",
                "sql": f"SELECT SUM({_safe_id(cname)}) AS total FROM {_safe_id(tname)}",  # nosec B608
                "engine": "postgresql",
                "datasource": datasource_id,
                "table": tname,
                "unit": "",
            }
            
            # AVG metric
            avg_name = f"{tname}_{cname}_平均"
            metrics[avg_name] = {
                "id": f"{tname}.{cname}.avg",
                "description": f"{tname}表 {cname} 的平均值",
                "sql": f"SELECT ROUND(AVG({_safe_id(cname)})::numeric, 2) AS average FROM {_safe_id(tname)}",  # nosec B608
                "engine": "postgresql",
                "datasource": datasource_id,
                "table": tname,
                "unit": "",
            }
        
        # COUNT metric per table
        count_name = f"{tname}_记录数"
        metrics[count_name] = {
            "id": f"{tname}.count",
            "description": f"{tname}表的记录总数",
            "sql": f"SELECT COUNT(*) AS cnt FROM {_safe_id(tname)}",  # nosec B608
            "engine": "postgresql",
            "datasource": datasource_id,
            "table": tname,
            "unit": "条",
        }
        
        # Generate dimensions for text/date columns
        for col in text_cols:
            cname = col["name"]
            if cname.endswith("_id") or cname == "id":
                continue
            dim_display = f"{tname}_{cname}"
            dimensions[dim_display] = {
                "id": f"{tname}.{cname}",
                "description": f"{tname}表的{cname}维度",
                "rds_column": cname,
                "chatbi_field": cname,
                "values": [],
                "engine": "postgresql",
                "datasource": datasource_id,
                "table": tname,
            }
        
        for col in date_cols:
            cname = col["name"]
            dim_display = f"{tname}_{cname}"
            dimensions[dim_display] = {
                "id": f"{tname}.{cname}",
                "description": f"{tname}表的{cname}时间维度",
                "rds_column": cname,
                "chatbi_field": cname,
                "values": [],
                "engine": "postgresql",
                "datasource": datasource_id,
                "table": tname,
            }
    
    # Generate cross-table synonyms based on common patterns
    _synonym_patterns = {
        "产量": ["actual_qty", "planned_qty", "target_qty", "completed_qty"],
        "缺陷": ["defect_qty", "defect_code", "defect_desc"],
        "良品率": ["yield_rate", "defect_qty"],
        "停机": ["downtime_minutes", "downtime"],
        "产线": ["line_name", "line_id", "line_type"],
        "工厂": ["factory"],
        "车型": ["model_name"],
        "班次": ["shift"],
        "订单": ["order_id", "order_date", "order_status"],
        "质检": ["inspection_type", "inspection_id", "result"],
        "设备": ["equipment_name", "equipment_type", "equipment_id"],
        "维护": ["maintenance", "last_maintenance", "next_maintenance"],
        "能耗": ["energy_kwh"],
        "产能": ["capacity_per_day"],
    }
    
    all_col_names = set()
    for table in tables_info:
        for col in table.get("columns", []):
            all_col_names.add(col["name"])
    
    for syn_term, target_cols in _synonym_patterns.items():
        for tc in target_cols:
            if tc in all_col_names:
                # Find the metric or dimension that contains this column
                for mname, minfo in metrics.items():
                    if tc in minfo.get("sql", ""):
                        synonyms[syn_term] = mname
                        break
                else:
                    for dname, dinfo in dimensions.items():
                        if dinfo.get("rds_column") == tc:
                            synonyms[syn_term] = dname
                            break
                break  # Only first match
    
    return {
        "metrics": metrics,
        "dimensions": dimensions,
        "synonyms": synonyms,
        "templates": templates,
    }


# ═══════ Universal Semantic Layer Generator ═══════

def generate_semantic(engine, tables_info, datasource_id="", database=""):
    """Generate semantic layer metrics/dimensions/synonyms for any SQL engine.
    
    Args:
        engine: 'postgresql', 'athena', 'snowflake'
        tables_info: list of {"name": str, "columns": [{"name": str, "type": str}], "row_count": int}
        datasource_id: datasource identifier
        database: database name (used as table prefix for Athena)
    
    Returns:
        dict with metrics, dimensions, synonyms, templates
    """
    metrics = {}
    dimensions = {}
    synonyms = {}
    templates = {}
    
    # Engine-specific SQL syntax
    if engine == "athena":
        numeric_types = {"int", "integer", "bigint", "float", "double", "decimal", "tinyint", "smallint"}
        dim_types = {"string", "varchar", "char", "date", "timestamp"}
        avg_fn = lambda col, tbl: f"SELECT ROUND(AVG(CAST({_safe_id(col)} AS DOUBLE)), 2) AS average FROM {_safe_id(tbl)}"  # nosec B608
        sum_fn = lambda col, tbl: f"SELECT SUM({_safe_id(col)}) AS total FROM {_safe_id(tbl)}"  # nosec B608
        cnt_fn = lambda tbl: f"SELECT COUNT(*) AS cnt FROM {_safe_id(tbl)}"  # nosec B608
        table_prefix = f"{database}." if database else ""
    elif engine == "snowflake":
        numeric_types = {"number", "decimal", "numeric", "int", "integer", "bigint", "smallint", "tinyint", "float", "float4", "float8", "double", "double precision", "real"}
        dim_types = {"varchar", "char", "character", "string", "text", "date", "timestamp", "timestamp_ltz", "timestamp_ntz", "timestamp_tz", "boolean"}
        avg_fn = lambda col, tbl: f"SELECT ROUND(AVG({_safe_id(col)}), 2) AS average FROM {_safe_id(tbl)}"  # nosec B608
        sum_fn = lambda col, tbl: f"SELECT SUM({_safe_id(col)}) AS total FROM {_safe_id(tbl)}"  # nosec B608
        cnt_fn = lambda tbl: f"SELECT COUNT(*) AS cnt FROM {_safe_id(tbl)}"  # nosec B608
        table_prefix = ""
    else:  # postgresql
        numeric_types = {"integer", "bigint", "numeric", "real", "double precision", "smallint", "decimal", "float"}
        dim_types = {"character varying", "text", "varchar", "date", "timestamp", "timestamp without time zone", "timestamp with time zone", "boolean"}
        avg_fn = lambda col, tbl: f"SELECT ROUND(AVG({_safe_id(col)})::numeric, 2) AS average FROM {_safe_id(tbl)}"  # nosec B608
        sum_fn = lambda col, tbl: f"SELECT SUM({_safe_id(col)}) AS total FROM {_safe_id(tbl)}"  # nosec B608
        cnt_fn = lambda tbl: f"SELECT COUNT(*) AS cnt FROM {_safe_id(tbl)}"  # nosec B608
        table_prefix = ""
    
    for table in tables_info:
        tname = table["name"]
        cols = table.get("columns", [])
        
        numeric_cols = [c for c in cols if c.get("type", "").lower() in numeric_types]
        text_cols = [c for c in cols if c.get("type", "").lower() in dim_types or "char" in c.get("type", "").lower() or "text" in c.get("type", "").lower()]
        date_cols = [c for c in cols if "date" in c.get("type", "").lower() or "timestamp" in c.get("type", "").lower()]
        
        # Remove duplicates (date cols may overlap with text cols)
        text_cols = [c for c in text_cols if c not in date_cols]
        
        for col in numeric_cols:
            cname = col["name"]
            if cname.endswith("_id") or cname == "id":
                continue
            
            sql_table = f"{table_prefix}{tname}"
            
            metrics[f"{tname}_{cname}_总计"] = {
                "id": f"{tname}.{cname}.sum",
                "description": f"{tname}表 {cname} 的总计",
                "sql": sum_fn(cname, sql_table),
                "engine": engine,
                "datasource": datasource_id,
                "table": f"{table_prefix}{tname}" if engine == "athena" else tname,
                "unit": "",
            }
            
            metrics[f"{tname}_{cname}_平均"] = {
                "id": f"{tname}.{cname}.avg",
                "description": f"{tname}表 {cname} 的平均值",
                "sql": avg_fn(cname, sql_table),
                "engine": engine,
                "datasource": datasource_id,
                "table": f"{table_prefix}{tname}" if engine == "athena" else tname,
                "unit": "",
            }
        
        # COUNT metric
        sql_table = f"{table_prefix}{tname}"
        metrics[f"{tname}_记录数"] = {
            "id": f"{tname}.count",
            "description": f"{tname}表的记录总数",
            "sql": cnt_fn(sql_table),
            "engine": engine,
            "datasource": datasource_id,
            "table": f"{table_prefix}{tname}" if engine == "athena" else tname,
            "unit": "条",
        }
        
        # Dimensions
        for col in text_cols + date_cols:
            cname = col["name"]
            if cname.endswith("_id") or cname == "id":
                continue
            is_date = col in date_cols
            dimensions[f"{tname}_{cname}"] = {
                "id": f"{tname}.{cname}",
                "description": f"{tname}表的{cname}" + ("时间维度" if is_date else "维度"),
                "rds_column": cname,
                "chatbi_field": cname,
                "values": [],
                "engine": engine,
                "datasource": datasource_id,
                "table": f"{table_prefix}{tname}" if engine == "athena" else tname,
            }
    
    # Common synonym patterns
    _patterns = {
        "产量": ["actual_qty", "planned_qty", "completed_qty", "quantity"],
        "缺陷": ["defect_qty", "defect_code", "defect_desc", "defect"],
        "良品率": ["yield_rate", "pass_rate"],
        "停机": ["downtime_minutes", "downtime"],
        "工厂": ["factory", "plant"],
        "车型": ["model_name", "model"],
        "订单": ["order_id", "order_date"],
        "设备": ["equipment_name", "equipment_type"],
        "能耗": ["energy_kwh", "energy"],
        "里程": ["mileage_km", "mileage", "effective_mileage_km"],
        "车辆": ["vin", "vehicle"],
        "品牌": ["brand"],
        "状态": ["status", "state"],
        "日期": ["date", "production_date", "created_at"],
        "金额": ["amount", "total_amount", "revenue", "cost"],
        "数量": ["count", "qty", "quantity"],
        "评分": ["rating", "score", "satisfaction"],
    }
    
    all_col_names = set()
    for table in tables_info:
        for col in table.get("columns", []):
            all_col_names.add(col["name"])
    
    for syn_term, target_cols in _patterns.items():
        for tc in target_cols:
            if tc in all_col_names:
                for mname, minfo in metrics.items():
                    if tc in minfo.get("sql", ""):
                        synonyms[syn_term] = mname
                        break
                else:
                    for dname, dinfo in dimensions.items():
                        if dinfo.get("rds_column") == tc:
                            synonyms[syn_term] = dname
                            break
                break
    
    return {"metrics": metrics, "dimensions": dimensions, "synonyms": synonyms, "templates": templates}
