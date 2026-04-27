"""
User Memory — 跨 session 的用户级记忆管理

存储在 DynamoDB config 表，key = "user_memory:{email}"
每次对话结束后自动提炼要点；新 session 注入 system prompt。
"""
import re
import logging
import boto3
from datetime import datetime, timezone
from config import CONFIG_TABLE, REGION

logger = logging.getLogger(__name__)
_ddb = boto3.resource("dynamodb", region_name=REGION)

MAX_MEMORY_ITEMS = 30


def _table():
    return _ddb.Table(CONFIG_TABLE)


def load_user_memory(email: str) -> dict:
    if not email:
        return {"preferences": [], "context": [], "topics": []}
    try:
        resp = _table().get_item(Key={"config_key": f"user_memory:{email}"})
        item = resp.get("Item", {})
        return item.get("data", {"preferences": [], "context": [], "topics": []})
    except Exception as e:
        logger.error(f"load_user_memory failed: {e}")
        return {"preferences": [], "context": [], "topics": []}


def save_user_memory(email: str, memory: dict):
    if not email:
        return
    try:
        _table().put_item(Item={
            "config_key": f"user_memory:{email}",
            "data": memory,
            "updated_at": datetime.now(timezone.utc).isoformat(),
        })
    except Exception as e:
        logger.error(f"save_user_memory failed: {e}")


def format_memory_for_prompt(memory: dict) -> str:
    parts = []
    if memory.get("preferences"):
        parts.append("用户偏好: " + "; ".join(memory["preferences"][-5:]))
    if memory.get("context"):
        parts.append("历史上下文: " + "; ".join(memory["context"][-8:]))
    if memory.get("topics"):
        parts.append("常关注话题: " + ", ".join(memory["topics"][-8:]))
    if not parts:
        return ""
    return "\n\n## 用户记忆 (跨会话)\n" + "\n".join(parts)


def _add_ctx(memory, ctx):
    """Add a context item if not duplicate or near-duplicate."""
    for existing in memory["context"]:
        if ctx in existing or existing in ctx:
            return
    memory["context"].append(ctx)


