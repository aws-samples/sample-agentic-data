"""
Agentic Data — Strands Agents (optimized)

Architecture: Single Smart Agent with all tools (fast path)
+ Sub-agents available for complex multi-source analysis (deep path)
"""
import sys, time, threading, json
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from config import REGION, GUARDRAIL_ID, GUARDRAIL_VERSION, DEFAULT_SUPERVISOR_MODEL, DEFAULT_SUB_AGENT_MODEL, MODEL_PROVIDER, SILICONFLOW_API_KEY, SILICONFLOW_BASE_URL, BEDROCK_REGION
from strands import Agent
# BedrockModel 仅在 bedrock provider 时导入
if MODEL_PROVIDER == "bedrock":
    from strands.models.bedrock import BedrockModel
else:
    BedrockModel = None  # SiliconFlow/OpenAI-compatible 不需要
from strands.agent.conversation_manager import SlidingWindowConversationManager
from strands.agent.conversation_manager.summarizing_conversation_manager import SummarizingConversationManager
from strands.hooks import BeforeToolCallEvent, AfterToolCallEvent
from strands.tools import tool

from .tools import (
    save_report, list_reports,
    get_data_catalog, nl2sql_query,
    semantic_query, pg_query,
    manage_alert_rules, manage_kpi_rules,
)
from agentic_core.db_engine import _safe_identifier

_sub_agent_config_default = {"model_id": DEFAULT_SUB_AGENT_MODEL, "max_tokens": 2048}
_sub_agent_local = threading.local()

# Property-like accessor: thread-local config > global default
def _get_sub_agent_config():
    return getattr(_sub_agent_local, 'config', _sub_agent_config_default)

def _set_sub_agent_config(val):
    _sub_agent_local.config = val

# Keep module-level name for backward compat (reads via property wrapper)
class _SubAgentConfigProxy:
    """Proxy that reads from thread-local, writes to thread-local."""
    def get(self, key, default=None):
        return _get_sub_agent_config().get(key, default)
    def __getitem__(self, key):
        return _get_sub_agent_config()[key]
    def __setitem__(self, key, val):
        cfg = _get_sub_agent_config()
        cfg[key] = val
        _set_sub_agent_config(cfg)
    def __contains__(self, key):
        return key in _get_sub_agent_config()
    def __repr__(self):
        return repr(_get_sub_agent_config())

_sub_agent_config = _SubAgentConfigProxy()

# Global trace callback (set by frontend before agent run)
_trace_callback = None
_trace_lock = threading.Lock()

SUB_TOOL_LABELS = {
}

def set_trace_callback(cb):
    """Set a callback for sub-agent trace events: cb(agent_name, event_type, data)"""
    global _trace_callback
    _trace_callback = cb

def _emit(agent_name, event_type, data):
    cb = _trace_callback
    if cb:
        cb(agent_name, event_type, data)
    elif _sub_agent_callback:
        _sub_agent_callback(agent_name, event_type, data)


def _sonnet():
    return _resolve_model(DEFAULT_SUPERVISOR_MODEL, max_tokens=4096, guardrail_enabled=False)

def _haiku():
    return _resolve_model(DEFAULT_SUB_AGENT_MODEL, max_tokens=2048, guardrail_enabled=False)


# ═══════ Sub-agent runner with inline hooks ═══════

class _ToolCallLimitExceeded(Exception):
    """Raised when a sub-agent exceeds its tool call limit."""
    pass

def _run_sub_agent(agent_name, tools, system_prompt, question, max_tool_calls=5, max_tokens=None, temperature=None, window_size=3, callback_handler=None):
    """Create and run a sub-agent, emitting trace events to the thread-local callback.
    Enforces max_tool_calls at the code level — Agent is stopped if it exceeds."""
    tool_times = {}
    call_count = [0]

    sub_cfg = getattr(sys.modules[__name__], '_sub_agent_config', {})
    sub_model_id = sub_cfg.get("model_id", DEFAULT_SUB_AGENT_MODEL)
    sub_max_tokens = max_tokens or sub_cfg.get("max_tokens", 2048)
    sub_temp = temperature

    # 统一用 _resolve_model (支持 Bedrock/SiliconFlow/Custom)
    sub_model = _resolve_model(sub_model_id, sub_max_tokens, guardrail_enabled=False)

    agent_kwargs = dict(
        model=sub_model,
        tools=tools,
        system_prompt=system_prompt,
        conversation_manager=SlidingWindowConversationManager(window_size=window_size),
    )
    if callback_handler:
        agent_kwargs["callback_handler"] = callback_handler

    agent = Agent(**agent_kwargs)

    def on_before(event: BeforeToolCallEvent):
        call_count[0] += 1
        name = event.tool_use.get("name", "") if isinstance(event.tool_use, dict) else getattr(event.tool_use, "name", "")
        if call_count[0] > max_tool_calls:
            _emit(agent_name, "tool_after", f"⚠️ 已达工具调用上限 ({max_tool_calls}次)")
            raise _ToolCallLimitExceeded(f"{agent_name} exceeded {max_tool_calls} tool calls")
        tool_times[name] = time.time()
        label = SUB_TOOL_LABELS.get(name, f"{name}")
        _emit(agent_name, "tool_before", label)

    def on_after(event: AfterToolCallEvent):
        name = event.tool_use.get("name", "") if isinstance(event.tool_use, dict) else getattr(event.tool_use, "name", "")
        label = SUB_TOOL_LABELS.get(name, name)
        ms = int((time.time() - tool_times.get(name, time.time())) * 1000)
        _emit(agent_name, "tool_after", f"{label} ({ms}ms)")

    agent.add_hook(on_before, BeforeToolCallEvent)
    agent.add_hook(on_after, AfterToolCallEvent)

    _emit(agent_name, "start", f"{agent_name} 开始分析...")
    try:
        result = agent(question)
    except _ToolCallLimitExceeded:
        # Agent was stopped — extract whatever partial result is in conversation
        msgs = agent.messages if hasattr(agent, 'messages') else []
        partial = ""
        for m in reversed(msgs):
            if isinstance(m, dict) and m.get("role") == "assistant":
                for c in m.get("content", []):
                    if isinstance(c, dict) and c.get("text"):
                        partial = c["text"]
                        break
            if partial:
                break
        result = partial or f"已用 {max_tool_calls} 次工具调用完成分析，结果如下（部分数据可能不完整）。"
    _emit(agent_name, "end", f"{agent_name} 完成")
    
    # Emit sub-agent cost metrics for tracking
    try:
        if hasattr(result, 'metrics') and result.metrics and result.metrics.agent_invocations:
            inv = result.metrics.agent_invocations[-1]
            u = inv.usage
            _emit(agent_name, "cost", json.dumps({
                "model": sub_model_id,
                "input_tokens": u.get("inputTokens", 0),
                "output_tokens": u.get("outputTokens", 0),
            }))
    except Exception:
        pass
    
    return str(result)


# Sub-agent notification callback — set by API layer, no-op by default
_sub_agent_callback = None
def _notify_sub(agent_name, status, data=None):
    if _sub_agent_callback:
        _sub_agent_callback(agent_name, status, data)
    elif _trace_callback:
        # Fallback: use trace callback (Direct mode)
        _trace_callback(agent_name, status, data)

# Custom model registry (loaded from DynamoDB)
_custom_models_cache = None

