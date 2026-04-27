"""
agentic_core/alert_rules.py - 动态告警规则引擎

告警规则存储在 DynamoDB config 表, config_key = "alert_rules"
每条规则:
{
    "id": "rule-uuid",
    "name": "电池 SOH 低于阈值",
    "enabled": true,
    "dataset": "battery_health",      # 关联的数据集
    "field": "soh_pct",               # 检测字段
    "operator": "<",                  # <, >, <=, >=, ==, !=, in, not_in
    "threshold": 89,                  # 阈值 (数值或列表)
    "level": "HIGH",                  # CRITICAL, HIGH, MEDIUM, LOW
    "category": "电池衰减",           # 告警分类
    "title_template": "{vin_short} SOH 降至 {value}%",  # 标题模板
    "detail_template": "衰减率 {degradation_rate_pct_month}%/月",  # 详情模板
    "query_template": "VIN {vin} 的电池详细分析",  # 关联查询
    "dedup_field": "vin",             # 去重字段 (每个唯一值只告警一次)
    "created_at": "2026-03-07T00:00:00Z",
    "updated_at": "2026-03-07T00:00:00Z"
}

KPI 规则也可配置:
{
    "id": "kpi-uuid",
    "type": "kpi",
    "name": "平均 SOH",
    "dataset": "battery_health",
    "agg": "avg",                     # avg, sum, count, min, max, count_where
    "field": "soh_pct",
    "dedup_field": "vin",             # 去重后再聚合 (取最新)
    "dedup_order": "date",            # 去重排序字段
    "format": "{value}%",             # 显示格式
    "thresholds": {"good": 92, "warning": 85},  # good >= 92, warning >= 85, else danger
    "query": "电池健康状况如何？",
    "order": 1
}
"""

import json, time, uuid, statistics, operator as op
import boto3
from config import CONFIG_TABLE, REGION

_ops = {
    "<": op.lt, ">": op.gt, "<=": op.le, ">=": op.ge,
    "==": op.eq, "!=": op.ne,
    "in": lambda a, b: a in b,
    "not_in": lambda a, b: a not in b,
}

# ── Storage ──

def _table():
    return boto3.resource("dynamodb", region_name=REGION).Table(CONFIG_TABLE)

def load_alert_rules():
    """Load all alert rules from DynamoDB."""
    try:
        resp = _table().get_item(Key={"config_key": "alert_rules"})
        return json.loads(resp.get("Item", {}).get("data", "[]"))
    except Exception:
        return []

def save_alert_rules(rules):
    """Save all alert rules to DynamoDB."""
    _table().put_item(Item={
        "config_key": "alert_rules",
        "data": json.dumps(rules, ensure_ascii=False, default=str)
    })

def load_kpi_rules():
    """Load all KPI rules from DynamoDB."""
    try:
        resp = _table().get_item(Key={"config_key": "kpi_rules"})
        return json.loads(resp.get("Item", {}).get("data", "[]"))
    except Exception:
        return []

def save_kpi_rules(rules):
    """Save all KPI rules to DynamoDB."""
    _table().put_item(Item={
        "config_key": "kpi_rules",
        "data": json.dumps(rules, ensure_ascii=False, default=str)
    })

# ── Default Rules (seeded on first access) ──

DEFAULT_ALERT_RULES = [
    {
        "id": "default-soh-low", "name": "电池 SOH 低于阈值", "enabled": True,
        "dataset": "battery_health", "field": "soh_pct", "operator": "<", "threshold": 89,
        "level": "HIGH", "category": "电池衰减",
        "title_template": "{vin_short} SOH 降至 {value}%",
        "detail_template": "衰减率 {degradation_rate_pct_month}%/月，电芯不平衡 {cell_imbalance_mv}mV",
        "query_template": "VIN {vin} 的电池详细分析，包括充电习惯和售后记录",
        "dedup_field": "vin", "dedup_order": "date",
    },
    {
        "id": "default-cell-imbalance", "name": "电芯不平衡超标", "enabled": True,
        "dataset": "battery_health", "field": "cell_imbalance_mv", "operator": ">=", "threshold": 45,
        "level": "CRITICAL", "category": "电芯不平衡",
        "title_template": "{vin_short} 电芯不平衡 {value}mV",
        "detail_template": "SOH {soh_pct}%，需立即 BMS 均衡检测",
        "query_template": "VIN {vin} 电芯不平衡分析及维修建议",
        "dedup_field": "vin", "dedup_order": "date",
    },
    {
        "id": "default-feedback-urgent", "name": "紧急投诉待处理", "enabled": True,
        "dataset": "customer_feedback", "field": "severity", "operator": "in", "threshold": ["high", "HIGH", "紧急"],
        "extra_filter": {"field": "status", "operator": "in", "values": ["pending", "open", "处理中"]},
        "level": "MEDIUM", "category": "客户投诉",
        "title_template": "紧急投诉待处理: {category}",
        "detail_template": "{content_short}",
        "query_template": "VIN {vin} 的投诉及关联车辆数据分析",
        "dedup_field": None,
    },
    {
        "id": "default-safety-low", "name": "安全评分低于阈值", "enabled": True,
        "dataset": "driving_daily", "field": "safety_score", "operator": "<", "threshold": 60,
        "level": "HIGH", "category": "驾驶安全",
        "title_template": "{vin_short} 安全评分 {value}",
        "detail_template": "急刹次数 {hard_brake_count}，超速次数 {over_speed_count}",
        "query_template": "VIN {vin} 的驾驶行为详细分析",
        "dedup_field": "vin", "dedup_order": "date",
    },
]

