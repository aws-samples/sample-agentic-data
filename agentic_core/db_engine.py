"""
SQL Engine Abstraction — supports SQLite (demo), Snowflake, and PostgreSQL.

Usage:
    from agentic_core.db_engine import get_engine, get_multi_engine
    engine = get_engine()          # default engine
    engine = get_engine("snowflake")  # specific engine
    result = engine.execute(sql)   # {"columns":[], "rows":[], "count":N}
    schema = engine.get_schema()   # {"table": {"columns":[...], "record_count":N}}
"""

import os, json, logging, sqlite3, re
from abc import ABC, abstractmethod
from typing import Dict, List, Any, Optional

logger = logging.getLogger(__name__)


def _safe_identifier(name: str) -> str:
    """Validate SQL identifier to prevent injection. Only allows alphanumeric, underscore, dot."""
    if not re.match(r'^[a-zA-Z_][a-zA-Z0-9_.]*$', name):
        raise ValueError(f"Invalid SQL identifier: {name}")
    return name


class SQLEngine(ABC):
    """Abstract base for SQL data sources."""
    name: str = "unknown"
    dialect: str = "sql"

    @abstractmethod
    def execute(self, sql: str, max_rows: int = 200) -> Dict[str, Any]: ...

    @abstractmethod
    def get_schema(self) -> Dict[str, Any]: ...

    @abstractmethod
    def test_connection(self) -> Dict[str, Any]: ...

    def get_source_label(self) -> str:
        return self.name


# ═══════ SQLite (demo/default) ═══════

class SQLiteEngine(SQLEngine):
    name = "SQLite (Demo)"
    dialect = "sqlite"

    def __init__(self, db_path=None):
        self.db_path = db_path or os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            "data", "agentic_auto.db"
        )

    def execute(self, sql, max_rows=200):
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        try:
            cur = conn.execute(sql)
            cols = [d[0] for d in cur.description] if cur.description else []
            rows = [dict(r) for r in cur.fetchmany(max_rows)]
            return {"columns": cols, "rows": rows, "count": len(rows)}
        except Exception as e:
            return {"error": str(e), "sql": sql}
        finally:
            conn.close()

    def get_schema(self):
        conn = sqlite3.connect(self.db_path)
        tables = {}
        for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall():
            tname = row[0]
            cols = conn.execute(f"PRAGMA table_info({_safe_identifier(tname)})").fetchall()  # nosec B608  # nosemgrep: sqlalchemy-execute-raw-query  # nosemgrep: formatted-sql-query
            sample = conn.execute(f"SELECT * FROM {_safe_identifier(tname)} LIMIT 2").fetchall()  # nosec B608  # nosemgrep: sqlalchemy-execute-raw-query  # nosemgrep: formatted-sql-query
            count = conn.execute(f"SELECT COUNT(*) FROM {_safe_identifier(tname)}").fetchone()[0]  # nosec B608  # nosemgrep: sqlalchemy-execute-raw-query  # nosemgrep: formatted-sql-query
            tables[tname] = {
                "columns": [{"name": c[1], "type": c[2]} for c in cols],
                "record_count": count,
                "sample": [dict(zip([c[1] for c in cols], s)) for s in sample],
            }
        conn.close()
        return tables

    def test_connection(self):
        try:
            conn = sqlite3.connect(self.db_path)
            tables = conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
            conn.close()
            return {"ok": True, "message": f"SQLite OK — {len(tables)} tables",
                    "details": {"path": self.db_path, "tables": [t[0] for t in tables]}}
        except Exception as e:
            return {"ok": False, "message": str(e), "details": {}}


# ═══════ Snowflake ═══════

