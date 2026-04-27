"""
Tableau REST API Client — 连接 Tableau Server/Online，
提取报表元数据 + 查询报表数据，作为 ChatBI 的知识源。

Usage:
    from agentic_core.tableau_client import get_tableau_client
    client = get_tableau_client()
    dashboards = client.list_dashboards()
    data = client.query_view_data(view_id)
    fields = client.get_datasource_fields(ds_id)
"""

import os, json, logging, time, csv, io
from typing import Dict, List, Any, Optional
from urllib.parse import urljoin

logger = logging.getLogger(__name__)


class TableauClient:
    """Tableau REST API client with auto-auth and metadata caching."""

    API_VERSION = "3.22"  # Tableau 2024.1+

    def __init__(self, server_url=None, site_id=None,
                 token_name=None, token_secret=None,
                 username=None, password=None):
        self.server_url = (server_url or os.getenv("TABLEAU_SERVER_URL", "")).rstrip("/")
        self.site_content_url = site_id or os.getenv("TABLEAU_SITE_ID", "")
        self.token_name = token_name or os.getenv("TABLEAU_TOKEN_NAME", "")
        self.token_secret = token_secret or os.getenv("TABLEAU_TOKEN_SECRET", "")
        self.username = username or os.getenv("TABLEAU_USERNAME", "")
        self.password = password or os.getenv("TABLEAU_PASSWORD", "")

        self._auth_token = None
        self._site_id = None
        self._user_id = None
        self._auth_expires = 0

        # Metadata cache
        self._workbooks_cache = None
        self._views_cache = None
        self._datasources_cache = None
        self._fields_cache = {}  # ds_id -> fields
        self._cache_time = 0
        self._cache_ttl = 300  # 5 min

    @property
    def base_url(self):
        return f"{self.server_url}/api/{self.API_VERSION}"

    @property
    def site_url(self):
        return f"{self.base_url}/sites/{self._site_id}"

    def _request(self, method, path, json_body=None, accept="application/json"):
        """Make authenticated HTTP request to Tableau REST API."""
        import urllib.request, urllib.error
        url = path if path.startswith("http") else f"{self.base_url}/{path.lstrip('/')}"
        headers = {"Accept": accept, "Content-Type": "application/json"}
        if self._auth_token:
            headers["X-Tableau-Auth"] = self._auth_token

        data = json.dumps(json_body).encode() if json_body else None
        req = urllib.request.Request(url, data=data, headers=headers, method=method)

        try:
            with urllib.request.urlopen(req, timeout=30) as resp:  # nosec B310  # nosemgrep: dynamic-urllib-use-detected
                body = resp.read()
                ct = resp.headers.get("Content-Type", "")
                if "json" in ct:
                    return json.loads(body)
                return body.decode("utf-8")
        except urllib.error.HTTPError as e:
            error_body = e.read().decode("utf-8", errors="replace")[:500]
            logger.error(f"Tableau API {method} {url}: {e.code} {error_body}")  # nosemgrep: logging-error-without-handling
            raise RuntimeError(f"Tableau API error {e.code}: {error_body}")

    # ── Authentication ──

    def signin(self):
        """Sign in using PAT (preferred) or username/password."""
        if not self.server_url:
            raise RuntimeError("TABLEAU_SERVER_URL not configured")

        if self.token_name and self.token_secret:
            body = {
                "credentials": {
                    "personalAccessTokenName": self.token_name,
                    "personalAccessTokenSecret": self.token_secret,
                    "site": {"contentUrl": self.site_content_url}
                }
            }
        elif self.username and self.password:
            body = {
                "credentials": {
                    "name": self.username,
                    "password": self.password,
                    "site": {"contentUrl": self.site_content_url}
                }
            }
        else:
            raise RuntimeError("No Tableau credentials configured (set TABLEAU_TOKEN_NAME + TABLEAU_TOKEN_SECRET or TABLEAU_USERNAME + TABLEAU_PASSWORD)")

        result = self._request("POST", f"{self.base_url}/auth/signin", body)
        creds = result.get("credentials", {})
        self._auth_token = creds.get("token")
        self._site_id = creds.get("site", {}).get("id")
        self._user_id = creds.get("user", {}).get("id")
        self._auth_expires = time.time() + 7200  # 2h default
        logger.info(f"Tableau signed in: site={self._site_id}, user={self._user_id}")
        return True

    def _ensure_auth(self):
        if not self._auth_token or time.time() > self._auth_expires:
            self.signin()

    def signout(self):
        if self._auth_token:
            try:
                self._request("POST", f"{self.base_url}/auth/signout")
            except Exception:
                pass
            self._auth_token = None

    # ── Workbooks & Views ──

    def list_workbooks(self, force=False) -> List[Dict]:
        """List all workbooks on the site."""
        if self._workbooks_cache and not force and time.time() - self._cache_time < self._cache_ttl:
            return self._workbooks_cache
        self._ensure_auth()
        result = self._request("GET", f"{self.site_url}/workbooks?pageSize=100")
        workbooks = result.get("workbooks", {}).get("workbook", [])
        self._workbooks_cache = workbooks
        self._cache_time = time.time()
        return workbooks

    def list_views(self, force=False) -> List[Dict]:
        """List all views (worksheets/dashboards) on the site."""
        if self._views_cache and not force and time.time() - self._cache_time < self._cache_ttl:
            return self._views_cache
        self._ensure_auth()
        result = self._request("GET", f"{self.site_url}/views?pageSize=300&includeUsageStatistics=true")
        views = result.get("views", {}).get("view", [])
        self._views_cache = views
        self._cache_time = time.time()
        return views

    def get_workbook_views(self, workbook_id: str) -> List[Dict]:
        """Get views for a specific workbook."""
        self._ensure_auth()
        result = self._request("GET", f"{self.site_url}/workbooks/{workbook_id}/views")
        return result.get("views", {}).get("view", [])

    # ── View Data (核心：查询报表数据) ──

    def query_view_data(self, view_id: str, max_rows: int = 500,
                        filters: Optional[Dict[str, str]] = None) -> Dict[str, Any]:
        """Query underlying data from a Tableau view.
        Returns {"columns":[], "rows":[], "count":N}
        
        filters: {"Region": "East", "Category": "Technology"} → applied as URL filter params
        """
        self._ensure_auth()
        url = f"{self.site_url}/views/{view_id}/data"
        # Add filters as vf_ params
        if filters:
            params = "&".join(f"vf_{k}={v}" for k, v in filters.items())
            url += f"?{params}"

        csv_text = self._request("GET", url, accept="text/csv")
        if isinstance(csv_text, bytes):
            csv_text = csv_text.decode("utf-8")

        reader = csv.DictReader(io.StringIO(csv_text))
        rows = []
        for i, row in enumerate(reader):
            if i >= max_rows:
                break
            rows.append(dict(row))

        columns = list(rows[0].keys()) if rows else []
        return {"columns": columns, "rows": rows, "count": len(rows), "view_id": view_id}

    def get_view_image(self, view_id: str, resolution: str = "high") -> bytes:
        """Get a PNG image of a view (for multi-modal analysis)."""
        self._ensure_auth()
        url = f"{self.site_url}/views/{view_id}/image?resolution={resolution}"
        return self._request("GET", url, accept="image/png")

    # ── Data Sources & Fields (元数据) ──

    def list_datasources(self, force=False) -> List[Dict]:
        """List all published data sources."""
        if self._datasources_cache and not force and time.time() - self._cache_time < self._cache_ttl:
            return self._datasources_cache
        self._ensure_auth()
        result = self._request("GET", f"{self.site_url}/datasources?pageSize=100")
        ds = result.get("datasources", {}).get("datasource", [])
        self._datasources_cache = ds
        self._cache_time = time.time()
        return ds

    def get_datasource_fields(self, datasource_id: str, force=False) -> List[Dict]:
        """Get field metadata for a data source (name, type, description, formula)."""
        if datasource_id in self._fields_cache and not force:
            return self._fields_cache[datasource_id]
        self._ensure_auth()
        result = self._request("GET", f"{self.site_url}/datasources/{datasource_id}/fields")
        fields = result.get("fields", {}).get("field", [])
        self._fields_cache[datasource_id] = fields
        return fields

    # ── Metadata GraphQL API (richer field info) ──

    def query_metadata(self, graphql_query: str) -> Dict:
        """Query Tableau Metadata API (GraphQL) for rich field info."""
        self._ensure_auth()
        url = f"{self.server_url}/api/metadata/graphql"
        return self._request("POST", url, {"query": graphql_query})

    def get_all_fields_metadata(self) -> List[Dict]:
        """Get field metadata for all published data sources via Metadata API."""
        query = """{
            publishedDatasources {
                name
                id
                description
                fields {
                    name
                    description
                    dataType
                    isCalculated
                    formula
                }
                upstreamTables {
                    name
                    schema
                    database { name }
                }
            }
        }"""
        try:
            result = self.query_metadata(query)
            return result.get("data", {}).get("publishedDatasources", [])
        except Exception as e:
            logger.warning(f"Metadata API not available: {e}")
            return []

    # ── Build Semantic Context (给 Agent 用) ──

    def build_semantic_context(self) -> Dict[str, Any]:
        """Build a complete semantic context from Tableau metadata.
        Returns a structure that can be injected into Agent prompts.
        """
        context = {
            "source": "tableau",
            "server": self.server_url,
            "dashboards": [],
            "datasources": [],
            "field_index": {},  # field_name -> {type, description, formula, dashboard}
        }

        try:
            # Get all views
            views = self.list_views()
            for v in views:
                context["dashboards"].append({
                    "id": v.get("id"),
                    "name": v.get("name"),
                    "workbook": v.get("workbook", {}).get("name", ""),
                    "content_url": v.get("contentUrl", ""),
                    "view_count": v.get("usage", {}).get("totalViewCount", 0),
                })

            # Get fields from Metadata API (preferred) or REST API
            metadata = self.get_all_fields_metadata()
            if metadata:
                for ds in metadata:
                    ds_info = {
                        "name": ds.get("name"),
                        "description": ds.get("description", ""),
                        "fields": [],
                        "upstream_tables": [
                            f"{t.get('database',{}).get('name','')}.{t.get('schema','')}.{t.get('name','')}"
                            for t in ds.get("upstreamTables", [])
                        ],
                    }
                    for f in ds.get("fields", []):
                        field_info = {
                            "name": f.get("name"),
                            "type": f.get("dataType"),
                            "description": f.get("description", ""),
                            "is_calculated": f.get("isCalculated", False),
                            "formula": f.get("formula", ""),
                        }
                        ds_info["fields"].append(field_info)
                        context["field_index"][f["name"]] = {
                            **field_info,
                            "datasource": ds.get("name"),
                        }
                    context["datasources"].append(ds_info)
            else:
                # Fallback to REST API
                datasources = self.list_datasources()
                for ds in datasources:
                    fields = self.get_datasource_fields(ds["id"])
                    ds_info = {
                        "name": ds.get("name"),
                        "description": ds.get("description", ""),
                        "fields": [
                            {"name": f.get("name"), "type": f.get("dataType"),
                             "description": f.get("description", "")}
                            for f in fields
                        ],
                    }
                    context["datasources"].append(ds_info)
                    for f in fields:
                        context["field_index"][f["name"]] = {
                            "name": f["name"], "type": f.get("dataType"),
                            "datasource": ds.get("name"),
                        }

        except Exception as e:
            logger.error(f"Failed to build Tableau semantic context: {e}")
            context["error"] = str(e)

        return context

    def format_prompt_context(self) -> str:
        """Format Tableau metadata as text for Agent prompt injection."""
        ctx = self.build_semantic_context()
        lines = ["## Tableau 报表数据源 (自动提取)", ""]

        if ctx.get("dashboards"):
            lines.append(f"### 可用 Dashboard ({len(ctx['dashboards'])} 个)")
            for d in ctx["dashboards"][:20]:
                lines.append(f"- **{d['name']}** (工作簿: {d['workbook']}, 浏览: {d.get('view_count',0)}次)")
            lines.append("")

        if ctx.get("datasources"):
            lines.append(f"### 数据源字段 ({len(ctx['datasources'])} 个数据源)")
            for ds in ctx["datasources"][:10]:
                lines.append(f"\n#### {ds['name']}")
                if ds.get("description"):
                    lines.append(f"  {ds['description']}")
                if ds.get("upstream_tables"):
                    lines.append(f"  上游表: {', '.join(ds['upstream_tables'][:5])}")
                for f in ds.get("fields", [])[:30]:
                    calc = f" [计算字段: {f['formula']}]" if f.get("is_calculated") and f.get("formula") else ""
                    desc = f" — {f['description']}" if f.get("description") else ""
                    lines.append(f"  - {f['name']} ({f.get('type','')}){desc}{calc}")
            lines.append("")

        return "\n".join(lines)

    # ── Connection Test ──

    def test_connection(self) -> Dict[str, Any]:
        """Test Tableau connection."""
        if not self.server_url:
            return {"ok": False, "message": "TABLEAU_SERVER_URL not configured", "details": {}}
        try:
            self.signin()
            views = self.list_views(force=True)
            workbooks = self.list_workbooks(force=True)
            datasources = self.list_datasources(force=True)
            return {
                "ok": True,
                "message": f"Tableau OK — {len(workbooks)} workbooks, {len(views)} views, {len(datasources)} datasources",
                "details": {
                    "server": self.server_url,
                    "site_id": self._site_id,
                    "workbooks": len(workbooks),
                    "views": len(views),
                    "datasources": len(datasources),
                }
            }
        except Exception as e:
            return {"ok": False, "message": str(e), "details": {}}


