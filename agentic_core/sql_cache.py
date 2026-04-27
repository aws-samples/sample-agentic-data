# Simple SQL cache for VQR feedback integration
# Keyed by session_id → {sql, engine, question}
_cache = {}

def set_sql(session_id, sql, engine, question):
    if session_id and sql:
        _cache[session_id] = {"sql": sql, "engine": engine, "question": question}

def get_sql(session_id):
    return _cache.get(session_id)

def clear(session_id):
    _cache.pop(session_id, None)
