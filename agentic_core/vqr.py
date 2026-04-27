"""
VQR (Verified Query Repository) — 反馈驱动的查询知识库。
用户 👍 → 候选队列 → LLM-as-Judge 评分 → 管理员审核 → VQR → 下次直接命中 (0 LLM)。
"""

import json
import time
import uuid
import logging
import re
import boto3
from typing import Optional

logger = logging.getLogger(__name__)

# ═══════ DDB 存储 ═══════

_ddb = None
_table = None

def _get_table():
    global _ddb, _table
    if _table is None:
        import os
        region = os.environ.get("AGENTIC_AUTO_REGION", os.environ.get("AWS_DEFAULT_REGION", "us-east-1"))
        table_name = os.environ.get("AGENTIC_AUTO_CONFIG_TABLE", "agentic-auto-config")
        _ddb = boto3.resource("dynamodb", region_name=region)
        _table = _ddb.Table(table_name)
    return _table


def _load_vqr() -> dict:
    """Load verified queries from DDB."""
    try:
        table = _get_table()
        item = table.get_item(Key={"config_key": "verified_queries"}).get("Item", {})
        raw = item.get("data") or item.get("value") or "{}"
        return json.loads(raw) if isinstance(raw, str) else raw
    except Exception as e:
        logger.error(f"[VQR] Failed to load verified_queries: {e}")
        return {}


def _save_vqr(data: dict):
    """Save verified queries to DDB."""
    try:
        table = _get_table()
        table.put_item(Item={
            "config_key": "verified_queries",
            "data": json.dumps(data, ensure_ascii=False, default=str),
            "value": json.dumps(data, ensure_ascii=False, default=str),
        })
    except Exception as e:
        logger.error(f"[VQR] Failed to save verified_queries: {e}")


def _load_candidates() -> dict:
    """Load VQR candidates from DDB."""
    try:
        table = _get_table()
        item = table.get_item(Key={"config_key": "vqr_candidates"}).get("Item", {})
        raw = item.get("data") or item.get("value") or "{}"
        return json.loads(raw) if isinstance(raw, str) else raw
    except Exception as e:
        logger.error(f"[VQR] Failed to load vqr_candidates: {e}")
        return {}


def _save_candidates(data: dict):
    """Save VQR candidates to DDB."""
    try:
        table = _get_table()
        table.put_item(Item={
            "config_key": "vqr_candidates",
            "data": json.dumps(data, ensure_ascii=False, default=str),
            "value": json.dumps(data, ensure_ascii=False, default=str),
        })
    except Exception as e:
        logger.error(f"[VQR] Failed to save vqr_candidates: {e}")


# ═══════ 匹配算法 ═══════

def _tokenize(text: str) -> set:
    """Simple Chinese + English tokenization by characters and words."""
    import re
    # Extract Chinese characters as individual tokens
    cn_chars = set(re.findall(r'[\u4e00-\u9fff]+', text))
    # Extract English words
    en_words = set(re.findall(r'[a-zA-Z_]+', text.lower()))
    # Also add 2-char Chinese bigrams for better matching
    bigrams = set()
    for seg in cn_chars:
        for i in range(len(seg) - 1):
            bigrams.add(seg[i:i+2])
    return cn_chars | en_words | bigrams


def _jaccard(s1: set, s2: set) -> float:
    """Jaccard similarity between two token sets."""
    if not s1 or not s2:
        return 0.0
    intersection = len(s1 & s2)
    union = len(s1 | s2)
    return intersection / union if union > 0 else 0.0