class SnowflakeEngine(SQLEngine):
    name = "Snowflake"
    dialect = "snowflake"

    def __init__(self, account=None, user=None, password=None, private_key_path=None,
                 warehouse=None, database=None, schema=None, role=None):
        self.account = account or os.getenv("SNOWFLAKE_ACCOUNT", "")
        self.user = user or os.getenv("SNOWFLAKE_USER", "")
        self.password = password or os.getenv("SNOWFLAKE_PASSWORD", "")
        self.private_key_path = private_key_path or os.getenv("SNOWFLAKE_PRIVATE_KEY_PATH", "")
        self.warehouse = warehouse or os.getenv("SNOWFLAKE_WAREHOUSE", "COMPUTE_WH")
        self.database = database or os.getenv("SNOWFLAKE_DATABASE", "")
        self.schema = schema or os.getenv("SNOWFLAKE_SCHEMA", "PUBLIC")
        self.role = role or os.getenv("SNOWFLAKE_ROLE", "")
        self._conn = None

    def _get_connection(self):
        if self._conn is not None:
            try:
                self._conn.cursor().execute("SELECT 1")
                return self._conn
            except Exception:
                self._conn = None
        try:
            import snowflake.connector
        except ImportError:
            raise RuntimeError("snowflake-connector-python not installed. Run: pip install snowflake-connector-python")

        kw = {"account": self.account, "user": self.user,
              "warehouse": self.warehouse, "database": self.database,
              "schema": self.schema, "login_timeout": 15, "network_timeout": 30}

        if self.private_key_path and os.path.exists(self.private_key_path):
            from cryptography.hazmat.backends import default_backend
            from cryptography.hazmat.primitives import serialization
            with open(self.private_key_path, "rb") as f:
                p_key = serialization.load_pem_private_key(f.read(), password=None, backend=default_backend())
            kw["private_key"] = p_key.private_bytes(
                encoding=serialization.Encoding.DER,
                format=serialization.PrivateFormat.PKCS8,
                encryption_algorithm=serialization.NoEncryption()
            )
        else:
            kw["password"] = self.password

        if self.role:
            kw["role"] = self.role

        self._conn = snowflake.connector.connect(**kw)
        logger.info(f"Snowflake connected: {self.account}/{self.database}/{self.schema}")
        return self._conn

    def execute(self, sql, max_rows=200):
        try:
            conn = self._get_connection()
            cur = conn.cursor()
            cur.execute(sql)
            cols = [desc[0] for desc in cur.description] if cur.description else []
            rows = []
            for row in cur.fetchmany(max_rows):
                # Convert Snowflake types (Decimal, datetime) to JSON-safe
                rows.append({c: _json_safe(v) for c, v in zip(cols, row)})
            cur.close()
            return {"columns": cols, "rows": rows, "count": len(rows)}
        except Exception as e:
            logger.error(f"Snowflake SQL error: {e}")
            return {"error": str(e), "sql": sql}

    def get_schema(self):
        try:
            conn = self._get_connection()
            cur = conn.cursor()
            _db = _safe_identifier(self.database)
            _sch = _safe_identifier(self.schema.upper())
            cur.execute(f"SELECT TABLE_NAME, ROW_COUNT FROM {_db}.INFORMATION_SCHEMA.TABLES WHERE TABLE_SCHEMA = '{_sch}' AND TABLE_TYPE = 'BASE TABLE' ORDER BY TABLE_NAME")  # nosec B608
            table_list = cur.fetchall()
            tables = {}
            for (tname, row_count) in table_list:
                _tname = _safe_identifier(tname)
                cur.execute(f"SELECT COLUMN_NAME, DATA_TYPE, COMMENT FROM {_db}.INFORMATION_SCHEMA.COLUMNS WHERE TABLE_SCHEMA = '{_sch}' AND TABLE_NAME = '{_tname}' ORDER BY ORDINAL_POSITION")  # nosec B608
                columns = [{"name": c[0], "type": c[1], "comment": c[2] or ""} for c in cur.fetchall()]
                try:
                    cur.execute(f'SELECT * FROM "{_safe_identifier(self.database)}"."{_safe_identifier(self.schema)}"."{_safe_identifier(tname)}" LIMIT 2')  # nosec B608  # nosemgrep: sqlalchemy-execute-raw-query  # nosemgrep: formatted-sql-query
                    scols = [d[0] for d in cur.description]
                    sample = [dict(zip(scols, [_json_safe(v) for v in r])) for r in cur.fetchall()]
                except Exception:
                    sample = []
                tables[tname] = {"columns": columns, "record_count": row_count or 0, "sample": sample}
            cur.close()
            return tables
        except Exception as e:
            logger.error(f"Snowflake schema error: {e}")
            return {"_error": str(e)}

    def test_connection(self):
        try:
            conn = self._get_connection()
            cur = conn.cursor()
            cur.execute("SELECT CURRENT_VERSION(), CURRENT_ACCOUNT(), CURRENT_DATABASE(), CURRENT_SCHEMA(), CURRENT_WAREHOUSE()")
            row = cur.fetchone()
            _db2 = _safe_identifier(self.database)
            _sch2 = _safe_identifier(self.schema.upper())
            cur.execute(f"SELECT COUNT(*) FROM {_db2}.INFORMATION_SCHEMA.TABLES WHERE TABLE_SCHEMA = '{_sch2}' AND TABLE_TYPE = 'BASE TABLE'")  # nosec B608
            tc = cur.fetchone()[0]
            cur.close()
            return {"ok": True, "message": f"Snowflake OK — {tc} tables",
                    "details": {"version": row[0], "account": row[1], "database": row[2],
                                "schema": row[3], "warehouse": row[4], "table_count": tc}}
        except Exception as e:
            return {"ok": False, "message": str(e), "details": {}}

    def get_source_label(self):
        return f"Snowflake ({self.database}.{self.schema})"