DEFAULT_KPI_RULES = [
    {
        "id": "kpi-avg-soh", "name": "平均 SOH", "dataset": "battery_health",
        "agg": "avg", "field": "soh_pct",
        "dedup_field": "vin", "dedup_order": "date",
        "format": "{value}%", "thresholds": {"good": 92, "warning": 85},
        "query": "电池健康状况如何？哪些车需要预警？", "order": 1,
    },
    {
        "id": "kpi-low-soh", "name": "SOH < 90% 车辆", "dataset": "battery_health",
        "agg": "count_where", "field": "soh_pct", "where_op": "<", "where_val": 90,
        "dedup_field": "vin", "dedup_order": "date",
        "format": "{value}", "thresholds": {"good": 0, "warning": 1, "danger_above": 3},
        "query": "SOH低于90%的车辆详细分析", "order": 2,
    },
    {
        "id": "kpi-open-feedback", "name": "待处理反馈", "dataset": "customer_feedback",
        "agg": "count_where", "field": "status", "where_op": "in", "where_val": ["pending", "open", "处理中"],
        "format": "{value}", "thresholds": {"good": 0, "warning": 5, "danger_above": 10},
        "query": "客户投诉热点是什么？各渠道反馈分布", "order": 3,
    },
    {
        "id": "kpi-satisfaction", "name": "客户满意度", "dataset": "customer_feedback",
        "agg": "avg", "field": "satisfaction",
        "format": "{value}", "thresholds": {"good": 4, "warning": 3},
        "extra_label": "满分 5 分",
        "query": "客户满意度分析，哪些类别评分最低？", "order": 4,
    },
    {
        "id": "kpi-safety", "name": "平均安全评分", "dataset": "driving_daily",
        "agg": "avg", "field": "safety_score",
        "format": "{value}", "thresholds": {"good": 80, "warning": 70},
        "extra_label": "满分 100",
        "query": "安全评分最低的车辆有哪些？", "order": 5,
    },
    {
        "id": "kpi-ota", "name": "OTA 成功率", "dataset": "ota_records",
        "agg": "success_rate", "field": "success", "success_values": [True, 1, "true", "True"],
        "format": "{value}%", "thresholds": {"good": 95, "warning": 85},
        "query": "OTA升级成功率多少？失败的主要原因？", "order": 6,
    },
    {
        "id": "kpi-charge-cost", "name": "总充电费用", "dataset": "charging_records",
        "agg": "sum", "field": "cost_yuan",
        "format": "¥{value:,.0f}", "thresholds": {},
        "query": "充电习惯分析：快充vs慢充比例", "order": 7,
    },
]

def ensure_defaults():
    """Seed default rules if none exist."""
    if not load_alert_rules():
        save_alert_rules(DEFAULT_ALERT_RULES)
    if not load_kpi_rules():
        save_kpi_rules(DEFAULT_KPI_RULES)

# ── Evaluation Engine ──

def _dedup_latest(data, dedup_field, order_field):
    """Keep only the latest record per dedup_field value."""
    if not dedup_field:
        return data
    latest = {}
    for r in data:
        key = r.get(dedup_field, "")
        if key not in latest or str(r.get(order_field, "")) > str(latest[key].get(order_field, "")):
            latest[key] = r
    return list(latest.values())

def _format_template(template, record, value=None):
    """Format a template string with record fields."""
    ctx = dict(record)
    if value is not None:
        ctx["value"] = value
    # Add computed fields
    vin = ctx.get("vin", "")
    ctx["vin_short"] = vin[-6:] if len(vin) > 6 else vin
    ctx["content_short"] = str(ctx.get("content", ""))[:80]
    try:
        return template.format(**ctx)
    except (KeyError, ValueError):
        return template