import time as _time
_custom_models_cache_ts = [0]

def _load_custom_models():
    global _custom_models_cache
    # 30s TTL 缓存, 确保多 worker 同步
    if _custom_models_cache is not None and (_time.time() - _custom_models_cache_ts[0]) < 30:
        return _custom_models_cache
    try:
        import boto3
        ddb = boto3.resource("dynamodb", region_name=REGION)
        from config import CONFIG_TABLE
        table = ddb.Table(CONFIG_TABLE)
        resp = table.get_item(Key={"config_key": "custom_models"})
        raw = resp.get("Item", {}).get("value", "[]")
        _custom_models_cache = json.loads(raw) if isinstance(raw, str) else raw
    except Exception:
        _custom_models_cache = []
    _custom_models_cache_ts[0] = _time.time()
    return _custom_models_cache

def invalidate_custom_models_cache():
    global _custom_models_cache
    _custom_models_cache = None
    _custom_models_cache_ts[0] = 0

def _resolve_model(model_id, max_tokens=8192, guardrail_enabled=True):
    """Resolve model ID to Strands model object.
    - Bedrock model IDs → BedrockModel (with optional Guardrail)
    - 'custom:Name' → LiteLLMModel (OpenAI-compatible)
    """
    if model_id.startswith("custom:"):
        custom_name = model_id[7:]  # strip "custom:"
        models = _load_custom_models()
        match = next((m for m in models if m.get("name") == custom_name), None)
        if not match:
            print(f"[Model] WARNING: Custom model '{custom_name}' not found. Available: {[m['name'] for m in models]}. Falling back to Bedrock default.")
            # Fall through to Bedrock default below
        else:
            from strands.models import LiteLLMModel
            # Build LiteLLM model string: openai/model_id for OpenAI-compatible
            litellm_model = f"openai/{match['model_id']}"
            
            # LiteLLM api_base 应该保留 /v1 (如果有的话)
            # 只剥掉 /chat/completions，不剥 /v1
            endpoint = match.get("endpoint", "")
            for suffix in ["/v1/chat/completions", "/chat/completions"]:
                if endpoint.endswith(suffix):
                    endpoint = endpoint[:-len(suffix)]
                    break
            # 确保 endpoint 以 /v1 结尾 (LiteLLM openai provider 需要)
            if not endpoint.endswith("/v1"):
                endpoint = endpoint.rstrip("/") + "/v1"
            
            # 自托管模型通常 context window 有限, 限制 max_tokens
            effective_max = min(max_tokens, int(match.get("max_tokens", 4096)))
            return LiteLLMModel(
                model_id=litellm_model,
                params={
                    "api_key": match.get("api_key", ""),
                    "api_base": endpoint,
                    "max_tokens": effective_max,
                    "drop_params": True,  # 兼容不支持 reasoning/thinking 的模型
                },
            )
    elif MODEL_PROVIDER == "siliconflow" and SILICONFLOW_API_KEY:
        # SiliconFlow — OpenAI-compatible API
        from strands.models import LiteLLMModel
        sf_base = SILICONFLOW_BASE_URL.rstrip("/")
        if not sf_base.endswith("/v1"):
            sf_base = sf_base + "/v1"
        return LiteLLMModel(
            model_id=f"openai/{model_id}",
            params={
                "api_key": SILICONFLOW_API_KEY,
                "api_base": sf_base,
                "max_tokens": max_tokens,
                "drop_params": True,
            },
        )
    else:
        if BedrockModel is None:
            # Fallback: 非 Bedrock 环境用 LiteLLM + SiliconFlow
            from strands.models import LiteLLMModel
            return LiteLLMModel(
                model_id=f"openai/{model_id}",
                params={
                    "api_key": SILICONFLOW_API_KEY or "",
                    "api_base": SILICONFLOW_BASE_URL,
                    "max_tokens": max_tokens,
                    "drop_params": True,
                },
            )
        # Standard Bedrock model
        model_kwargs = dict(model_id=model_id, region_name=BEDROCK_REGION, max_tokens=max_tokens)
        if guardrail_enabled and GUARDRAIL_ID:
            model_kwargs["guardrail_id"] = GUARDRAIL_ID
            model_kwargs["guardrail_version"] = GUARDRAIL_VERSION
            model_kwargs["guardrail_trace"] = "enabled"
            model_kwargs["guardrail_stream_processing_mode"] = "async"
        return BedrockModel(**model_kwargs)


# Per-request call limiter for deep_* tools
# Uses a simple global counter — reset at the start of each API request.
# Thread-safe via lock. Works with thread pools (Strands SDK runs tools on worker threads).
import threading
_deep_call_lock = threading.Lock()
_deep_call_count = 0  # global counter, reset per request

def _check_deep_limit(max_calls=2):
    """Returns True if within limit, False if exceeded."""
    global _deep_call_count
    with _deep_call_lock:
        if _deep_call_count >= max_calls:
            return False
        _deep_call_count += 1
        return True

def _reset_deep_limit():
    """Reset at start of each API request."""
    global _deep_call_count
    with _deep_call_lock:
        _deep_call_count = 0