# ═══════ PostgreSQL (future real RDS) ═══════

class PostgreSQLEngine(SQLEngine):
    name = "PostgreSQL"
    dialect = "postgresql"

    def __init__(self, host=None, port=None, database=None, user=None, password=None, schema=None):
        self.host = host or os.getenv("POSTGRES_HOST", "localhost")
        self.port = int(port or os.getenv("POSTGRES_PORT", "5432"))
        self.database = database or os.getenv("POSTGRES_DATABASE", "agentic_auto")
        self.user = user or os.getenv("POSTGRES_USER", "postgres")
        self.password = password or os.getenv("POSTGRES_PASSWORD", "")
        self.schema = schema or os.getenv("POSTGRES_SCHEMA", "public")
        self._conn = None

    def _get_connection(self):
        if self._conn:
            try:
                self._conn.cursor().execute("SELECT 1")
                return self._conn
            except Exception:
                self._conn = None
        try:
            import psycopg2
        except ImportError:
            raise RuntimeError("psycopg2 not installed. Run: pip install psycopg2-binary")
        self._conn = psycopg2.connect(host=self.host, port=self.port, dbname=self.database,
                                       user=self.user, password=self.password, connect_timeout=10)
        self._conn.autocommit = True
        return self._conn

    def execute(self, sql, max_rows=200):
        try:
            import psycopg2.extras
            conn = self._get_connection()
            cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
            cur.execute(sql)
            cols = [desc[0] for desc in cur.description] if cur.description else []
            rows = [dict(r) for r in cur.fetchmany(max_rows)]
            cur.close()
            return {"columns": cols, "rows": rows, "count": len(rows)}
        except Exception as e:
            return {"error": str(e), "sql": sql}

    def get_schema(self):
        try:
            conn = self._get_connection()
            cur = conn.cursor()
            cur.execute(f"SELECT table_name FROM information_schema.tables WHERE table_schema='{_safe_identifier(self.schema)}' AND table_type='BASE TABLE'")  # nosec B608  # nosemgrep: sqlalchemy-execute-raw-query  # nosemgrep: formatted-sql-query
            tables = {}
            for (tname,) in cur.fetchall():
                cur.execute(f"SELECT column_name, data_type FROM information_schema.columns WHERE table_schema='{_safe_identifier(self.schema)}' AND table_name='{_safe_identifier(tname)}' ORDER BY ordinal_position")  # nosec B608  # nosemgrep: sqlalchemy-execute-raw-query  # nosemgrep: formatted-sql-query
                columns = [{"name": c[0], "type": c[1]} for c in cur.fetchall()]
                cur.execute(f"SELECT COUNT(*) FROM {_safe_identifier(self.schema)}.{_safe_identifier(tname)}")  # nosec B608  # nosemgrep: sqlalchemy-execute-raw-query  # nosemgrep: formatted-sql-query
                count = cur.fetchone()[0]
                tables[tname] = {"columns": columns, "record_count": count, "sample": []}
            cur.close()
            return tables
        except Exception as e:
            return {"_error": str(e)}

    def test_connection(self):
        try:
            conn = self._get_connection()
            cur = conn.cursor()
            cur.execute("SELECT version()")
            ver = cur.fetchone()[0]
            cur.close()
            return {"ok": True, "message": f"PostgreSQL OK", "details": {"version": ver, "host": self.host}}
        except Exception as e:
            return {"ok": False, "message": str(e), "details": {}}