def match_verified_query(question: str, datasource_filter: list = None) -> Optional[dict]:
    """Match question against VQR. Returns best match or None.
    
    Three-layer matching:
    1. Exact question match or variant match (0ms)
    2. Keyword match (0ms)
    3. Jaccard similarity > 0.7 (5ms)
    """
    vqr = _load_vqr()
    if not vqr:
        return None

    question_lower = question.strip().lower()
    question_tokens = _tokenize(question)
    best_match = None
    best_score = 0.0

    for vq_id, vq in vqr.items():
        # Datasource filter
        if datasource_filter and vq.get("datasource"):
            if vq["datasource"] not in datasource_filter:
                continue

        # Layer 1: Exact match
        if question_lower == vq.get("question", "").lower():
            return {**vq, "id": vq_id, "match_type": "exact", "match_score": 1.0}

        # Layer 1b: Variant match
        variants = [v.lower() for v in vq.get("variants", [])]
        if question_lower in variants:
            return {**vq, "id": vq_id, "match_type": "variant", "match_score": 1.0}

        # Layer 2: Keyword match (all keywords must be present)
        keywords = vq.get("keywords", [])
        if keywords and all(kw.lower() in question_lower for kw in keywords):
            score = 0.9
            if score > best_score:
                best_score = score
                best_match = {**vq, "id": vq_id, "match_type": "keyword", "match_score": score}
            continue

        # Layer 3: Jaccard similarity
        vq_tokens = _tokenize(vq.get("question", ""))
        # Also include variant tokens
        for variant in vq.get("variants", []):
            vq_tokens |= _tokenize(variant)

        sim = _jaccard(question_tokens, vq_tokens)
        if sim > 0.7 and sim > best_score:
            best_score = sim
            best_match = {**vq, "id": vq_id, "match_type": "similarity", "match_score": round(sim, 3)}

    return best_match


# ═══════ 候选管理 ═══════

def add_candidate(question: str, sql: str, engine: str, datasource: str = "",
                  session_id: str = "", trace_id: str = "", rating: str = "up",
                  run_judge: bool = True) -> str:
    """Add a feedback-driven candidate to the review queue."""
    candidates = _load_candidates()

    # Deduplicate: skip if same question already pending
    for cand in candidates.values():
        if cand.get("question", "").lower() == question.lower() and cand.get("status") == "pending":
            logger.info(f"[VQR] Duplicate candidate skipped: {question[:50]}")
            return ""

    cand_id = f"cand_{uuid.uuid4().hex[:8]}"
    rule_score = evaluate_candidate(question, sql)

    # Negative feedback → directly mark as rejected, skip judge
    _status = "rejected" if rating == "down" else "pending"
    
    candidates[cand_id] = {
        "question": question,
        "sql": sql,
        "engine": engine,
        "datasource": datasource,
        "session_id": session_id,
        "trace_id": trace_id,
        "rating": rating,
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "status": _status,
        "auto_score": rule_score,
        "judge": None,  # Will be filled by LLM judge
    }

    _save_candidates(candidates)
    logger.info(f"[VQR] Candidate added: {cand_id} (rule_score={rule_score})")

    # Run LLM judge in background thread
    if run_judge and rule_score >= 0.2:
        import threading
        def _judge_bg():
            try:
                judge_result = llm_judge(question, sql, engine, run_sql=True)
                # Reload and update (another thread may have changed)
                cands = _load_candidates()
                if cand_id in cands:
                    cands[cand_id]["judge"] = judge_result
                    cands[cand_id]["auto_score"] = judge_result.get("overall", rule_score)
                    _save_candidates(cands)
                    logger.info(f"[VQR] Judge complete for {cand_id}: overall={judge_result.get('overall')}, reason={judge_result.get('reason','')[:80]}")

                    # Auto-approve disabled — always require human review
                    # Score is recorded but candidate stays pending
                    logger.info(f"[VQR] Judge scored {cand_id}: overall={judge_result.get('overall')}, awaiting human review")
            except Exception as e:
                logger.error(f"[VQR] Judge background error: {e}")

        t = threading.Thread(target=_judge_bg, daemon=True)
        t.start()

    return cand_id


