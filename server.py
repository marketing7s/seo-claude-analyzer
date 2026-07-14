"""
SEO Claude Analyzer - MCP server
Connects Claude Chat to Google Search Console (GSC) and Google Analytics 4 (GA4).

AUTO-DISCOVERY MODE:
  This server can find every GSC site and GA4 property your service account
  has access to, all by itself. You do NOT need to type any IDs.
  When you add a new website later, you only grant the service account access
  in Search Console + Analytics; the server discovers it automatically.

You do NOT need to understand this code. Paste it into GitHub exactly as-is.
All private values live in Render's Environment tab, not here.
"""

import os
import json
import difflib
from datetime import date, timedelta

from fastmcp import FastMCP
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse
from google.oauth2 import service_account
from googleapiclient.discovery import build
from google.analytics.data_v1beta import BetaAnalyticsDataClient
from google.analytics.data_v1beta.types import (
    RunReportRequest,
    DateRange,
    Dimension,
    Metric,
)
from google.analytics.admin_v1beta import AnalyticsAdminServiceClient

# ---------------------------------------------------------------------------
# 1. LOAD YOUR GOOGLE LOGIN (the service account key)
# ---------------------------------------------------------------------------
SCOPES = [
    "https://www.googleapis.com/auth/webmasters.readonly",
    "https://www.googleapis.com/auth/analytics.readonly",
]

_creds_raw = os.environ.get("GOOGLE_CREDENTIALS", "")
if not _creds_raw:
    raise RuntimeError(
        "GOOGLE_CREDENTIALS environment variable is missing. "
        "Add it in Render > your service > Environment."
    )

_creds_info = json.loads(_creds_raw)
CREDENTIALS = service_account.Credentials.from_service_account_info(
    _creds_info, scopes=SCOPES
)

# ---------------------------------------------------------------------------
# 2. OPTIONAL NICKNAMES (you can leave this empty)
# ---------------------------------------------------------------------------
# In auto-discovery mode you don't need this. But if you ever want a short
# nickname, add it to the PROJECTS variable on Render, e.g.
# {"acme": {"gsc_site": "sc-domain:acme.com", "ga4_property": "123456789"}}
_projects_raw = os.environ.get("PROJECTS", "{}")
PROJECTS = json.loads(_projects_raw)


def _default_dates(start, end):
    """Default to the last ~month, ending 3 days ago (GSC data lags a bit)."""
    if not end:
        end = (date.today() - timedelta(days=3)).isoformat()
    if not start:
        start = (date.today() - timedelta(days=31)).isoformat()
    return start, end


# ---------------------------------------------------------------------------
# DISCOVERY HELPERS
# ---------------------------------------------------------------------------
def _list_gsc_sites():
    """Return every GSC site (siteUrl) the service account can read."""
    service = build("searchconsole", "v1", credentials=CREDENTIALS)
    resp = service.sites().list().execute()
    out = []
    for entry in resp.get("siteEntry", []):
        level = entry.get("permissionLevel", "")
        if level == "siteUnverifiedUser":
            continue  # no data access
        out.append(entry["siteUrl"])
    return out


def _list_ga4_properties():
    """Return list of {property_id, display_name, account} for GA4."""
    client = AnalyticsAdminServiceClient(credentials=CREDENTIALS)
    out = []
    for summary in client.list_account_summaries():
        account_name = summary.display_name
        for prop in summary.property_summaries:
            # prop.property looks like "properties/123456789"
            pid = prop.property.split("/")[-1]
            out.append(
                {
                    "property_id": pid,
                    "display_name": prop.display_name,
                    "account": account_name,
                }
            )
    return out


