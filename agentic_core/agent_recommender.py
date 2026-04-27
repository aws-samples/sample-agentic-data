"""
Agent Recommender — 基于已连接数据源自动推荐 Agent 架构

核心逻辑 (纯规则引擎, 零 LLM 调用):
1. 分析所有已连接数据源的 schema (字段名、类型、业务含义)
2. 将数据集归类到业务域 (销售、客户、运维、安全...)
3. 基于业务域数量决定 Agent 拆分策略
4. 为每个 Agent 生成 prompt、绑定工具和数据集
5. 生成测试用例
"""

import re
from typing import Any

# ────── 业务域分类规则 ──────
DOMAIN_RULES = {
    "sales": {
        "name_zh": "销售分析",
        "field_patterns": ["sales", "revenue", "price", "discount", "order", "deal",
                          "amount", "payment", "channel", "成交", "营收", "销量", "订单"],
        "dataset_patterns": ["sale", "order", "revenue", "transaction", "deal"],
    },
    "customer": {
        "name_zh": "客户洞察",
        "field_patterns": ["customer", "user", "member", "gender", "age", "loyalty",
                          "satisfaction", "feedback", "complaint", "nps", "客户", "用户",
                          "满意度", "投诉", "反馈"],
        "dataset_patterns": ["customer", "user", "member", "feedback", "complaint", "survey"],
    },
    "operations": {
        "name_zh": "运营分析",
        "field_patterns": ["usage", "daily", "monthly", "active", "session", "retention",
                          "churn", "frequency", "engagement", "活跃", "留存", "使用"],
        "dataset_patterns": ["usage", "activity", "engagement", "daily", "operations", "app_usage"],
    },
    "finance": {
        "name_zh": "财务分析",
        "field_patterns": ["cost", "profit", "budget", "expense", "margin", "roi",
                          "成本", "利润", "费用", "预算"],
        "dataset_patterns": ["finance", "cost", "budget", "expense", "profit", "accounting"],
    },
    "product": {
        "name_zh": "产品分析",
        "field_patterns": ["product", "sku", "category", "brand", "model", "version",
                          "feature", "产品", "品类", "型号", "版本"],
        "dataset_patterns": ["product", "catalog", "inventory", "sku"],
    },
    "safety": {
        "name_zh": "安全监控",
        "field_patterns": ["event", "incident", "alert", "severity", "risk", "safety",
                          "sensor", "collision", "brake", "事件", "事故", "风险", "告警"],
        "dataset_patterns": ["event", "incident", "alert", "safety", "risk", "sensor"],
    },
    "quality": {
        "name_zh": "质量管理",
        "field_patterns": ["defect", "failure", "quality", "inspection", "warranty",
                          "repair", "maintenance", "health", "soh", "故障", "维修", "质量", "健康"],
        "dataset_patterns": ["quality", "defect", "repair", "maintenance", "health", "battery",
                            "service", "warranty", "inspection"],
    },
    "logistics": {
        "name_zh": "物流分析",
        "field_patterns": ["delivery", "shipping", "warehouse", "inventory", "route",
                          "distance", "location", "配送", "仓储", "物流", "库存"],
        "dataset_patterns": ["logistics", "delivery", "shipping", "warehouse", "inventory"],
    },
    "marketing": {
        "name_zh": "营销分析",
        "field_patterns": ["campaign", "conversion", "click", "impression", "lead",
                          "funnel", "utm", "转化", "点击", "投放", "渠道"],
        "dataset_patterns": ["marketing", "campaign", "ads", "promotion", "lead"],
    },
    "hr": {
        "name_zh": "人力分析",
        "field_patterns": ["employee", "salary", "department", "position", "attendance",
                          "performance", "hire", "员工", "薪资", "考勤", "绩效", "部门"],
        "dataset_patterns": ["employee", "hr", "staff", "payroll", "attendance"],
    },
}