def evaluate_candidate(question: str, sql: str) -> float:
    """Fast rule-based pre-screening. Returns 0-1 score."""
    from agentic_core.semantic_layer import METRICS

    score = 0.5
    sql_upper = sql.upper().strip()

    # ── Positive signals ──
    # Known tables from semantic layer
    known_tables = set()
    for m in METRICS.values():
        if isinstance(m, dict) and m.get("table"):
            known_tables.add(m["table"])
    if any(t.split(".")[-1].lower() in sql.lower() for t in known_tables if t):
        score += 0.1

    # Uses metric SQL fragments
    for m in METRICS.values():
        if isinstance(m, dict) and m.get("sql") and m["sql"].upper() in sql_upper:
            score += 0.1
            break

    # Aggregation functions (analytical intent)
    agg_funcs = ["COUNT(", "SUM(", "AVG(", "MAX(", "MIN(", "ROUND("]
    agg_count = sum(1 for f in agg_funcs if f in sql_upper)
    if agg_count >= 1:
        score += 0.05
    if agg_count >= 2:
        score += 0.05

    # Structural quality
    if "GROUP BY" in sql_upper:
        score += 0.05
    if "ORDER BY" in sql_upper:
        score += 0.03
    if "LIMIT" in sql_upper:
        score += 0.02
    if "WHERE" in sql_upper:
        score += 0.03
    if "JOIN" in sql_upper:
        score += 0.03

    # ── Negative signals ──
    # SELECT * (lazy, not reusable)
    if re.match(r'SELECT\s+\*\s+FROM', sql_upper):
        score -= 0.2

    # No aggregation, no WHERE, no GROUP BY = probably just a dump
    if all(kw not in sql_upper for kw in ["GROUP BY", "WHERE", "HAVING", "COUNT(", "SUM(", "AVG("]):
        score -= 0.15

    # Dangerous operations
    if any(kw in sql_upper for kw in ["DROP ", "DELETE ", "TRUNCATE ", "UPDATE ", "INSERT ", "ALTER "]):
        return 0.0

    # Nested subqueries
    select_count = sql_upper.count("SELECT")
    if select_count > 3:
        score -= 0.2
    elif select_count > 2:
        score -= 0.1

    return round(min(max(score, 0), 1.0), 2)


# ═══════ LLM-as-Judge ═══════

JUDGE_PROMPT = """你是数据查询质量审核员。评估以下 SQL 是否正确回答了用户的问题。

## 用户问题
{question}

## SQL 查询
```sql
{sql}
```

## 数据库上下文
- 查询引擎: {engine}
- 可用表及列:
{table_schemas}
- 语义层指标: {metrics}

## SQL 执行结果
{exec_info}

## 评估维度 (每项 0.0-1.0)

1. **correctness** (正确性): SQL 能否准确回答问题？表/字段/聚合是否正确？
2. **completeness** (完整性): 是否覆盖问题所有方面？是否遗漏维度或条件？
3. **safety** (安全性): 有无全表扫描风险？有无 LIMIT？有无危险操作？
4. **reusability** (可复用性): 作为模板的价值？是否通用（不含硬编码值）？

## 重要规则
- 如果 SQL 执行失败，correctness 必须 <= 0.3
- 如果列名不存在，必须在 improved_sql 中使用实际存在的列
- improved_sql 必须只使用「可用表及列」中列出的列名
- 如果无法用现有列回答问题，improved_sql 留空，reason 说明原因

## 严格输出 JSON (不要输出其他内容)
{{
  "correctness": 0.0,
  "completeness": 0.0,
  "safety": 0.0,
  "reusability": 0.0,
  "overall": 0.0,
  "reason": "一句话总结",
  "suggestion": "改进建议（没有则留空）",
  "improved_sql": "修复/优化后的SQL（无需修改则留空）"
}}"""


def _get_table_schemas(engine: str) -> str:
    """Get actual table schemas for the judge prompt."""
    import os
    lines = []
    try:
        if engine == "athena":
            glue = boto3.client("glue", region_name=os.environ.get("AGENTIC_AUTO_REGION",
                os.environ.get("AWS_DEFAULT_REGION", "us-east-1")))
            db = os.environ.get("AGENTIC_AUTO_ATHENA_DB", "")
            tables = glue.get_tables(DatabaseName=db).get("TableList", [])
            for t in tables:
                cols = [c["Name"] for c in t["StorageDescriptor"]["Columns"]]
                lines.append(f"  {db}.{t['Name']}: [{', '.join(cols)}]")
        elif engine == "postgresql":
            import psycopg2
            from config import POSTGRES_HOST, POSTGRES_PORT, POSTGRES_DATABASE, POSTGRES_USER, POSTGRES_PASSWORD
            if POSTGRES_HOST:
                conn = psycopg2.connect(host=POSTGRES_HOST, port=POSTGRES_PORT or 5432,
                    dbname=POSTGRES_DATABASE or "postgres", user=POSTGRES_USER, password=POSTGRES_PASSWORD)
                conn.set_session(readonly=True)
                cur = conn.cursor()
                cur.execute("""SELECT table_name, string_agg(column_name, ', ' ORDER BY ordinal_position)
                    FROM information_schema.columns WHERE table_schema='public'
                    GROUP BY table_name ORDER BY table_name""")
                for row in cur.fetchall():
                    lines.append(f"  {row[0]}: [{row[1]}]")
                conn.close()
        elif engine == "snowflake":
            try:
                from agentic_core.db_engine import SnowflakeEngine
                sf = SnowflakeEngine()
                schema_info = sf.get_schema()
                if isinstance(schema_info, dict) and "_error" not in schema_info:
                    for tname, tinfo in schema_info.items():
                        cols = [c["name"] for c in tinfo.get("columns", [])]
                        lines.append(f"  {tname}: [{', '.join(cols)}]")
            except Exception as e:
                lines.append(f"  (Snowflake schema获取失败: {str(e)[:80]})")
    except Exception as e:
        lines.append(f"  (获取失败: {str(e)[:80]})")
    return "\n".join(lines) if lines else "  (未知)"