def _ensure_chart_drill(result: str, question: str) -> str:
    """Post-process: auto-generate chart/drill blocks if agent didn't include them."""
    import re, json as _json
    has_chart = '```chart' in result
    has_drill = '```drill' in result

    if has_chart and has_drill:
        return result

    # Try to extract numbers from markdown tables for chart
    if not has_chart:
        # Parse markdown table properly: split each row by |, identify value column
        table_lines = [l.strip() for l in result.split('\n') if l.strip().startswith('|') and '---' not in l]
        items = []
        if len(table_lines) >= 3:  # header + at least 2 data rows
            header_cells = [c.strip() for c in table_lines[0].split('|') if c.strip()]
            # Find the best value column: first numeric-header column (skip 排名/序号)
            val_col = -1
            name_col = -1
            skip_cols = set()  # columns to skip (rank/index)
            for ci, h in enumerate(header_cells):
                h_lower = h.lower().replace('*', '')
                if h_lower in ('排名', '序号', '#', 'rank', 'no', 'no.'):
                    skip_cols.add(ci)
            for ci, h in enumerate(header_cells):
                if ci in skip_cols:
                    continue
                h_lower = h.lower().replace('*', '')
                if name_col < 0 and any(k in h_lower for k in ['车型', '名称', '型号', '工厂', '产线', '表名', '场景', 'name', 'model', 'type']):
                    name_col = ci
                elif val_col < 0 and ci != name_col and any(k in h_lower for k in ['数量', '里程', '总数', '平均', '产量', '良品', '合格', '金额', '费用', '次数', '占比', '比例', 'count', 'avg', 'sum', 'total', 'km', 'cnt']):
                    val_col = ci
            # Fallback: name=first non-rank non-numeric col, value=first numeric col after name
            if name_col < 0:
                for ci, h in enumerate(header_cells):
                    if ci in skip_cols:
                        continue
                    name_col = ci
                    break
                if name_col < 0:
                    name_col = min(1, len(header_cells) - 1)

            for row_line in table_lines[1:11]:  # skip header, max 10 rows
                cells = [c.strip() for c in row_line.split('|') if c.strip()]
                if len(cells) <= max(name_col, 0):
                    continue
                name = re.sub(r'^[🥇🥈🥉\s]+', '', cells[name_col]).strip().replace('**', '')
                if not name or len(name) < 2:
                    continue
                # Find value: use val_col if set, otherwise scan cells for first parseable number
                v = None
                if val_col >= 0 and val_col < len(cells):
                    raw = cells[val_col].replace('**', '').replace(',', '').replace('%', '').strip()
                    try:
                        v = float(raw)
                    except ValueError:
                        pass
                if v is None:
                    for ci in range(len(cells)):
                        if ci == name_col:
                            continue
                        raw = cells[ci].replace('**', '').replace(',', '').replace('%', '').strip()
                        try:
                            v = float(raw)
                            if v > 0:
                                break
                        except ValueError:
                            continue
                if v is not None and v > 0:
                    # Round for display
                    display_v = int(v) if v == int(v) else round(v, 2)
                    items.append({"name": name[:20], "value": display_v})
            if len(items) >= 2:
                # Determine chart type
                chart_type = "bar"
                q_lower = question.lower()
                if any(k in q_lower for k in ["分布", "占比", "比例", "构成"]):
                    chart_type = "pie"
                elif any(k in q_lower for k in ["趋势", "变化", "走势", "月"]):
                    chart_type = "line"

                chart_data = {"type": chart_type, "items": items}
                result += f"\n\n```chart\n{_json.dumps(chart_data, ensure_ascii=False)}\n```"

    # Auto-generate drill suggestions only if agent didn't include them
    if not has_drill:
        # Generate contextual drill-down questions based on the question content
        q_short = question[:30]
        drills = []
        q_lower = question.lower()
        
        # Detect domain and generate specific drills
        if any(k in q_lower for k in ['车', '车型', 'vin', '里程', '能源', 'bev', 'ice']):
            drills = [
                {"title": "按车型分布", "desc": "各车型的数量占比", "query": "各车型数量TOP10"},
                {"title": "按能源类型", "desc": "燃油/纯电/插混分布", "query": "按能源类型的车辆数量分布"},
                {"title": "行驶里程分析", "desc": "不同车型的平均里程", "query": "各车型的平均行驶里程排名"},
            ]
        elif any(k in q_lower for k in ['产线', '良品', '产量', '质检', '设备', '工厂']):
            drills = [
                {"title": "产线良品率", "desc": "各产线的良品率排名", "query": "各产线良品率排名"},
                {"title": "设备状态", "desc": "当前设备运行情况", "query": "各设备当前状态汇总"},
                {"title": "产量趋势", "desc": "近期产量变化", "query": "最近7天的每日产量趋势"},
            ]
        else:
            drills = [
                {"title": "维度下钻", "desc": "按不同维度拆分分析", "query": f"针对「{q_short}」按不同维度细分"},
                {"title": "TOP 排名", "desc": "查看排名前列的数据", "query": f"关于「{q_short}」的 TOP10 排名"},
                {"title": "趋势分析", "desc": "查看时间维度的变化", "query": f"关于「{q_short}」的近期趋势变化"},
            ]
        
        result += f"\n\n```drill\n{_json.dumps(drills, ensure_ascii=False)}\n```"

    return result


def _parallel_cross_domain(question, base_prompt, all_tools, mcp_clients):
    """Execute Athena + PG queries in parallel for cross-domain scenarios."""
    from concurrent.futures import ThreadPoolExecutor, as_completed
    from agentic_core.tools import nl2sql_query, get_data_catalog, pg_query, snowflake_query

    # Dynamic agent names from V2 config
    _athena_name = "车联网数据分析"
    _pg_name = "生产制造分析"
    try:
        from agentic_core.agent_registry import get_scene, get_agent_def
        _scene = get_scene(_sub_agent_config.get("_scenario_id", "cross_domain"))
        if _scene:
            _agents = _scene.get("orchestration", {}).get("agents", [])
            for _ref in _agents:
                _aid = _ref["id"] if isinstance(_ref, dict) else _ref
                _adef = get_agent_def(_aid)
                if _adef:
                    _tools = _adef.get("tools", [])
                    if any("athena" in t or "nl2sql" in t or "semantic" in t for t in _tools):
                        _athena_name = _adef.get("name", _athena_name)
                    elif any("pg" in t for t in _tools):
                        _pg_name = _adef.get("name", _pg_name)
    except Exception:
        pass

    _emit("CrossDomain", "start", f"跨域并行分析: {_athena_name} + {_pg_name} 同时启动")

    # Split tools by data source
    athena_tools = [t for t in all_tools if getattr(t, '__name__', getattr(t, 'name', '')) in ('semantic_query', 'nl2sql_query', 'get_data_catalog')]
    pg_tools_list = [t for t in all_tools if getattr(t, '__name__', getattr(t, 'name', '')) in ('semantic_query', 'pg_query', 'get_data_catalog')]
    # Fallback: if filtering failed, use import
    if not athena_tools:
        athena_tools = [semantic_query, nl2sql_query, get_data_catalog]
    if not pg_tools_list:
        pg_tools_list = [semantic_query, pg_query, get_data_catalog]
    if not pg_tools_list:
        pg_tools_list = [pg_query, get_data_catalog]

    # NOTE: MCP clients are NOT added to parallel tools — MCP sessions can't run
    # inside ThreadPoolExecutor threads. PG Agent uses native pg_query + semantic_query.

    _semantic_first_athena = """

## 🔴 工具调用顺序（强制）
1. 必须先调 semantic_query — 把用户问题原封不动传入
2. 如果 semantic_query 返回了结果，直接使用，不要再调 nl2sql_query
3. 只有 semantic_query 明确返回"无匹配"时才用 nl2sql_query
4. 绝对不要跳过 semantic_query 直接写 SQL

返回查询结果的数据表格，不需要最终结论。"""

    _semantic_first_pg = """

## 🔴 工具调用顺序（强制）
1. 必须先调 semantic_query — 把用户问题传入看是否有匹配的预定义指标
2. 如果无匹配再用 pg_query
3. 不要跳过 semantic_query

只用 semantic_query 和 pg_query 工具。返回查询结果的数据表格，不需要最终结论。"""

    athena_prompt = base_prompt + f"\n\n## 你的角色: {_athena_name}\n你只负责查询 Athena 数据。\n\n## ⛔ 绝对禁止\n- 不要查询 PostgreSQL 的表\n- 不要使用 public.xxx 前缀（这是 PostgreSQL 格式，Athena 没有 public schema）\n- 你只能用 database.table 格式的表名\n\n## Athena SQL 语法（Trino/Presto，不是 MySQL！）\n- timestamp 类型字段不要用 DATE_PARSE()！\n- 日期差值: date_diff('day', start, end)（不是 DATEDIFF！）\n- 正确: date_diff('day', col, current_timestamp)\n- 错误: DATEDIFF('day', ...) / DATE_PARSE(col, ...) / DATEADD(...)" + _semantic_first_athena
    pg_prompt = base_prompt + f"\n\n## 你的角色: {_pg_name}\n你只负责查询 PostgreSQL 数据。\n\n## 表名规则（重要）\n- 直接写表名，不要加数据库名前缀\n- 如果表在非 public schema，使用 schema.table 格式（如 aftermarket.work_orders）" + _semantic_first_pg

    results = {}
    errors = {}

    def run_athena():
        try:
            def _ah(**kwargs):
                data = kwargs.get("data", "")
                if data:
                    _notify_sub(_athena_name, "text", {"text": data})
            r = _run_sub_agent(_athena_name, athena_tools, athena_prompt, question,
                             max_tool_calls=5, max_tokens=2048, temperature=0.1,
                             window_size=3, callback_handler=_ah)
            return r
        except Exception as e:
            return f"[{_athena_name} 错误] {str(e)[:200]}"

    def run_pg():
        try:
            def _ph(**kwargs):
                data = kwargs.get("data", "")
                if data:
                    _notify_sub(_pg_name, "text", {"text": data})
            r = _run_sub_agent(_pg_name, pg_tools_list, pg_prompt, question,
                             max_tool_calls=5, max_tokens=2048, temperature=0.1,
                             window_size=3, callback_handler=_ph)
            return r
        except Exception as e:
            return f"[{_pg_name} 错误] {str(e)[:200]}"

    t_start = time.time()
    with ThreadPoolExecutor(max_workers=2) as executor:
        future_athena = executor.submit(run_athena)
        future_pg = executor.submit(run_pg)

        for future in as_completed([future_athena, future_pg]):
            if future == future_athena:
                results["athena"] = future.result()
                elapsed = int((time.time() - t_start) * 1000)
                _emit(_athena_name, "end", f"{_athena_name} 完成 ({elapsed}ms)")
            else:
                results["pg"] = future.result()
                elapsed = int((time.time() - t_start) * 1000)
                _emit(_pg_name, "end", f"{_pg_name} 完成 ({elapsed}ms)")

    total_ms = int((time.time() - t_start) * 1000)

    # Merge results — let the caller (Supervisor) see both
    athena_result = results.get("athena", "Athena 查询无结果")
    pg_result = results.get("pg", "PG 查询无结果")

    _emit("CrossDomain", "end", f"并行查询完成 ({total_ms}ms)")

    merged = f"""## 跨域并行查询结果 (耗时 {total_ms}ms)

### 车联网数据 (Athena)
{athena_result}

### 生产制造数据 (PostgreSQL)
{pg_result}

---
请基于以上两个数据源的结果，综合分析回答用户问题。对比关联两个数据源时，以车型名称(model_name)作为关联键。"""

    # Apply chart/drill post-processing
    merged = _ensure_chart_drill(merged, question)
    return merged