def classify_datasets(datasources, semantic=None):
    """将数据集归类到业务域。返回 {domain_id: [dataset_info, ...]}"""
    if semantic is None:
        semantic = {}
    domain_map = {}
    unclassified = []

    for ds in datasources:
        name = ds.get("name", "").lower()
        ds_type = ds.get("type", "")
        config = ds.get("config", {})
        
        fields = []
        if "schema" in ds:
            fields = [f.get("name", "").lower() for f in ds.get("schema", {}).get("fields", [])]
        elif "fields" in config:
            fields = [f.lower() for f in config.get("fields", [])]
        # 也从 chatbi dataset 的 columns 获取字段
        if "columns" in ds:
            fields.extend([c.lower() for c in ds.get("columns", [])])

        prefix = config.get("prefix", name)
        for mk in semantic.get("metrics", {}):
            if prefix in mk.lower():
                fields.append(mk.lower())
        for dk in semantic.get("dimensions", {}):
            if prefix in dk.lower():
                fields.append(dk.lower())

        all_text = " ".join([name] + fields)
        
        scores = {}
        for domain_id, rule in DOMAIN_RULES.items():
            score = 0
            for pat in rule["dataset_patterns"]:
                if pat in name:
                    score += 3
            for pat in rule["field_patterns"]:
                if pat in all_text:
                    score += 1
            if score > 0:
                scores[domain_id] = score
        
        ds_info = {
            "name": ds.get("name", ""),
            "type": ds_type,
            "fields": fields[:30],
            "record_count": ds.get("record_count", 0),
        }
        
        if scores:
            best_domain = max(scores, key=scores.get)
            domain_map.setdefault(best_domain, []).append(ds_info)
        else:
            unclassified.append(ds_info)
    
    if unclassified:
        domain_map["general"] = unclassified
    
    return domain_map


def _build_agent_prompt(domain_id, domain_info, datasets):
    """为一个业务域生成 Agent prompt"""
    name_zh = domain_info.get("name_zh", "数据分析")
    ds_names = [d["name"] for d in datasets]
    ds_desc = ", ".join(ds_names)
    
    all_fields = []
    for d in datasets:
        all_fields.extend(d.get("fields", [])[:15])
    field_str = ", ".join(sorted(set(all_fields))[:20]) if all_fields else "(请先查询数据集获取字段)"

    return (
        f"你是{name_zh}专家。基于以下数据集回答分析问题。\n\n"
        f"## 数据集\n{ds_desc}\n\n"
        f"## 关键字段\n{field_str}\n\n"
        f"## 规则\n"
        f"1. 所有数字必须来自工具返回的真实数据，绝不编造\n"
        f"2. 先调用 list_datasets 确认可用数据\n"
        f"3. 工具调用 ≤ 3 次\n"
        f"4. 无数据时明确告知用户\n"
        f"5. 中文回答，技术术语保留英文\n"
    )


def _pick_tools(domain_id, datasets):
    """为业务域选择工具"""
    base = ["semantic_query", "get_data_catalog"]
    # 根据数据源类型添加查询工具
    ds_types = {d.get("type", "").lower() for d in datasets}
    # RDS 也看 config.engine
    for d in datasets:
        if d.get("type", "").upper() == "RDS":
            ds_types.add(d.get("config", {}).get("engine", "mysql").lower())
    if any(t in ds_types for t in ("rds", "mysql", "postgresql", "sqlite")):
        base.append("pg_query")
    if "snowflake" in ds_types:
        base.append("snowflake_query")
    if "athena" in ds_types:
        base.append("nl2sql_query")
    return list(dict.fromkeys(base))


def _generate_test_cases(domain_id, domain_info, datasets):
    """生成测试用例"""
    name_zh = domain_info.get("name_zh", "数据")
    ds_names = [d["name"] for d in datasets]
    
    cases = [{
        "question": f"{name_zh}数据的总体情况如何？",
        "expected_keywords": [],
        "description": f"验证 Agent 能访问{name_zh}数据并返回概览",
    }]
    
    if len(ds_names) > 1:
        cases.append({
            "question": f"{ds_names[0]}和{ds_names[1]}之间有什么关联？",
            "expected_keywords": [],
            "description": "验证跨数据集分析能力",
        })
    
    all_fields = []
    for d in datasets:
        all_fields.extend(d.get("fields", []))
    
    numeric_hints = ["amount", "count", "price", "score", "rate", "revenue",
                     "sales", "cost", "total", "quantity"]
    for f in all_fields:
        if any(h in f for h in numeric_hints):
            cases.append({
                "question": f"{f} 的分布和 Top 5 是什么？",
                "expected_keywords": [],
                "description": f"验证对 {f} 的聚合分析",
            })
            break
    
    return cases[:5]