def _resolve_gsc_site(name: str) -> str:
    """
    Turn whatever the user said into a real GSC siteUrl.
    Accepts: an exact siteUrl, a PROJECTS nickname, a bare domain,
    or a close/partial match to a discovered site.
    """
    # 1. Exact siteUrl already
    if name.startswith("sc-domain:") or name.startswith("http"):
        return name
    # 2. A nickname in PROJECTS
    for key, cfg in PROJECTS.items():
        if key.lower() == name.lower() and cfg.get("gsc_site"):
            return cfg["gsc_site"]
    # 3. Match against discovered sites
    sites = _list_gsc_sites()
    low = name.lower().strip()
    for s in sites:
        if low in s.lower():
            return s
    # fuzzy fallback
    match = difflib.get_close_matches(low, [s.lower() for s in sites], n=1, cutoff=0.5)
    if match:
        for s in sites:
            if s.lower() == match[0]:
                return s
    raise ValueError(
        f"Could not find a Search Console site matching '{name}'. "
        f"Available sites: {', '.join(sites) or '(none — check service account access)'}"
    )


def _resolve_ga4_property(name: str) -> str:
    """
    Turn whatever the user said into a GA4 property ID (digits).
    Accepts: a numeric ID, a PROJECTS nickname, or a display-name match.
    """
    # 1. Already a numeric ID
    if name.isdigit():
        return name
    # 2. A nickname in PROJECTS
    for key, cfg in PROJECTS.items():
        if key.lower() == name.lower() and cfg.get("ga4_property"):
            return str(cfg["ga4_property"])
    # 3. Match against discovered property display names
    props = _list_ga4_properties()
    low = name.lower().strip()
    for p in props:
        if low in p["display_name"].lower():
            return p["property_id"]
    match = difflib.get_close_matches(
        low, [p["display_name"].lower() for p in props], n=1, cutoff=0.5
    )
    if match:
        for p in props:
            if p["display_name"].lower() == match[0]:
                return p["property_id"]
    raise ValueError(
        f"Could not find a GA4 property matching '{name}'. "
        f"Available: {', '.join(p['display_name'] + ' (' + p['property_id'] + ')' for p in props) or '(none — check service account access)'}"
    )


# ---------------------------------------------------------------------------
# 3. THE MCP SERVER AND ITS TOOLS
# ---------------------------------------------------------------------------
CONNECTOR_SECRET = os.environ.get("CONNECTOR_SECRET", "")


class SecretAuthMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request, call_next):
        if request.url.path in ("/", "/health"):
            return await call_next(request)
        if CONNECTOR_SECRET:
            sent = request.headers.get("x-connector-secret", "")
            if sent != CONNECTOR_SECRET:
                return JSONResponse({"error": "unauthorized"}, status_code=401)
        return await call_next(request)


mcp = FastMCP("SEO Claude Analyzer")


@mcp.tool()
def list_sites() -> str:
    """
    List every website (project) available to analyze. Shows the Search
    Console sites and the GA4 properties your service account can read.
    Use this first to see the exact names you can use in other tools.
    """
    lines = []
    try:
        gsc = _list_gsc_sites()
        lines.append("Search Console sites:")
        lines.extend(f"  - {s}" for s in gsc) or lines.append("  (none)")
    except Exception as e:
        lines.append(f"Search Console error: {e}")

    lines.append("")
    try:
        ga = _list_ga4_properties()
        lines.append("GA4 properties (name -> id):")
        if ga:
            lines.extend(
                f"  - {p['display_name']} -> {p['property_id']}  [account: {p['account']}]"
                for p in ga
            )
        else:
            lines.append("  (none)")
    except Exception as e:
        lines.append(f"GA4 error: {e}")

    if PROJECTS:
        lines.append("")
        lines.append("Configured nicknames:")
        lines.extend(f"  - {k}" for k in PROJECTS)

    return "\n".join(lines)