def _call_bedrock_judge(prompt: str) -> dict:
    """Call Bedrock Haiku for judge evaluation."""
    import os
    region = os.environ.get("AGENTIC_AUTO_REGION", os.environ.get("AWS_DEFAULT_REGION", "us-east-1"))
    model_id = os.environ.get("AGENTIC_AUTO_JUDGE_MODEL", "us.anthropic.claude-haiku-4-5-20251001-v1:0")

    client = boto3.client("bedrock-runtime", region_name=region)
    try:
        resp = client.converse(
            modelId=model_id,
            messages=[{"role": "user", "content": [{"text": prompt}]}],
            inferenceConfig={"maxTokens": 512, "temperature": 0.0},
        )
        text = resp["output"]["message"]["content"][0]["text"].strip()
        # Extract JSON from response (handle markdown code blocks)
        if "```" in text:
            json_match = re.search(r'```(?:json)?\s*([\s\S]*?)```', text)
            if json_match:
                text = json_match.group(1).strip()
        return json.loads(text)
    except json.JSONDecodeError as e:
        logger.error(f"[VQR Judge] JSON parse error: {e}, raw: {text[:200]}")
        return {"error": f"JSON parse error: {str(e)}", "raw": text[:200]}
    except Exception as e:
        logger.error(f"[VQR Judge] Bedrock call error: {e}")
        return {"error": str(e)}