@tool
def deep_data_analysis(question: str) -> str:
    """Delegate to DataAnalystAgent for data queries.
    Use for: natural language data queries, SQL generation, cross-source analytics,
    data exploration, statistical analysis, distribution queries.
    Examples: "各车型数量TOP10" "平均行驶里程" "各工厂良品率对比"
    
    Args:
        question: The data analysis question in natural language
    """
    # Extract original question if history context is prepended
    _orig_question = question
    if "当前问题:" in question:
        _orig_question = question.split("当前问题:")[-1].strip()
    if not _check_deep_limit(max_calls=5):
        return json.dumps({"error": "本次消息的数据分析次数已达上限(5次)，请发送新消息继续提问。", "status": "rate_limited"}, ensure_ascii=False)
    from agentic_core.tools import nl2sql_query, get_data_catalog, pg_query, snowflake_query

    # ── MCP-first: try loading MCP clients for the scenario ──
    mcp_clients = []
    use_mcp = _sub_agent_config.get("use_mcp", True)  # Default: try MCP
    if use_mcp:
        try:
            from mcp_servers.router import get_mcp_clients_for_scenario
            scenario_cfg = _sub_agent_config.get("_scenario_cfg")
            if scenario_cfg:
                mcp_clients = get_mcp_clients_for_scenario(scenario_cfg)
                if mcp_clients:
                    print(f"[MCP] Loaded {len(mcp_clients)} MCP server(s) for scenario")
        except Exception as e:
            print(f"[MCP] Failed to load MCP clients: {e}, falling back to native tools")
            mcp_clients = []

    # ── Scenario-driven tool filtering ──
    all_da_tools = {
        'semantic_query': semantic_query, 'nl2sql_query': nl2sql_query,
        'pg_query': pg_query, 'snowflake_query': snowflake_query,
        'get_data_catalog': get_data_catalog,
    }

    # ── Datasource-aware filtering: only offer tools whose backing datasource is connected ──
    try:
        from config import REGION, CONFIG_TABLE
        import boto3 as _b3
        _ds_resp = _b3.resource("dynamodb", region_name=REGION).Table(CONFIG_TABLE).get_item(Key={"config_key": "custom_datasources"})
        _connected_ds = json.loads(_ds_resp.get("Item", {}).get("data", _ds_resp.get("Item", {}).get("value", "[]")))
        _ds_types = {ds.get("type", "").lower() for ds in _connected_ds if ds.get("enabled", True)}
        # Normalize: RDS → check engine field for mysql/postgresql
        for ds in _connected_ds:
            if ds.get("type", "").upper() == "RDS" and ds.get("enabled", True):
                engine = ds.get("config", {}).get("engine", "mysql").lower()
                _ds_types.add(engine)  # adds 'mysql' or 'postgresql'
        print(f"[Tools] ds_types={_ds_types}, tools={list(all_da_tools.keys())}")
        # pg_query requires a postgresql or mysql datasource
        if "postgresql" not in _ds_types and "mysql" not in _ds_types and "rds" not in _ds_types:
            removed = all_da_tools.pop("pg_query", None)
        # snowflake_query requires a snowflake datasource
        if "snowflake" not in _ds_types:
            all_da_tools.pop("snowflake_query", None)
        # nl2sql_query requires an athena datasource (semantic_query is universal — works with any engine)
        if "athena" not in _ds_types:
            all_da_tools.pop("nl2sql_query", None)
            print("[Tools] nl2sql_query removed — no athena datasource connected")
    except Exception as e:
        import traceback; traceback.print_exc()
        print(f"[Tools] Datasource check failed: {e}, keeping all tools")

    # ── Dynamic tool docstrings: inject real schema from DDB datasources ──
    try:
        from agentic_core.tools import _build_nl2sql_docstring, _build_pg_docstring, _build_snowflake_docstring
        if 'nl2sql_query' in all_da_tools:
            nl2sql_fn = all_da_tools['nl2sql_query']
            if hasattr(nl2sql_fn, '_tool_spec'):
                nl2sql_fn._tool_spec['description'] = _build_nl2sql_docstring()
        if 'pg_query' in all_da_tools:
            pg_fn = all_da_tools['pg_query']
            if hasattr(pg_fn, '_tool_spec'):
                pg_fn._tool_spec['description'] = _build_pg_docstring()
        if 'snowflake_query' in all_da_tools:
            sf_fn = all_da_tools['snowflake_query']
            if hasattr(sf_fn, '_tool_spec'):
                sf_fn._tool_spec['description'] = _build_snowflake_docstring()
    except Exception as e:
        print(f"[Tools] Dynamic docstring injection failed: {e}")

    
    scenario_da_tools = _sub_agent_config.get("scenario_da_tools")
    if mcp_clients:
        # MCP tools are supplementary — native tools always take priority
        # because native tools have artifact emission, VQR integration, error tracking
        # Only add MCP tools that don't overlap with native tools
        native_names = set(all_da_tools.keys())
        for mc in mcp_clients:
            prefix = getattr(mc, '_prefix', '') or ''
            # Skip MCP PG tools — native pg_query is better (has artifact + VQR)
            if prefix == 'pg' and 'pg_query' in native_names:
                print(f"[MCP] Skipping MCP pg tools — native pg_query preferred")
                continue
            # Add non-overlapping MCP tools
            print(f"[MCP] Adding MCP tools (prefix={prefix})")
        
        da_tools = []
        # Add native tools from scenario config
        if scenario_da_tools:
            for t in scenario_da_tools:
                if t in all_da_tools:
                    da_tools.append(all_da_tools[t])
        else:
            for t, fn in all_da_tools.items():
                da_tools.append(fn)
        
        # Always keep semantic_query if not already added
        if semantic_query not in da_tools:
            da_tools.append(semantic_query)
        
        # Don't add MCP clients — native tools are preferred
        # MCP tools lack artifact emission, VQR integration, error tracking
        # Suppress MCP cleanup errors (event loop conflict in threaded context)
        import atexit
        for mc in mcp_clients:
            try:
                # Pre-mark as consumer so Agent can manage lifecycle
                mc._consumers = getattr(mc, '_consumers', set())
            except Exception:
                pass
    elif scenario_da_tools:
        # Fallback: native tools filtered by scenario
        da_tools = [all_da_tools[t] for t in scenario_da_tools if t in all_da_tools]
        print(f"[Tools] Scenario filter applied: {scenario_da_tools} → {[getattr(t,'__name__',str(t)) for t in da_tools]}")
        if get_data_catalog not in da_tools:
            da_tools.append(get_data_catalog)
    else:
        da_tools = list(all_da_tools.values())

    from agentic_core.dynamic_context import build_data_analyst_prompt
    # Pass scenario datasource IDs to filter prompt data context
    scenario_cfg = _sub_agent_config.get("_scenario_cfg")
    ds_ids = scenario_cfg.get("datasources", []) if scenario_cfg else None
    prompt = _sub_agent_config.get("data_analyst_prompt", build_data_analyst_prompt(datasource_ids=ds_ids))

    # Inject skills content into prompt
    skill_ids = _sub_agent_config.get("skills", [])

    if skill_ids:
        try:
            import boto3 as _b3
            from config import REGION, CONFIG_TABLE
            resp = _b3.resource("dynamodb", region_name=REGION).Table(CONFIG_TABLE).get_item(Key={"config_key": "skills"})
            if "Item" in resp:
                all_skills = json.loads(resp["Item"].get("data", resp["Item"].get("value", "{}")))
                skill_sections = []
                for sid in skill_ids:
                    if sid in all_skills:
                        s = all_skills[sid]
                        skill_sections.append(f"### {s.get('name', sid)}\n{s.get('content', '')}")
                if skill_sections:
                    prompt += "\n\n## 领域知识 (Skills)\n" + "\n\n".join(skill_sections)
        except Exception as e:
            print(f"[Skills] Failed to load: {e}")

    # Pre-fetch schema + semantic layer context to augment prompt
    try:
        schema_info = get_data_catalog()
        from agentic_core.semantic_layer import get_semantic_context
        semantic_ctx = get_semantic_context(question)
        augmented_prompt = prompt + f"\n\n## 数据字典\n{schema_info[:1500]}\n\n{semantic_ctx}"
        # 🔴 语义层优先 — 必须先调 semantic_query
        augmented_prompt += """

## 🔴 工具调用顺序（强制）
1. **必须先调 semantic_query** — 把用户问题原封不动传入，让语义层匹配预定义指标
2. 如果 semantic_query 返回了 SQL 和结果 → 直接使用，不要再调 nl2sql_query
3. 只有当 semantic_query 明确返回"无匹配"时，才用 nl2sql_query 或 pg_query
4. **绝对不要跳过 semantic_query 直接写 SQL** — 语义层的 SQL 是经过验证的，你自己写的 SQL 容易出字段名错误、表名错误、扫描量爆炸"""
        # 优化提示
        augmented_prompt += "\n\n## 效率优化\n数据字典已在上方提供，目标: 1-2 次工具调用完成回答。"
        # 🔴 强制格式提醒放最后 — 模型对末尾指令最敏感
        augmented_prompt += """

## 🔴 输出格式检查清单（回答前必须逐条确认）
1. 有 ```chart 代码块？（任何有数字的回答都必须带图表）
2. 有 ```drill 代码块？（必须带 2-3 个追问建议）
3. chart 的 value 是真实数值不是缩写？
4. drill 的 query 是完整可发送的问题？
缺少任何一项 = 不合格回答，必须补上！"""
    except Exception:
        augmented_prompt = prompt

    _da_display_name = _sub_agent_config.get("agent_display_name", "DataAnalyst")

    # Stream sub-agent text to parent
    def _text_handler(**kwargs):
        data = kwargs.get("data", "")
        if data:
            _notify_sub(_da_display_name, "text", {"text": data})

    # Inject scenario prompt context if available
    scenario_ctx = _sub_agent_config.get("scenario_prompt_context", "")
    if scenario_ctx:
        augmented_prompt = f"## 场景上下文\n{scenario_ctx}\n\n" + augmented_prompt

    # ── Cross-domain parallel execution ──
    scenario_cfg = _sub_agent_config.get("_scenario_cfg") or {}
    is_cross_domain = len(scenario_cfg.get("datasources", [])) >= 2 and \
                      any("athena" in ds for ds in scenario_cfg.get("datasources", [])) and \
                      any("pg" in ds for ds in scenario_cfg.get("datasources", [])) and \
                      "pg_query" in all_da_tools  # Only parallel if PG datasource is actually connected

    if is_cross_domain:
        return _parallel_cross_domain(question, augmented_prompt, da_tools, mcp_clients)

    try:
        _da_display_name = _sub_agent_config.get("agent_display_name", "DataAnalyst")
        result = _run_sub_agent(
            _da_display_name,
            da_tools,
            augmented_prompt,
            question,
            max_tool_calls=8,
            max_tokens=4096,
            temperature=0.1,
            window_size=4,
            callback_handler=_text_handler,
        )
        # 后处理: 如果没有 chart/drill 代码块，尝试从结果中自动生成
        result = _ensure_chart_drill(result, _orig_question)
        
        # Insight 层: 只用原始问题检测，避免对话历史中的关键词误触发
        result = _maybe_run_insight(_orig_question, result)
        
        return result
    except Exception as e:
        import traceback
        print(f"[{_da_display_name}] EXCEPTION: {e}")
        traceback.print_exc()
        _notify_sub(_da_display_name, "end", {"error": str(e)[:200]})
        return "数据查询暂时无法完成，请用户重新提问或换个问法。"