# ═══════ MultiEngine Manager ═══════

class MySQLEngine(SQLEngine):
    name = "MySQL"
    dialect = "mysql"

    def __init__(self, host=None, port=None, database=None, user=None, password=None):
        self.host = host or os.getenv("MYSQL_HOST", "localhost")
        self.port = int(port or os.getenv("MYSQL_PORT", "3306"))
        self.database = database or os.getenv("MYSQL_DATABASE", "agentic_auto")
        self.user = user or os.getenv("MYSQL_USER", "admin")
        self.password = password or os.getenv("MYSQL_PASSWORD", "")
        self._conn = None

    def _get_connection(self):
        if self._conn:
            try:
                self._conn.ping(reconnect=True)
                return self._conn
            except Exception:
                self._conn = None
        try:
            import pymysql
        except ImportError:
            raise RuntimeError("pymysql not installed. Run: pip install pymysql")
        self._conn = pymysql.connect(
            host=self.host, port=self.port, database=self.database,
            user=self.user, password=self.password,
            connect_timeout=10, charset='utf8mb4',
            cursorclass=pymysql.cursors.DictCursor
        )
        return self._conn

    def execute(self, sql, max_rows=200):
        try:
            conn = self._get_connection()
            with conn.cursor() as cur:
                cur.execute(sql)
                if cur.description:
                    cols = [desc[0] for desc in cur.description]
                    rows = [dict(r) for r in cur.fetchmany(max_rows)]
                else:
                    cols, rows = [], []
                    conn.commit()
            return {"columns": cols, "rows": rows, "count": len(rows)}
        except Exception as e:
            return {"error": str(e), "sql": sql}

    def get_schema(self):
        try:
            conn = self._get_connection()
            with conn.cursor() as cur:
                cur.execute(f"SELECT table_name FROM information_schema.tables WHERE table_schema='{_safe_identifier(self.database)}' AND table_type='BASE TABLE'")  # nosec B608  # nosemgrep: sqlalchemy-execute-raw-query  # nosemgrep: formatted-sql-query
                tables = {}
                for row in cur.fetchall():
                    tname = row['TABLE_NAME']
                    cur.execute(f"SELECT column_name, data_type FROM information_schema.columns WHERE table_schema='{_safe_identifier(self.database)}' AND table_name='{_safe_identifier(tname)}' ORDER BY ordinal_position")  # nosec B608  # nosemgrep: sqlalchemy-execute-raw-query  # nosemgrep: formatted-sql-query
                    columns = [{"name": c['COLUMN_NAME'], "type": c['DATA_TYPE']} for c in cur.fetchall()]
                    cur.execute(f"SELECT COUNT(*) as cnt FROM `{_safe_identifier(tname)}`")  # nosec B608  # nosemgrep: sqlalchemy-execute-raw-query  # nosemgrep: formatted-sql-query
                    count = cur.fetchone()['cnt']
                    tables[tname] = {"columns": columns, "record_count": count, "sample": []}
            return tables
        except Exception as e:
            return {"_error": str(e)}

    def test_connection(self):
        try:
            conn = self._get_connection()
            with conn.cursor() as cur:
                cur.execute("SELECT VERSION()")
                ver = cur.fetchone()['VERSION()']
            return {"ok": True, "message": "MySQL OK", "details": {"version": ver, "host": self.host}}
        except Exception as e:
            return {"ok": False, "message": str(e), "details": {}}