def evaluate_alerts(datasets: dict) -> list:
    """Evaluate all alert rules against loaded datasets.
    datasets: {"battery_health": [...], "customer_feedback": [...], ...}
    """
    rules = load_alert_rules()
    alerts = []
    
    for rule in rules:
        if not rule.get("enabled", True):
            continue
        ds_name = rule.get("dataset", "")
        data = datasets.get(ds_name, [])
        if not data:
            continue
        
        # Dedup
        data = _dedup_latest(data, rule.get("dedup_field"), rule.get("dedup_order", "date"))
        
        field = rule.get("field", "")
        threshold = rule.get("threshold")
        op_name = rule.get("operator", "<")
        op_fn = _ops.get(op_name)
        if not op_fn:
            continue
        
        for record in data:
            val = record.get(field)
            if val is None:
                continue
            
            # Check extra filter
            extra = rule.get("extra_filter")
            if extra:
                ef_val = record.get(extra["field"])
                ef_op = _ops.get(extra["operator"])
                if ef_op and not ef_op(ef_val, extra.get("values", extra.get("threshold"))):
                    continue
            
            try:
                if not op_fn(val, threshold):
                    continue
            except (TypeError, ValueError):
                continue
            
            alerts.append({
                "level": rule.get("level", "MEDIUM"),
                "category": rule.get("category", ""),
                "title": _format_template(rule.get("title_template", ""), record, val),
                "detail": _format_template(rule.get("detail_template", ""), record, val),
                "query": _format_template(rule.get("query_template", ""), record, val),
                "time": record.get("date", ""),
                "rule_id": rule["id"],
            })
    
    # Sort: CRITICAL > HIGH > MEDIUM > LOW
    order = {"CRITICAL": 0, "HIGH": 1, "MEDIUM": 2, "LOW": 3}
    alerts.sort(key=lambda a: order.get(a.get("level", "LOW"), 9))
    return alerts

def evaluate_kpis(datasets: dict) -> list:
    """Evaluate all KPI rules against loaded datasets."""
    rules = load_kpi_rules()
    kpis = []
    
    for rule in sorted(rules, key=lambda r: r.get("order", 99)):
        ds_name = rule.get("dataset", "")
        data = datasets.get(ds_name, [])
        if not data:
            continue
        
        # Dedup if needed
        data = _dedup_latest(data, rule.get("dedup_field"), rule.get("dedup_order", "date"))
        
        field = rule.get("field", "")
        agg = rule.get("agg", "avg")
        
        try:
            if agg == "avg":
                vals = [r[field] for r in data if isinstance(r.get(field), (int, float))]
                value = round(statistics.mean(vals), 1) if vals else 0
            elif agg == "sum":
                vals = [r[field] for r in data if isinstance(r.get(field), (int, float))]
                value = round(sum(vals), 0)
            elif agg == "count":
                value = len(data)
            elif agg == "min":
                vals = [r[field] for r in data if isinstance(r.get(field), (int, float))]
                value = min(vals) if vals else 0
            elif agg == "max":
                vals = [r[field] for r in data if isinstance(r.get(field), (int, float))]
                value = max(vals) if vals else 0
            elif agg == "count_where":
                w_op = _ops.get(rule.get("where_op", "<"))
                w_val = rule.get("where_val")
                value = sum(1 for r in data if w_op and w_op(r.get(field), w_val))
            elif agg == "success_rate":
                success_vals = rule.get("success_values", [True])
                total = len(data)
                success = sum(1 for r in data if r.get(field) in success_vals)
                value = round(success / total * 100, 1) if total > 0 else 0
            else:
                continue
        except Exception:
            continue
        
        # Determine status from thresholds
        thresholds = rule.get("thresholds", {})
        status = "good"
        if "danger_above" in thresholds and isinstance(value, (int, float)):
            if value >= thresholds["danger_above"]:
                status = "danger"
            elif value >= thresholds.get("warning", 0):
                status = "warning"
        elif thresholds.get("good") is not None:
            if value >= thresholds["good"]:
                status = "good"
            elif value >= thresholds.get("warning", 0):
                status = "warning"
            else:
                status = "danger"
        
        # Format value
        fmt = rule.get("format", "{value}")
        try:
            display = fmt.format(value=value)
        except Exception:
            display = str(value)
        
        kpis.append({
            "id": rule["id"],
            "label": rule["name"],
            "value": display,
            "trend": None,
            "trend_label": rule.get("extra_label", ""),
            "status": status,
            "query": rule.get("query", ""),
            "rule_id": rule["id"],
        })
    
    return kpis