# ═══════ Insight Layer ═══════

INSIGHT_TRIGGERS = {
    "anomaly": ["为什么", "异常", "下降", "上升", "波动", "突变", "突然", "暴跌", "骤降", "急升"],
    "trend": ["趋势", "变化", "走势", "近期", "最近", "历史"],
    "forecast": ["预测", "预估", "下个月", "未来", "展望", "预期", "下季度"],
    "attribution": ["原因", "归因", "导致", "影响因素", "为什么", "怎么回事", "什么原因"],
}

def _detect_insight_needs(question: str) -> list:
    """Detect which insight analyses are needed based on the question."""
    q = question.lower()
    # Skip insight for simple queries (counts, distributions, TOPs, lists)
    skip_patterns = ["多少", "top", "有哪些", "列出", "数量", "占比", "分布", "分别", "排名", "总共", "总数", "清单"]
    if any(p in q for p in skip_patterns):
        return []
    needs = set()
    for insight_type, keywords in INSIGHT_TRIGGERS.items():
        for kw in keywords:
            if kw in q:
                needs.add(insight_type)
                break
    return list(needs)


def _extract_table_data(result: str) -> list:
    """Extract markdown table data from DataAnalyst result as list of dicts."""
    import re
    lines = result.split('\n')
    tables = []
    i = 0
    while i < len(lines):
        line = lines[i].strip()
        if '|' in line and line.startswith('|'):
            # Found table header
            headers = [h.strip() for h in line.split('|') if h.strip()]
            # Skip separator
            if i + 1 < len(lines) and '---' in lines[i + 1]:
                i += 2
            else:
                i += 1
            rows = []
            while i < len(lines) and lines[i].strip().startswith('|'):
                cells = [c.strip() for c in lines[i].strip().split('|') if c.strip()]
                if len(cells) == len(headers):
                    row = {}
                    for h, c in zip(headers, cells):
                        # Try to parse numbers
                        try:
                            row[h] = float(c.replace(',', '').replace('%', ''))
                        except ValueError:
                            row[h] = c
                    rows.append(row)
                i += 1
            if rows:
                tables.append({"headers": headers, "rows": rows})
        else:
            i += 1
    return tables