@mcp.tool()
def gsc_query(
    site: str,
    start_date: str = "",
    end_date: str = "",
    dimension: str = "query",
    row_limit: int = 25,
) -> str:
    """
    Get Google Search Console data for one website.

    site:       the site to query. You can pass the exact Search Console
                identifier (e.g. "sc-domain:acme.com" or "https://acme.com/"),
                a bare domain ("acme.com"), or a nickname. Run list_sites first
                if unsure.
    start_date: YYYY-MM-DD (optional; defaults to 31 days ago)
    end_date:   YYYY-MM-DD (optional; defaults to 3 days ago)
    dimension:  break the data down by: "query", "page", "country",
                "device", or "date"
    row_limit:  how many rows to return (default 25, max 1000)
    """
    site_url = _resolve_gsc_site(site)
    start_date, end_date = _default_dates(start_date or None, end_date or None)

    service = build("searchconsole", "v1", credentials=CREDENTIALS)
    body = {
        "startDate": start_date,
        "endDate": end_date,
        "dimensions": [dimension],
        "rowLimit": min(int(row_limit), 1000),
    }
    resp = service.searchanalytics().query(siteUrl=site_url, body=body).execute()
    rows = resp.get("rows", [])
    if not rows:
        return f"No GSC data for '{site_url}' between {start_date} and {end_date}."

    out = [f"GSC data for '{site_url}' ({start_date} to {end_date}), by {dimension}:"]
    out.append(f"{dimension} | clicks | impressions | ctr | position")
    for r in rows:
        key = r["keys"][0]
        out.append(
            f"{key} | {r.get('clicks', 0):.0f} | {r.get('impressions', 0):.0f} | "
            f"{r.get('ctr', 0) * 100:.2f}% | {r.get('position', 0):.1f}"
        )
    return "\n".join(out)


@mcp.tool()
def ga4_query(
    property: str,
    start_date: str = "",
    end_date: str = "",
    dimension: str = "sessionDefaultChannelGroup",
    metric: str = "sessions",
    row_limit: int = 25,
) -> str:
    """
    Get Google Analytics 4 data for one website.

    property:   the GA4 property to query. You can pass the numeric property ID
                (e.g. "123456789"), the property's display name as shown in
                Analytics (e.g. "Acme - GA4"), or a nickname. Run list_sites
                first if unsure.
    start_date: YYYY-MM-DD (optional; defaults to 31 days ago)
    end_date:   YYYY-MM-DD (optional; defaults to 3 days ago)
    dimension:  GA4 dimension. Common: "sessionDefaultChannelGroup",
                "pagePath", "country", "deviceCategory", "date",
                "sessionSource", "landingPage"
    metric:     GA4 metric. Common: "sessions", "totalUsers",
                "screenPageViews", "engagementRate",
                "averageSessionDuration", "conversions", "bounceRate"
    row_limit:  how many rows to return (default 25)
    """
    property_id = _resolve_ga4_property(property)
    start_date, end_date = _default_dates(start_date or None, end_date or None)

    client = BetaAnalyticsDataClient(credentials=CREDENTIALS)
    request = RunReportRequest(
        property=f"properties/{property_id}",
        date_ranges=[DateRange(start_date=start_date, end_date=end_date)],
        dimensions=[Dimension(name=dimension)],
        metrics=[Metric(name=metric)],
        limit=int(row_limit),
    )
    resp = client.run_report(request)
    if not resp.rows:
        return f"No GA4 data for property {property_id} between {start_date} and {end_date}."

    out = [f"GA4 data for property {property_id} ({start_date} to {end_date}):"]
    out.append(f"{dimension} | {metric}")
    for row in resp.rows:
        dim_val = row.dimension_values[0].value
        met_val = row.metric_values[0].value
        out.append(f"{dim_val} | {met_val}")
    return "\n".join(out)


# ---------------------------------------------------------------------------
# 4. START THE SERVER
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    import uvicorn

    port = int(os.environ.get("PORT", "8000"))
    app = mcp.http_app(path="/mcp")
    app.add_middleware(SecretAuthMiddleware)
    uvicorn.run(app, host="0.0.0.0", port=port)