class MultiEngine:
    """Manages multiple SQL engines."""
    def __init__(self):
        self.engines: Dict[str, SQLEngine] = {}
        self.default_key: str = ""

    def register(self, key, engine, is_default=False):
        self.engines[key] = engine
        if is_default or not self.default_key:
            self.default_key = key
        logger.info(f"SQL engine registered: {key} ({engine.name})" + (" [default]" if is_default else ""))

    def get(self, key=None):
        k = key or self.default_key
        if k not in self.engines:
            raise KeyError(f"SQL engine '{k}' not found. Available: {list(self.engines.keys())}")
        return self.engines[k]

    def execute(self, sql, engine_key=None, max_rows=200):
        return self.get(engine_key).execute(sql, max_rows)

    def get_all_schemas(self):
        result = {}
        for key, eng in self.engines.items():
            try:
                for tname, tinfo in eng.get_schema().items():
                    result[f"{key}.{tname}"] = {**tinfo, "engine": key, "engine_name": eng.name}
            except Exception as e:
                result[f"{key}._error"] = {"error": str(e)}
        return result

    def test_all(self):
        return {key: eng.test_connection() for key, eng in self.engines.items()}

    @property
    def engine_names(self):
        return list(self.engines.keys())


# ═══════ Helpers ═══════

def _json_safe(v):
    """Convert Snowflake/PG types to JSON-serializable."""
    import decimal, datetime
    if isinstance(v, decimal.Decimal):
        return float(v)
    if isinstance(v, (datetime.datetime, datetime.date)):
        return v.isoformat()
    if isinstance(v, bytes):
        return v.hex()
    return v


# ═══════ Singleton Factory ═══════

_multi_engine = None

def get_multi_engine() -> MultiEngine:
    global _multi_engine
    if _multi_engine is not None:
        return _multi_engine

    _multi_engine = MultiEngine()

    # SQLite (demo) — only if db file exists and has tables
    sqlite_eng = SQLiteEngine()
    if os.path.exists(sqlite_eng.db_path):
        try:
            schema = sqlite_eng.get_schema()
            if schema:  # has actual tables
                _multi_engine.register("sqlite", sqlite_eng, is_default=True)
                logger.info(f"SQLite demo: {len(schema)} tables")
            else:
                logger.info("SQLite demo: db exists but no tables, skipping")
        except Exception:
            logger.info("SQLite demo: failed to read, skipping")
    else:
        logger.info("SQLite demo: db file not found, skipping")

    # Snowflake (if configured)
    if os.getenv("SNOWFLAKE_ACCOUNT"):
        try:
            _multi_engine.register("snowflake", SnowflakeEngine())
        except Exception as e:
            logger.warning(f"Snowflake init failed: {e}")

    # PostgreSQL (if configured)
    if os.getenv("POSTGRES_HOST"):
        try:
            _multi_engine.register("postgresql", PostgreSQLEngine())
        except Exception as e:
            logger.warning(f"PostgreSQL init failed: {e}")

    # MySQL (if configured)
    if os.getenv("MYSQL_HOST"):
        try:
            _multi_engine.register("mysql", MySQLEngine())
        except Exception as e:
            logger.warning(f"MySQL init failed: {e}")

    # Default from env
    default = os.getenv("SQL_ENGINE", "sqlite")
    if default in _multi_engine.engines:
        _multi_engine.default_key = default

    return _multi_engine

def get_engine(key=None) -> SQLEngine:
    return get_multi_engine().get(key)