def _query_raw_data_for_insight(question: str, da_result: str) -> tuple:
    """Query raw time-series data from DB for insight analysis. Returns (data_list, metric, time_col, dims)."""
    import re
    from config import POSTGRES_HOST

    # Detect scenario from da_result or question
    has_pg = bool(POSTGRES_HOST)

    # Keywords to guess the metric
    metric_hints = {
        "良品率": ("ROUND(100.0 - (defect_qty::numeric / NULLIF(actual_qty, 0) * 100), 2)", "quality_rate"),
        "产量": ("actual_qty", "actual_qty"),
        "缺陷": ("defect_qty", "defect_qty"),
        "里程": ("effective_mileage", "effective_mileage"),
    }

    metric_sql = "ROUND(100.0 - (defect_qty::numeric / NULLIF(actual_qty, 0) * 100), 2)"
    metric_alias = "quality_rate"
    for hint, (sql, alias) in metric_hints.items():
        if hint in question:
            metric_sql, metric_alias = sql, alias
            break

    if has_pg:
        try:
            import psycopg2
            from config import POSTGRES_HOST, POSTGRES_PORT, POSTGRES_DATABASE, POSTGRES_USER, POSTGRES_PASSWORD
            conn = psycopg2.connect(
                host=POSTGRES_HOST, port=POSTGRES_PORT or 5432,
                dbname=POSTGRES_DATABASE or "postgres",
                user=POSTGRES_USER, password=POSTGRES_PASSWORD,
            )
            cur = conn.cursor()
            _alias = _safe_identifier(metric_alias)
            cur.execute(f"SELECT production_date::text as production_date, line_id, {metric_sql} as {_alias} FROM daily_production WHERE actual_qty > 0 ORDER BY production_date LIMIT 500")  # nosec B608
            rows = cur.fetchall()
            cols = [d[0] for d in cur.description]
            data = [dict(zip(cols, r)) for r in rows]
            conn.close()
            # Convert Decimal to float
            for row in data:
                for k, v in row.items():
                    if hasattr(v, 'as_integer_ratio'):
                        row[k] = float(v)
                    elif isinstance(v, str):
                        try: row[k] = float(v)
                        except: pass
            dims = ["line_id"]
            return data, metric_alias, "production_date", dims
        except Exception as e:
            print(f"[InsightAgent] PG query failed: {e}")

    return [], "", "", []


def _maybe_run_insight(question: str, da_result: str) -> str:
    """Pure deterministic insight — zero LLM cost, millisecond latency.
    Only runs if the current agent/scene has the 'insight' skill enabled."""
    # Check if insight skill is enabled for this agent
    skills = _sub_agent_config.get("skills", [])
    if skills and "insight" not in [s.lower().replace(" ", "_").replace("分析", "").strip() for s in skills]:
        # If skills are explicitly configured but insight is not among them, skip
        insight_names = {"insight", "insight分析", "insight 分析", "insight_analysis"}
        if not any(s.lower().strip() in insight_names for s in skills):
            return da_result

    needs = _detect_insight_needs(question)
    if not needs:
        return da_result

    _emit("InsightAgent", "start", "深度洞察分析")

    try:
        from agentic_core.insight import detect_anomaly, analyze_trend, attribution, forecast

        # Step 1: Get raw data directly from DB (no LLM)
        data, metric, time_col, dims = _query_raw_data_for_insight(question, da_result)
        if not data or not metric:
            print(f"[InsightAgent] No raw data available, skipping insight")
            _emit("InsightAgent", "end", "无原始数据可分析")
            return da_result

        data_json = json.dumps(data, ensure_ascii=False, default=str)
        print(f"[InsightAgent] Got {len(data)} rows, metric={metric}, time_col={time_col}, dims={dims}")

        insight_parts = []

        # Step 2: Run deterministic analyses based on detected needs
        if "anomaly" in needs or "attribution" in needs:
            _emit("InsightAgent", "tool_before", "异常检测")
            r = json.loads(detect_anomaly(data_json, metric, time_col))
            _emit("InsightAgent", "tool_after", f"发现 {r['anomaly_count']} 个异常")
            if r["anomalies"]:
                lines = [f"**异常检测** — 发现 {r['anomaly_count']} 个异常 (共 {r['total_points']} 个数据点)"]
                for a in r["anomalies"][:5]:
                    lines.append(f"- {a['label']}: {a['value']} ({a['severity']}级{a['direction']}, {a['methods']})")
                lines.append(f"- 正常范围: {r['stats']['normal_range'][0]} ~ {r['stats']['normal_range'][1]}")
                insight_parts.append("\n".join(lines))

        if "trend" in needs:
            if len(data) >= 5:
                _emit("InsightAgent", "tool_before", "趋势分析")
                r = json.loads(analyze_trend(data_json, metric, time_col))
                _emit("InsightAgent", "tool_after", f"趋势: {r['direction']} {r['direction_symbol']}")
                lines = [f"**趋势分析** {r['direction_symbol']}"]
                lines.append(f"- 整体趋势: {r['direction']}，变化率 {r['total_change_pct']}%")
                lines.append(f"- 近期趋势: {r['recent_direction']}")
                if r.get("significant"):
                    lines.append(f"- 统计显著 (R²={r['r_squared']}, p={r['p_value']})")
                if r.get("turning_points"):
                    for tp in r["turning_points"][:3]:
                        lines.append(f"- 拐点: {tp['label']} = {tp['value']} ({tp['type']})")
                insight_parts.append("\n".join(lines))

        if "attribution" in needs and dims:
            _emit("InsightAgent", "tool_before", "归因分析")
            r = json.loads(attribution(data_json, metric, ",".join(dims)))
            _emit("InsightAgent", "tool_after", f"{len(r.get('contributions', []))} 个维度")
            if r.get("contributions"):
                lines = ["**归因分析**"]
                for c in r["contributions"][:3]:
                    lines.append(f"- {c['dimension']}: 贡献 {c.get('normalized_pct', c['explained_pct'])}%")
                    for g in c["top_groups"][:3]:
                        lines.append(f"  - {g['value']}: 均值={g['mean']}, 标准差={g['std']}")
                insight_parts.append("\n".join(lines))

        if "forecast" in needs:
            if len(data) >= 5:
                _emit("InsightAgent", "tool_before", "趋势预测")
                r = json.loads(forecast(data_json, metric, 7, time_col))
                _emit("InsightAgent", "tool_after", f"预测: {r['forecast_direction']}")
                lines = [f"**预测 (未来 {r['forecast_periods']} 期)**"]
                lines.append(f"- 当前值: {r['current_value']}")
                lines.append(f"- 预测方向: {r['forecast_direction']} ({'+' if r['change_pct']>0 else ''}{r['change_pct']}%)")
                for p in r["predictions"][:5]:
                    lines.append(f"- 第{p['period']}期: {p['predicted']} [{p['lower']} ~ {p['upper']}]")
                lines.append(f"- 模型: {r['method']}, R²={r['model_quality']['r_squared']}")
                insight_parts.append("\n".join(lines))

        _emit("InsightAgent", "end", "洞察分析完成")

        if insight_parts:
            insight_block = "\n\n```insight\n" + "\n\n".join(insight_parts) + "\n```"
            print(f"[InsightAgent] insight_block len={len(insight_block)}, parts={len(insight_parts)}")
            # Emit insight block directly to SSE stream (bypasses Supervisor summarization)
            _emit("InsightAgent", "insight_result", insight_block)
        else:
            print(f"[InsightAgent] No insight_parts generated")

    except Exception as e:
        print(f"[InsightAgent] Error: {e}")
        import traceback; traceback.print_exc()
        _emit("InsightAgent", "end", "洞察分析跳过")

    return da_result