def _verify_sql_execution(sql: str, engine: str) -> dict:
    """Actually execute the SQL and check results."""
    import os
    result = {"executable": False, "row_count": 0, "error": None, "sample": None}

    try:
        if engine == "postgresql":
            import psycopg2
            from config import POSTGRES_HOST, POSTGRES_PORT, POSTGRES_DATABASE, POSTGRES_USER, POSTGRES_PASSWORD
            if not POSTGRES_HOST:
                result["error"] = "PostgreSQL not configured"
                return result
            conn = psycopg2.connect(
                host=POSTGRES_HOST, port=POSTGRES_PORT or 5432,
                dbname=POSTGRES_DATABASE or "postgres",
                user=POSTGRES_USER, password=POSTGRES_PASSWORD,
            )
            conn.set_session(readonly=True)
            cur = conn.cursor()
            # Add LIMIT if not present to prevent full scan
            test_sql = sql.strip().rstrip(';')
            if "LIMIT" not in test_sql.upper():
                test_sql += " LIMIT 5"
            cur.execute(test_sql)
            rows = cur.fetchall()
            cols = [d[0] for d in cur.description] if cur.description else []
            result["executable"] = True
            result["row_count"] = len(rows)
            result["columns"] = cols
            if rows:
                result["sample"] = [dict(zip(cols, [str(v)[:50] for v in r])) for r in rows[:2]]
            conn.close()

        elif engine == "athena":
            from config import REGION
            athena_db = os.environ.get("AGENTIC_AUTO_ATHENA_DB", "")
            athena_output = os.environ.get("AGENTIC_AUTO_ATHENA_OUTPUT",
                f"s3://agentic-data-{REGION}/athena-results/")
            client = boto3.client("athena", region_name=REGION)
            # Add LIMIT if not present
            test_sql = sql.strip().rstrip(';')
            if "LIMIT" not in test_sql.upper():
                test_sql += " LIMIT 5"
            resp = client.start_query_execution(
                QueryString=test_sql,
                QueryExecutionContext={"Database": athena_db},
                ResultConfiguration={"OutputLocation": athena_output},
            )
            qid = resp["QueryExecutionId"]
            # Poll for completion (max 30s)
            for _ in range(30):
                status = client.get_query_execution(QueryExecutionId=qid)
                state = status["QueryExecution"]["Status"]["State"]
                if state in ("SUCCEEDED", "FAILED", "CANCELLED"):
                    break
                time.sleep(1)  # nosemgrep: arbitrary-sleep
            if state == "SUCCEEDED":
                result["executable"] = True
                res = client.get_query_results(QueryExecutionId=qid, MaxResults=5)
                rows = res.get("ResultSet", {}).get("Rows", [])
                if rows:
                    cols = [c.get("VarCharValue", "") for c in rows[0].get("Data", [])]
                    result["columns"] = cols
                    result["row_count"] = len(rows) - 1  # minus header
                    if len(rows) > 1:
                        result["sample"] = [
                            dict(zip(cols, [c.get("VarCharValue", "") for c in r.get("Data", [])]))
                            for r in rows[1:3]
                        ]
            else:
                reason = status["QueryExecution"]["Status"].get("StateChangeReason", "Unknown")
                result["error"] = f"{state}: {reason[:200]}"

        elif engine == "snowflake":
            from agentic_core.db_engine import SnowflakeEngine
            sf = SnowflakeEngine()
            test_sql = sql.strip().rstrip(';')
            if "LIMIT" not in test_sql.upper():
                test_sql += " LIMIT 5"
            sf_result = sf.execute(test_sql, max_rows=5)
            if sf_result.get("error"):
                result["error"] = sf_result["error"][:200]
            else:
                result["executable"] = True
                result["row_count"] = sf_result.get("count", 0)
                result["columns"] = sf_result.get("columns", [])
                result["sample_rows"] = sf_result.get("rows", [])[:3]

    except Exception as e:
        result["error"] = str(e)[:200]

    return result