def recommend(datasources, semantic=None, model_provider="bedrock"):
    """
    基于已连接数据源推荐完整 Agent 架构。
    
    Returns: {supervisor, sub_agents, test_cases, summary, domain_map}
    """
    if semantic is None:
        semantic = {}
    
    if not datasources:
        return {
            "supervisor": None, "sub_agents": [], "test_cases": [],
            "summary": "暂无已连接的数据源。请先添加数据源。", "domain_map": {},
        }
    
    domain_map = classify_datasets(datasources, semantic)
    domains = list(domain_map.keys())
    total_datasets = sum(len(v) for v in domain_map.values())
    
    sub_agents = []
    all_test_cases = []
    
    if len(domains) == 1 and total_datasets <= 3:
        # 简单场景: 1 个数据分析师就够
        domain_id = domains[0]
        rule = DOMAIN_RULES.get(domain_id, {"name_zh": "通用数据分析"})
        datasets = domain_map[domain_id]
        
        agent = {
            "id": "data_analyst",
            "name": "DataAnalyst",
            "name_zh": rule.get("name_zh", "数据分析师"),
            "role": "sub-agent",
            "enabled": True,
            "description": f"负责{rule.get('name_zh', '数据')}相关的所有查询和分析",
            "model_type": "sub",
            "tools": _pick_tools(domain_id, datasets),
            "datasets": [d["name"] for d in datasets],
            "prompt": _build_agent_prompt(domain_id, rule, datasets),
            "capabilities": ["数据查询", "趋势分析", "统计汇总"],
        }
        sub_agents.append(agent)
        all_test_cases.extend(_generate_test_cases(domain_id, rule, datasets))
    else:
        # 多域: 每个域一个子 Agent
        for domain_id, datasets in domain_map.items():
            rule = DOMAIN_RULES.get(domain_id, {"name_zh": "通用分析"})
            name_zh = rule.get("name_zh", "数据分析")
            
            agent = {
                "id": f"analyst_{domain_id}",
                "name": f"Analyst_{domain_id.title()}",
                "name_zh": name_zh + "师",
                "role": "sub-agent",
                "enabled": True,
                "description": f"负责{name_zh}: " + ", ".join(d["name"] for d in datasets),
                "model_type": "sub",
                "tools": _pick_tools(domain_id, datasets),
                "datasets": [d["name"] for d in datasets],
                "prompt": _build_agent_prompt(domain_id, rule, datasets),
                "capabilities": [f"{name_zh}查询", "趋势分析", "统计汇总"],
            }
            sub_agents.append(agent)
            all_test_cases.extend(_generate_test_cases(domain_id, rule, datasets))
    
    # ── Supervisor ──
    sub_desc = []
    routing_rules = []
    for sa in sub_agents:
        ds_list = ", ".join(sa["datasets"])
        sub_desc.append(f"- **{sa['name_zh']}** ({sa['id']}): 负责 {ds_list}")
        routing_rules.append(f"涉及 {ds_list} → deep_{sa['id']}_analysis")
    
    nl = "\n"
    supervisor_prompt = (
        "你是智能数据分析平台的调度中枢。\n\n"
        "## 数据真实性（最高优先级）\n"
        "- 所有数字必须来自工具返回的真实数据，绝不编造\n"
        "- 没有调工具查到的数据，不能出现在回答中\n\n"
        f"## 子 Agent 团队\n{nl.join(sub_desc)}\n\n"
        f"## 路由规则\n{nl.join(routing_rules)}\n"
        "- 不确定归属 → 委派最相关的 Agent\n"
        "- 每个 deep_* 每次对话只调 1 次\n\n"
        "## 回答格式\n"
        "- 中文回答，技术术语保留英文\n"
        "- 200-400 字，先结论后证据\n"
    )
    
    supervisor = {
        "id": "supervisor",
        "name": "Supervisor",
        "name_zh": "调度中枢",
        "role": "orchestrator",
        "enabled": True,
        "description": f"协调 {len(sub_agents)} 个专业分析 Agent",
        "model_type": "primary",
        "tools": [f"deep_{sa['id']}_analysis" for sa in sub_agents] + ["list_datasets", "get_data_catalog"],
        "prompt": supervisor_prompt,
        "capabilities": ["意图识别", "任务路由", "结果汇总"],
    }
    
    # ── 汇总 ──
    domain_summary = []
    for d_id, ds_list in domain_map.items():
        rule = DOMAIN_RULES.get(d_id, {"name_zh": "通用"})
        domain_summary.append(f"  {rule.get('name_zh','通用')}: {len(ds_list)} 个数据集")
    
    summary = (
        f"基于 {total_datasets} 个数据源，识别出 {len(domains)} 个业务域，"
        f"推荐 1 个 Supervisor + {len(sub_agents)} 个子 Agent。\n"
        + "\n".join(domain_summary)
    )
    
    return {
        "supervisor": supervisor,
        "sub_agents": sub_agents,
        "test_cases": all_test_cases,
        "summary": summary,
        "domain_map": {k: [d["name"] for d in v] for k, v in domain_map.items()},
    }