# ═══════ Supervisor — lazy creation to avoid global hook registry pollution ═══════

_supervisor_instance = None

# Guardrail config
# GUARDRAIL_ID imported from config
# GUARDRAIL_VERSION imported from config

# Cost tracking (per 1K tokens, USD)
MODEL_PRICING = {
    "us.anthropic.claude-sonnet-4-6": {"input": 0.003, "output": 0.015},
    "us.anthropic.claude-haiku-4-5-20251001-v1:0": {"input": 0.0008, "output": 0.004},
    # China region models
    "anthropic.claude-3-5-sonnet-20241022-v2:0": {"input": 0.003, "output": 0.015},
    "anthropic.claude-3-sonnet-20240229-v1:0": {"input": 0.003, "output": 0.015},
    "anthropic.claude-3-haiku-20240307-v1:0": {"input": 0.00025, "output": 0.00125},
    "global.anthropic.claude-opus-4-6-v1": {"input": 0.015, "output": 0.075},
    "us.anthropic.claude-sonnet-4-5-v1": {"input": 0.003, "output": 0.015},
    # SiliconFlow models (CNY per 1K tokens)
    "deepseek-ai/DeepSeek-V3": {"input": 0.002, "output": 0.008, "currency": "CNY"},
    "Pro/deepseek-ai/DeepSeek-R1": {"input": 0.004, "output": 0.016, "currency": "CNY"},
    "Qwen/Qwen3-32B": {"input": 0.00126, "output": 0.00126, "currency": "CNY"},
    "Qwen/Qwen2.5-72B-Instruct": {"input": 0.00413, "output": 0.00413, "currency": "CNY"},
    "Qwen/Qwen2.5-14B-Instruct": {"input": 0.0007, "output": 0.0007, "currency": "CNY"},
    "Qwen/Qwen2.5-7B-Instruct": {"input": 0, "output": 0, "currency": "CNY"},
}

# Available Bedrock models
# ── Model Registry (region-aware) ──
# ── Model Registry: all models from DDB, no hardcoding ──

def _load_all_models():
    """Load all models (Bedrock + custom) from DDB custom_models key.
    Returns dict {display_name: model_id} for Bedrock models.
    """
    models = _load_custom_models()  # already reads from DDB
    result = {}
    for m in models:
        name = m.get("name", "")
        if m.get("protocol") == "bedrock" or m.get("provider") == "bedrock":
            result[name] = m.get("model_id", "")
        # custom:Name models are handled separately in _resolve_model
    return result

def _load_sub_models():
    """Sub-agent models: prefer smaller/cheaper models if available."""
    all_m = _load_all_models()
    if not all_m:
        return all_m
    # Heuristic: haiku/lite/micro/small first, then all
    sub = {}
    for name, mid in all_m.items():
        low = name.lower()
        if any(k in low for k in ("haiku", "lite", "micro", "small", "mini", "7b", "14b")):
            sub[name] = mid
    # If no small models found, return all
    return sub if sub else all_m

AVAILABLE_MODELS = _load_all_models()
SUB_AGENT_MODELS = _load_sub_models()

def reload_models():
    """Reload models from DDB (call after model config changes)."""
    global AVAILABLE_MODELS, SUB_AGENT_MODELS
    AVAILABLE_MODELS = _load_all_models()
    SUB_AGENT_MODELS = _load_sub_models()

DEFAULT_DATA_ANALYST_PROMPT = """你是数据分析师。用最少的工具调用回答数据问题。

## ⛔ 数据真实性（最高优先级！）
- **所有数字必须来自工具返回结果，绝不允许编造**
- **工具返回空或失败 → 回答"暂无相关数据"，不能编**

## ⛔ SQL 表名规则
- **Athena**: 表名格式 `database.table`
  - Athena 用 Trino/Presto SQL 语法，不是 MySQL/SQL Server！
  - timestamp 类型字段不要用 DATE_PARSE()！
  - 日期差值: date_diff('day', start, end)（不是 DATEDIFF！）
  - 错误: DATEDIFF('day', ...) / DATE_PARSE(col, ...) / DATEADD(...)
- **PostgreSQL**: 直接写表名，不要加数据库名前缀
  - 如果表在非 public schema，使用 schema.table 格式
  - 注意字段值的大小写！查看工具描述中的字段值说明

## 工具选择
1. semantic_query → 语义层匹配指标和表关联
2. 根据返回结果选择合适工具查询

## 多指标对比
- 用户要求对比多个指标（如"对比产量和良品率"）→ **允许多次调用工具分别查询再合并**
- 每次调用只查一个指标维度，最后综合分析
- 对比分析时必须有明确的数值和结论

## 错误自愈
- 如果工具返回错误或 SQL 执行失败 → **分析错误信息，修正参数后重试一次**
- 常见可修复错误：列名拼写错误、表名不对、语法错误
- 重试一次后仍失败 → 如实告知用户"查询失败"并说明原因

## 跨表查询
- 语义层会返回**表关联映射（joins）**，跨表查询时必须使用提供的 JOIN 条件
- 不要自己猜测 JOIN 关系，只用语义层给出的映射

## 规则
- ⚠️ **工具调用 ≤ 5次**（对比分析允许更多）
- 查完立即写答案

## 深入分析建议
- 在回答末尾，基于**本次查询结果**生成 2-3 个深入分析建议
- 格式必须是：
```drill
[{"title":"简短标题","desc":"一句话描述","query":"可以直接发送的具体问题"}]
```
- 建议必须是**与本次结果相关的进一步分析**，不要给泛泛的建议
- 例如：查了"各车型数量"→ 建议"TOP3车型的平均里程对比"、"A4L和A6L的年份分布"
- 例如：查了"产线良品率"→ 建议"良品率最低的长春1线近7天趋势"、"缺陷类型分布"
"""