def llm_judge(question: str, sql: str, engine: str = "athena",
              run_sql: bool = True) -> dict:
    """Full evaluation: rule pre-screen → SQL execution → LLM judge (with exec context) → validate improved SQL.
    
    Flow:
    1. Rule pre-screening (fast, free) — filter obvious garbage
    2. SQL execution verification — does it actually run?
    3. LLM-as-Judge — with real table schemas + exec results
    4. If LLM suggests improved_sql, validate it too
    """
    result = {
        "rule_score": 0.0,
        "exec_result": None,
        "llm_scores": None,
        "overall": 0.0,
        "reason": "",
        "suggestion": "",
        "improved_sql": "",
        "improved_sql_valid": None,
    }

    # ── Step 1: Rule pre-screening ──
    rule_score = evaluate_candidate(question, sql)
    result["rule_score"] = rule_score

    if rule_score < 0.2:
        result["overall"] = rule_score
        result["reason"] = "规则预筛未通过 — SQL 质量过低"
        return result

    # ── Step 2: SQL execution verification ──
    exec_result = {"skipped": True}
    if run_sql:
        try:
            exec_result = _verify_sql_execution(sql, engine)
            logger.info(f"[VQR Judge] SQL exec: executable={exec_result.get('executable')}, rows={exec_result.get('row_count')}, error={exec_result.get('error')}")
        except Exception as e:
            exec_result = {"executable": False, "error": str(e)[:200]}
    result["exec_result"] = exec_result

    # ── Step 3: LLM-as-Judge (with real context) ──
    from agentic_core.semantic_layer import METRICS

    # Build exec info string for the prompt
    if exec_result.get("skipped"):
        exec_info = "未执行验证"
    elif exec_result.get("executable"):
        sample_str = ""
        if exec_result.get("sample"):
            sample_str = f"\n  样例数据: {json.dumps(exec_result['sample'], ensure_ascii=False)[:300]}"
        exec_info = f"执行成功，返回 {exec_result.get('row_count', 0)} 行\n  列: {exec_result.get('columns', [])}{sample_str}"
    else:
        exec_info = f"执行失败: {exec_result.get('error', '未知错误')}"

    # Get real table schemas
    table_schemas = _get_table_schemas(engine)

    metric_names = [f"{k} ({v.get('sql','')})" for k, v in list(METRICS.items())[:15] if isinstance(v, dict)]

    prompt = JUDGE_PROMPT.format(
        question=question,
        sql=sql,
        engine=engine,
        table_schemas=table_schemas,
        metrics="; ".join(metric_names) if metric_names else "未配置",
        exec_info=exec_info,
    )

    llm_result = _call_bedrock_judge(prompt)
    if "error" in llm_result:
        result["overall"] = rule_score
        result["reason"] = f"LLM 评估失败: {llm_result['error'][:100]}"
        if exec_result.get("executable"):
            result["overall"] = min(result["overall"] + 0.15, 1.0)
        return result

    result["llm_scores"] = {
        "correctness": llm_result.get("correctness", 0),
        "completeness": llm_result.get("completeness", 0),
        "safety": llm_result.get("safety", 0),
        "reusability": llm_result.get("reusability", 0),
    }
    result["reason"] = llm_result.get("reason", "")
    result["suggestion"] = llm_result.get("suggestion", "")

    # ── Step 4: Validate improved_sql if provided ──
    improved_sql = (llm_result.get("improved_sql") or "").strip()
    if improved_sql and improved_sql != sql.strip():
        result["improved_sql"] = improved_sql
        if run_sql:
            try:
                improved_exec = _verify_sql_execution(improved_sql, engine)
                result["improved_sql_valid"] = improved_exec.get("executable", False)
                if improved_exec.get("executable"):
                    logger.info(f"[VQR Judge] Improved SQL validated: rows={improved_exec.get('row_count')}")
                else:
                    logger.warning(f"[VQR Judge] Improved SQL also failed: {improved_exec.get('error','')[:100]}")
                    # LLM's fix didn't work either — clear it
                    result["improved_sql"] = ""
                    result["improved_sql_valid"] = False
                    result["suggestion"] = (result["suggestion"] or "") + " (优化SQL验证未通过，已清除)"
            except Exception as e:
                result["improved_sql_valid"] = False
                logger.error(f"[VQR Judge] Improved SQL exec error: {e}")
    elif improved_sql == sql.strip():
        # No actual change
        result["improved_sql"] = ""

    # ── Step 5: Weighted overall score ──
    llm_overall = llm_result.get("overall", 0)
    exec_bonus = 0.0
    if exec_result.get("executable"):
        exec_bonus = 0.8
        if exec_result.get("row_count", 0) > 0:
            exec_bonus = 1.0
        elif exec_result.get("row_count", 0) == 0:
            exec_bonus = 0.5
    elif exec_result.get("error"):
        exec_bonus = 0.0

    overall = round(0.5 * llm_overall + 0.3 * rule_score + 0.2 * exec_bonus, 2)
    result["overall"] = min(max(overall, 0), 1.0)

    return result


def verify_candidate(candidate_id: str, verified_by: str = "admin",
                     question: str = None, sql: str = None,
                     keywords: list = None, variants: list = None) -> str:
    """Approve a candidate and move it to VQR."""
    candidates = _load_candidates()
    cand = candidates.get(candidate_id)
    if not cand:
        return ""

    vqr = _load_vqr()
    vq_id = f"vq_{uuid.uuid4().hex[:8]}"

    # Allow overrides from admin review
    final_question = question or cand["question"]
    final_sql = sql or cand["sql"]

    # Auto-generate keywords from question
    auto_keywords = _extract_keywords(final_question)

    vqr[vq_id] = {
        "question": final_question,
        "canonical": final_question,
        "sql": final_sql,
        "engine": cand.get("engine", "athena"),
        "datasource": cand.get("datasource", ""),
        "keywords": keywords or auto_keywords,
        "variants": variants or [],
        "verified_by": verified_by,
        "verified_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "source": "feedback",
        "feedback_id": candidate_id,
        "hit_count": 0,
        "last_hit": None,
    }

    # Update candidate status
    cand["status"] = "verified"
    cand["vqr_id"] = vq_id

    _save_vqr(vqr)
    _save_candidates(candidates)
    logger.info(f"[VQR] Candidate {candidate_id} verified as {vq_id}")
    return vq_id