# ═══════ Demo/Mock Client (no real Tableau) ═══════

class TableauMockClient(TableauClient):
    """Mock client for demo — returns realistic fake data without a real Tableau Server."""

    def __init__(self):
        super().__init__(server_url="https://tableau.demo.local")
        self._mock = True

    def signin(self):
        self._auth_token = "mock-token"
        self._site_id = "demo-site"
        self._user_id = "demo-user"
        self._auth_expires = time.time() + 99999
        return True

    def list_workbooks(self, force=False):
        return [
            {"id": "wb-1", "name": "销售业绩分析", "project": {"name": "营销部"}},
            {"id": "wb-2", "name": "客户画像", "project": {"name": "客户部"}},
            {"id": "wb-3", "name": "运营监控大屏", "project": {"name": "运营部"}},
        ]

    def list_views(self, force=False):
        return [
            {"id": "v-1", "name": "月度销售趋势", "contentUrl": "SalesAnalysis/MonthlyTrend",
             "workbook": {"name": "销售业绩分析"}, "usage": {"totalViewCount": 1247}},
            {"id": "v-2", "name": "区域销售分布", "contentUrl": "SalesAnalysis/RegionMap",
             "workbook": {"name": "销售业绩分析"}, "usage": {"totalViewCount": 893}},
            {"id": "v-3", "name": "客户分群 (RFM)", "contentUrl": "CustomerProfile/RFM",
             "workbook": {"name": "客户画像"}, "usage": {"totalViewCount": 672}},
            {"id": "v-4", "name": "NPS 趋势", "contentUrl": "CustomerProfile/NPS",
             "workbook": {"name": "客户画像"}, "usage": {"totalViewCount": 541}},
            {"id": "v-5", "name": "实时运营概览", "contentUrl": "Ops/Overview",
             "workbook": {"name": "运营监控大屏"}, "usage": {"totalViewCount": 2103}},
        ]

    def list_datasources(self, force=False):
        return [
            {"id": "ds-1", "name": "销售订单数据", "type": "snowflake",
             "description": "来自 Snowflake ANALYTICS_DB.PUBLIC"},
            {"id": "ds-2", "name": "客户主数据", "type": "snowflake",
             "description": "来自 Snowflake ANALYTICS_DB.PUBLIC"},
        ]

    def get_datasource_fields(self, datasource_id, force=False):
        fields_map = {
            "ds-1": [
                {"name": "ORDER_ID", "dataType": "STRING", "description": "订单编号"},
                {"name": "ORDER_DATE", "dataType": "DATE", "description": "订单日期"},
                {"name": "MODEL", "dataType": "STRING", "description": "车型"},
                {"name": "CITY", "dataType": "STRING", "description": "城市"},
                {"name": "DEALER", "dataType": "STRING", "description": "经销商"},
                {"name": "FINAL_PRICE_YUAN", "dataType": "REAL", "description": "成交价(元)"},
                {"name": "DISCOUNT_YUAN", "dataType": "REAL", "description": "优惠金额(元)"},
                {"name": "CHANNEL", "dataType": "STRING", "description": "销售渠道"},
                {"name": "SALES_PERSON", "dataType": "STRING", "description": "销售顾问"},
            ],
            "ds-2": [
                {"name": "CUSTOMER_ID", "dataType": "STRING", "description": "客户ID"},
                {"name": "NAME", "dataType": "STRING", "description": "姓名"},
                {"name": "AGE", "dataType": "INTEGER", "description": "年龄"},
                {"name": "CITY", "dataType": "STRING", "description": "城市"},
                {"name": "OCCUPATION", "dataType": "STRING", "description": "职业"},
                {"name": "INCOME_LEVEL", "dataType": "STRING", "description": "收入水平"},
                {"name": "NPS_SCORE", "dataType": "REAL", "description": "NPS评分(1-10)"},
                {"name": "LOYALTY_TIER", "dataType": "STRING", "description": "忠诚度等级"},
                {"name": "LIFETIME_SPEND_YUAN", "dataType": "REAL", "description": "累计消费(元)"},
            ],
        }
        return fields_map.get(datasource_id, [])

    def query_view_data(self, view_id, max_rows=500, filters=None):
        """Return mock data for demo views."""
        import random
        mock_data = {
            "v-1": {  # 月度销售趋势
                "columns": ["月份", "销量", "营收_万元", "环比增长"],
                "rows": [
                    {"月份": f"2025-{m:02d}", "销量": random.randint(30,60),
                     "营收_万元": round(random.uniform(800,1500),1),
                     "环比增长": f"{random.uniform(-5,15):.1f}%"}
                    for m in range(1, 13)
                ],
            },
            "v-2": {  # 区域分布
                "columns": ["城市", "销量", "平均成交价", "市占率"],
                "rows": [
                    {"城市": c, "销量": random.randint(20,80),
                     "平均成交价": round(random.uniform(25,45),1),
                     "市占率": f"{random.uniform(5,25):.1f}%"}
                    for c in ["北京","上海","深圳","广州","成都","杭州","武汉","南京"]
                ],
            },
            "v-3": {  # RFM
                "columns": ["客户分群", "客户数", "平均消费_万元", "平均NPS"],
                "rows": [
                    {"客户分群": s, "客户数": random.randint(10,50),
                     "平均消费_万元": round(random.uniform(20,80),1),
                     "平均NPS": round(random.uniform(5,9.5),1)}
                    for s in ["高价值忠诚","高价值潜力","中等价值","低价值沉睡","新客户","流失风险"]
                ],
            },
            "v-4": {  # NPS趋势
                "columns": ["月份", "NPS均分", "推荐者占比", "贬损者占比", "净NPS"],
                "rows": [
                    {"月份": f"2025-{m:02d}", "NPS均分": round(random.uniform(6.5,8.5),1),
                     "推荐者占比": f"{random.uniform(30,55):.0f}%",
                     "贬损者占比": f"{random.uniform(8,25):.0f}%",
                     "净NPS": round(random.uniform(15,45),0)}
                    for m in range(1, 13)
                ],
            },
            "v-5": {  # 运营概览
                "columns": ["指标", "当前值", "目标", "达成率"],
                "rows": [
                    {"指标": "在线车辆", "当前值": "18/20", "目标": "20", "达成率": "90%"},
                    {"指标": "充电桩利用率", "当前值": "73%", "目标": "80%", "达成率": "91%"},
                    {"指标": "OTA成功率", "当前值": "81%", "目标": "95%", "达成率": "85%"},
                    {"指标": "24h工单完结率", "当前值": "88%", "目标": "90%", "达成率": "98%"},
                    {"指标": "App日活", "当前值": "156", "目标": "200", "达成率": "78%"},
                ],
            },
        }
        data = mock_data.get(view_id, {"columns": [], "rows": []})
        data["count"] = len(data.get("rows", []))
        data["view_id"] = view_id
        data["source"] = "Tableau (Mock)"
        return data

    def get_all_fields_metadata(self):
        return []  # skip GraphQL in mock

    def test_connection(self):
        return {
            "ok": True,
            "message": "Tableau Mock OK — 3 workbooks, 5 views, 2 datasources",
            "details": {"server": "demo-mode", "workbooks": 3, "views": 5, "datasources": 2, "mock": True}
        }


# ═══════ Singleton Factory ═══════

_client = None

def get_tableau_client() -> TableauClient:
    """Get or create the Tableau client (real or mock based on config)."""
    global _client
    if _client is not None:
        return _client

    server_url = os.getenv("TABLEAU_SERVER_URL", "")
    if server_url:
        _client = TableauClient()
        logger.info(f"Tableau client initialized: {server_url}")
    else:
        _client = TableauMockClient()
        logger.info("Tableau mock client initialized (TABLEAU_SERVER_URL not set)")

    return _client


def reset_client():
    """Reset the singleton (for runtime reconfiguration)."""
    global _client
    if _client:
        try:
            _client.signout()
        except Exception:
            pass
    _client = None