def _build_system_prompt():
    """动态生成 Supervisor system prompt — 基于当前数据状态"""
    from agentic_core.tools import CHATBI_DATASETS
    from agentic_core.semantic_layer import METRICS
    from agentic_core.dynamic_context import build_supervisor_data_section
    
    has_data = bool(CHATBI_DATASETS) or bool(METRICS)
    dynamic_section = "{{DYNAMIC_DATA_SECTION}}"  # Placeholder — replaced by create_supervisor() with scenario-filtered data
    
    # 基础规则（始终生效）
    base = """你是智能数据分析平台的高级分析师。

## ⛔ 数据真实性（最高优先级，违反即失败！）
1. **所有数字、排名、统计必须来自工具返回的真实数据，绝不允许编造、估算、杜撰**
2. **没有调工具查到的数据，就不能出现在回答中**
3. **工具调用失败或无数据时，必须明确告知用户 — 说"暂无该数据"**
4. **宁可回答"数据不足，无法分析"，也绝不能给出没有数据支撑的结论**
5. **绝对禁止根据工具名称或参数来推断数据内容** — 工具存在不代表有数据"""

    if not has_data:
        return base + """

## ⚠️ 当前状态：平台暂无已连接的数据源

**当用户询问"有什么数据""能分析什么"等问题时，你的唯一回答是：**
> 当前平台暂未连接任何数据源。请在管理后台的"数据源"页面添加数据源（支持 S3、DynamoDB、Athena、RDS、Snowflake 等），连接后系统会自动生成语义层并启用数据分析能力。

**绝对不要：**
- 不要列出任何数据集名称（vehicle_master、driving_daily 等统统不许提）
- 不要列出任何工具名称（统统不许提）
- 不要说"基于工具能力推断"
- 不要生成任何数据表格
- 不要给出"分析场景建议"
- 不要创建"业务部门分析"分类

**简短回答，不超过 3 句话。**
"""
    
    return base + f"""

## 工具使用策略
- **数据查询/统计/分析** → deep_data_analysis（最多1次，DataAnalystAgent 执行）
- ⚠️ deep_data_analysis 每次对话只调1次
- ⚠️ 你没有直接的 SQL 查询工具

{dynamic_section}

## 效率规则
- 总 tool 调用 ≤ 12 次
- 数据查询类问题 → 委派 deep_data_analysis
- 用户提到"报表"/"仪表盘"/"Tableau" → 委派 deep_data_analysis

## 回答格式
- 中文回答，技术术语保留英文
- 200-400字，高信息密度
- 用 Markdown 表格呈现关键数据
- 不要在标题前加 emoji
- 先结论后证据
"""

DEFAULT_SYSTEM_PROMPT = _build_system_prompt()


def create_supervisor(config=None):
    """Create a fresh supervisor agent with optional config override."""
    from agentic_core.dynamic_context import (
        update_tool_descriptions, build_supervisor_data_section,
        build_data_analyst_prompt
    )
    
    # P0: 动态更新工具描述
    update_tool_descriptions()
    
    config = config or {}
    sup_model_id = config.get("supervisor_model", DEFAULT_SUPERVISOR_MODEL)
    sup_max_tokens = config.get("supervisor_max_tokens", 8192)
    sup_prompt = config.get("system_prompt", _build_system_prompt())
    sup_window = config.get("window_size", 10)
    sub_model_id = config.get("sub_agent_model", DEFAULT_SUB_AGENT_MODEL)
    sub_max_tokens = config.get("sub_agent_max_tokens", 2048)
    
    # P0: 动态注入数据源信息到 Supervisor prompt
    # P0: 动态注入数据源信息到 Supervisor prompt — filtered by scenario
    scenario_cfg = config.get("_scenario_cfg")
    ds_ids = scenario_cfg.get("datasources", []) if scenario_cfg else None
    dynamic_data = build_supervisor_data_section(datasource_ids=ds_ids)
    sup_prompt = sup_prompt.replace("{{DYNAMIC_DATA_SECTION}}", dynamic_data)

    # Update sub-agent config (model + prompts) — thread-local
    _new_cfg = {
        "model_id": sub_model_id, "max_tokens": sub_max_tokens,
    }
    if config.get("_scenario_id"):
        _new_cfg["_scenario_id"] = config["_scenario_id"]
    # Pass scenario config to sub-agents
    if config.get("scenario_da_tools"):
        _new_cfg["scenario_da_tools"] = config["scenario_da_tools"]
    if config.get("scenario_prompt_context"):
        _new_cfg["scenario_prompt_context"] = config["scenario_prompt_context"]
    if config.get("_scenario_cfg"):
        _new_cfg["_scenario_cfg"] = config["_scenario_cfg"]
    if config.get("skills"):
        _new_cfg["skills"] = config["skills"]
    
    # Dynamic agent display name from V2 scene → agent definition
    _scene_id = config.get("_scenario_id")
    if _scene_id:
        try:
            from agentic_core.agent_registry import get_scene, get_agent_def
            _v2_scene = get_scene(_scene_id)
            if _v2_scene:
                _orch_agents = _v2_scene.get("orchestration", {}).get("agents", [])
                if _orch_agents:
                    _primary = _orch_agents[0]
                    _aid = _primary["id"] if isinstance(_primary, dict) else _primary
                    _v2_def = get_agent_def(_aid)
                    if _v2_def:
                        _new_cfg["agent_display_name"] = _v2_def.get("name", "DataAnalyst")
        except Exception:
            pass

    # Commit to thread-local
    _set_sub_agent_config(_new_cfg)

    guardrail_enabled = config.get("guardrail_enabled", True)
    
    # 支持第三方模型: model ID 以 "custom:" 开头时走 LiteLLM
    model = _resolve_model(sup_model_id, sup_max_tokens, guardrail_enabled)

    from agentic_core.tools import manage_alert_rules, manage_kpi_rules

    # 动态加载 tools: 从 agent_definitions 配置读取, fallback 到默认集合
    # 完整工具注册表 — 所有可用工具
    from agentic_core.tools import (
        get_data_catalog, semantic_query, nl2sql_query, pg_query,
    )
    all_tools_map = {

        # 报告
        'save_report': save_report, 'list_reports': list_reports,
        # Sub-agent 委派
        'deep_data_analysis': deep_data_analysis, 'deep_data_analyst_analysis': deep_data_analysis,
        # 数据查询
        'get_data_catalog': get_data_catalog,
        'semantic_query': semantic_query, 'pg_query': pg_query,
        # 告警
        'manage_alert_rules': manage_alert_rules, 'manage_kpi_rules': manage_kpi_rules,
    }
    
    # 从 config 中读取 supervisor 配置的 tools 列表
    sup_tool_names = config.get("supervisor_tools", [])
    if sup_tool_names:
        tools = [all_tools_map[t] for t in sup_tool_names if t in all_tools_map]
    else:
        # 默认: 数据分析 + 告警管理 (不含已移除的 safety/behavior)
        tools = [
            deep_data_analysis, manage_alert_rules, manage_kpi_rules,
            save_report, list_reports,
        ]
    
    return Agent(
        model=model,
        tools=tools,
        system_prompt=sup_prompt,
        conversation_manager=SummarizingConversationManager(
            summary_ratio=0.3,
            preserve_recent_messages=max(sup_window, 6),
        ),
    )