def reject_candidate(candidate_id: str, reason: str = ""):
    """Reject a candidate."""
    candidates = _load_candidates()
    cand = candidates.get(candidate_id)
    if cand:
        cand["status"] = "rejected"
        cand["reject_reason"] = reason
        _save_candidates(candidates)
        logger.info(f"[VQR] Candidate {candidate_id} rejected: {reason}")


def get_candidates(status: str = None) -> list:
    """List candidates, optionally filtered by status."""
    candidates = _load_candidates()
    result = []
    for cid, cand in candidates.items():
        if status and cand.get("status") != status:
            continue
        result.append({**cand, "id": cid})
    result.sort(key=lambda x: x.get("timestamp", ""), reverse=True)
    return result


def get_verified_queries() -> list:
    """List all verified queries."""
    vqr = _load_vqr()
    result = []
    for vid, vq in vqr.items():
        result.append({**vq, "id": vid})
    result.sort(key=lambda x: x.get("hit_count", 0), reverse=True)
    return result


def record_hit(vqr_id: str):
    """Increment hit count for a verified query."""
    vqr = _load_vqr()
    if vqr_id in vqr:
        vqr[vqr_id]["hit_count"] = vqr[vqr_id].get("hit_count", 0) + 1
        vqr[vqr_id]["last_hit"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        _save_vqr(vqr)


def get_stats() -> dict:
    """Get VQR statistics."""
    vqr = _load_vqr()
    candidates = _load_candidates()

    total_hits = sum(v.get("hit_count", 0) for v in vqr.values())
    pending = sum(1 for c in candidates.values() if c.get("status") == "pending")
    verified = len(vqr)
    rejected = sum(1 for c in candidates.values() if c.get("status") == "rejected")

    return {
        "verified_count": verified,
        "pending_count": pending,
        "rejected_count": rejected,
        "total_hits": total_hits,
        "queries": [
            {"question": v.get("question", ""), "hits": v.get("hit_count", 0), "id": vid}
            for vid, v in sorted(vqr.items(), key=lambda x: -x[1].get("hit_count", 0))
        ][:20],
    }


# ═══════ 手动添加 VQR ═══════

def add_verified_query(question: str, sql: str, engine: str = "athena",
                       datasource: str = "", keywords: list = None,
                       variants: list = None, verified_by: str = "admin") -> str:
    """Manually add a verified query (bypasses candidate queue)."""
    vqr = _load_vqr()
    vq_id = f"vq_{uuid.uuid4().hex[:8]}"

    auto_keywords = keywords or _extract_keywords(question)

    vqr[vq_id] = {
        "question": question,
        "canonical": question,
        "sql": sql,
        "engine": engine,
        "datasource": datasource,
        "keywords": auto_keywords,
        "variants": variants or [],
        "verified_by": verified_by,
        "verified_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "source": "manual",
        "feedback_id": None,
        "hit_count": 0,
        "last_hit": None,
    }

    _save_vqr(vqr)
    logger.info(f"[VQR] Manual query added: {vq_id}")
    return vq_id


def update_verified_query(vqr_id: str, updates: dict) -> bool:
    """Update fields of a verified query."""
    vqr = _load_vqr()
    if vqr_id not in vqr:
        return False
    for k, v in updates.items():
        if k in ("question", "sql", "keywords", "variants", "engine", "datasource", "canonical"):
            vqr[vqr_id][k] = v
    _save_vqr(vqr)
    return True


def delete_verified_query(vqr_id: str) -> bool:
    """Delete a verified query."""
    vqr = _load_vqr()
    if vqr_id in vqr:
        del vqr[vqr_id]
        _save_vqr(vqr)
        return True
    return False


# ═══════ 辅助函数 ═══════

def _extract_keywords(question: str) -> list:
    """Extract meaningful keywords from a question."""
    from agentic_core.semantic_layer import METRICS, DIMENSIONS, SYNONYMS

    keywords = []
    # Match known metric names / dimension names / synonyms
    all_terms = list(METRICS.keys()) + list(DIMENSIONS.keys()) + list(SYNONYMS.keys())
    for term in all_terms:
        if term in question:
            keywords.append(term)

    # Deduplicate while preserving order
    seen = set()
    result = []
    for kw in keywords:
        if kw not in seen:
            seen.add(kw)
            result.append(kw)
    return result