def extract_memory_from_conversation(question: str, answer: str, existing_memory: dict) -> dict:
    memory = {
        "preferences": list(existing_memory.get("preferences", [])),
        "context": list(existing_memory.get("context", [])),
        "topics": list(existing_memory.get("topics", [])),
    }

    q = question.strip()
    a = answer.strip()[:800]
    qa = q + " " + a  # 同时从问题和回答中提取

    # ═══════════════════════════════════════════
    # 1. TOPICS — 从问题中提取话题
    # ═══════════════════════════════════════════
    topic_keywords = {
        "电池": "电池健康", "SOH": "电池健康", "充电": "充电分析",
        "驾驶": "驾驶行为", "安全": "安全分析", "OTA": "OTA升级",
        "投诉": "客户反馈", "反馈": "客户反馈", "满意度": "客户满意度",
        "NPS": "客户满意度", "售后": "售后服务", "保养": "售后服务",
        "销量": "销量分析", "成交": "销量分析", "App": "车联网App",
        "车联网": "车联网App", "地图": "地理分析", "区域": "地理分析",
        "成本": "成本分析", "费用": "成本分析", "里程": "里程分析",
        "能耗": "能耗分析", "温度": "温控分析", "故障": "故障诊断",
        "召回": "召回管理", "续航": "续航分析", "预测": "预测分析",
        "排名": "排名分析", "TOP": "排名分析", "总结": "数据总结",
        "概览": "数据总结", "报警": "预警监控", "告警": "预警监控",
    }
    for kw, topic in topic_keywords.items():
        if kw in q and topic not in memory["topics"]:
            memory["topics"].append(topic)

    # ═══════════════════════════════════════════
    # 2. CONTEXT — 从问题+回答中提取具体上下文
    # ═══════════════════════════════════════════

    # 2a. VIN
    vins = re.findall(r'\b[A-HJ-NPR-Z0-9]{17}\b', q)
    for vin in vins:
        _add_ctx(memory, f"查询过 VIN {vin}")

    # 2b. 城市/区域 (扩大范围)
    cities = [
        "北京", "上海", "广州", "深圳", "南京", "杭州", "成都", "天津",
        "武汉", "重庆", "苏州", "西安", "郑州", "长沙", "沈阳", "哈尔滨",
        "大连", "青岛", "厦门", "昆明", "贵阳", "合肥", "济南", "太原",
        "东北", "华东", "华南", "华北", "华中", "西南", "西北",
    ]
    for city in cities:
        if city in q:
            _add_ctx(memory, f"关注{city}区域数据")

    # 2c. 车型/品牌
    models = re.findall(r'(Model\s*[SX3Y]|ES[68]|ET[57]|EX\d|EC\d|[A-Z]{2,3}-\d{3,4}|[A-Z]\d{2,3}[a-z]?)', qa, re.IGNORECASE)
    for m in models[:3]:
        _add_ctx(memory, f"关注车型 {m.strip()}")

    # 2d. 时间范围
    time_patterns = [
        (r'(\d{4})[年/-](\d{1,2})[月/-]', lambda m: f"查询过 {m.group(1)}年{m.group(2)}月数据"),
        (r'(最近|近)\s*(\d+)\s*(天|周|月|年)', lambda m: f"查询过最近{m.group(2)}{m.group(3)}数据"),
        (r'(上个月|本月|上周|本周|今年|去年|Q[1-4])', lambda m: f"查询过{m.group(1)}数据"),
    ]
    for pat, fmt in time_patterns:
        m = re.search(pat, q)
        if m:
            _add_ctx(memory, fmt(m))
            break

    # 2e. 具体数值/阈值 (从问题中)
    threshold_patterns = [
        (r'SOH\s*[<>≤≥低于高于不足]\s*(\d+)', lambda m: f"关注SOH阈值{m.group(1)}%"),
        (r'(\d+)\s*[次公里km]+', lambda m: None),  # 跳过普通数值
    ]
    for pat, fmt in threshold_patterns:
        m = re.search(pat, q, re.IGNORECASE)
        if m and fmt:
            ctx = fmt(m)
            if ctx:
                _add_ctx(memory, ctx)

    # 2f. 从回答中提取关键发现（精简版）
    answer_patterns = [
        (r'(\d+)\s*辆[车台].*?(SOH|电池|低于|预警|异常)', f"发现{{}}辆需要关注的车辆"),
        (r'平均\s*SOH\s*[为是约]?\s*(\d+\.?\d*)%', "整体平均SOH为{}%"),
        (r'满意度[为是约]?\s*(\d+\.?\d*)', "客户满意度为{}"),
    ]
    for pat, tpl in answer_patterns:
        m = re.search(pat, a)
        if m:
            _add_ctx(memory, tpl.format(m.group(1)))

    # 2g. 查询的具体数据类型（从问题概括）
    query_types = [
        (["列表", "明细", "清单", "所有"], "查询过详细列表"),
        (["分布", "占比", "比例"], "查询过分布情况"),
        (["排名", "TOP", "最高", "最低", "最多", "最少"], "查询过排名数据"),
        (["汇总", "总计", "统计", "总共", "共计"], "查询过汇总统计"),
        (["同比", "环比", "增长率", "增幅"], "查询过同比环比数据"),
    ]
    for keywords, ctx_text in query_types:
        if any(w in q for w in keywords):
            _add_ctx(memory, ctx_text)

    # ═══════════════════════════════════════════
    # 3. PREFERENCES — 从问题中提取偏好
    # ═══════════════════════════════════════════
    pref_rules = [
        (["对比", "比较", "哪个更", "vs", "VS"], "喜欢对比分析"),
        (["趋势", "变化", "走势", "演变"], "关注趋势变化"),
        (["预警", "风险", "异常", "报警", "告警"], "关注风险预警"),
        (["详细", "深入", "具体", "展开", "详情"], "偏好深度分析"),
        (["导出", "报告", "PDF", "下载"], "需要导出报告"),
        (["图表", "可视化", "柱状图", "饼图", "折线图", "曲线"], "偏好图表可视化"),
        (["简单", "简要", "概括", "一句话"], "偏好简洁回答"),
        (["为什么", "原因", "根因", "根本原因"], "喜欢追问原因"),
        (["建议", "怎么办", "如何改善", "优化"], "注重行动建议"),
        (["跨", "多个", "综合", "全面"], "偏好综合分析"),
    ]
    for keywords, pref in pref_rules:
        if any(w in q for w in keywords) and pref not in memory["preferences"]:
            memory["preferences"].append(pref)

    # ═══════════════════════════════════════════
    # 4. 裁剪
    # ═══════════════════════════════════════════
    memory["preferences"] = memory["preferences"][-MAX_MEMORY_ITEMS:]
    memory["context"] = memory["context"][-MAX_MEMORY_ITEMS:]
    memory["topics"] = memory["topics"][-MAX_MEMORY_ITEMS:]

    return memory
