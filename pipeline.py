"""
Clay Enrichment Pipeline — hot-reloadable implementation module.

All tool logic lives here. server.py calls importlib.reload(pipeline) before
each tool invocation so changes here take effect immediately without restarting
Claude Desktop.

To add/rename a tool: edit server.py (requires Claude Desktop restart, rare).
To change tool logic:  edit this file (takes effect on next tool call, no restart).
"""

import json
import re
import sys
import time
import tempfile
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

from clay_client import ClayClient
import _state

load_dotenv(Path(__file__).parent / ".env")

# ============================================================================
# CONFIG
# ============================================================================

CONFIG_PATH = Path.home() / ".clay-mcp" / "profiles.json"
_FALLBACK = Path(__file__).parent / "profiles.json"


def _load_config() -> dict:
    path = CONFIG_PATH if CONFIG_PATH.exists() else _FALLBACK
    if not path.exists():
        raise RuntimeError(
            f"No profiles.json found. Copy profiles.json.template to {CONFIG_PATH} and fill it in."
        )
    return json.loads(path.read_text())


def _get_profile(profile: str) -> dict:
    config = _load_config()
    if profile not in config:
        available = [k for k in config if not k.startswith("_")]
        raise ValueError(f"Profile '{profile}' not found. Available: {available}")
    return config[profile]


def _get_clay(profile: str) -> ClayClient:
    prof = _get_profile(profile)
    cookie = prof.get("clay_cookie", "")
    email = prof.get("clay_email", "")
    password = prof.get("clay_password", "")
    if not cookie and not (email and password):
        raise RuntimeError(
            f"Profile '{profile}': set clay_cookie (from browser DevTools) "
            "or clay_email + clay_password in profiles.json"
        )
    return ClayClient(profile_name=profile, cookie=cookie, email=email, password=password)


# ============================================================================
# SFDC CLIENT (lazy, per-profile — cache lives in _state to survive reloads)
# ============================================================================

def _get_sf(profile: str = ""):
    """
    Return a Salesforce client for the given profile.
    Credentials are read from profiles.json first (sf_username, sf_password,
    sf_security_token), falling back to SF_USERNAME / SF_PASSWORD /
    SF_SECURITY_TOKEN environment variables for backwards compatibility.
    """
    cache_key = profile or "_default"
    if cache_key in _state.sf_clients:
        return _state.sf_clients[cache_key]

    import os
    from simple_salesforce import Salesforce as SimpleSalesforce

    username = password = token = ""
    if profile:
        try:
            prof = _get_profile(profile)
            username = prof.get("sf_username", "")
            password = prof.get("sf_password", "")
            token = prof.get("sf_security_token", "")
        except Exception:
            pass

    if not username:
        username = os.environ.get("SF_USERNAME", "")
    if not password:
        password = os.environ.get("SF_PASSWORD", "")
    if not token:
        token = os.environ.get("SF_SECURITY_TOKEN", "")

    if not (username and password):
        raise RuntimeError(
            f"Salesforce credentials missing for profile '{profile}'. "
            "Add sf_username / sf_password / sf_security_token to profiles.json, "
            "or set SF_USERNAME / SF_PASSWORD / SF_SECURITY_TOKEN in .env"
        )

    class _SF:
        def __init__(self):
            self._username = username
            self._password = password
            self._token = token
            self._sf = SimpleSalesforce(
                username=self._username,
                password=self._password,
                security_token=self._token,
            )

        def _reconnect(self):
            self._sf = SimpleSalesforce(
                username=self._username,
                password=self._password,
                security_token=self._token,
            )

        def query(self, soql: str) -> list[dict]:
            try:
                return self._sf.query_all(soql)["records"]
            except Exception as e:
                if "INVALID_SESSION_ID" in str(e):
                    self._reconnect()
                    return self._sf.query_all(soql)["records"]
                raise

        def bulk_update(self, sobject: str, records: list[dict], retries: int = 3) -> list:
            import time as _time
            for attempt in range(retries):
                try:
                    sf_obj = getattr(self._sf.bulk, sobject)
                    return sf_obj.update(records, batch_size=200)
                except Exception as e:
                    if attempt < retries - 1:
                        wait = 10 * (attempt + 1)
                        print(f"    [bulk_update] attempt {attempt+1} failed: {e}. Retrying in {wait}s...", flush=True)
                        _time.sleep(wait)
                        self._reconnect()
                    else:
                        raise

    _state.sf_clients[cache_key] = _SF()
    return _state.sf_clients[cache_key]


# ============================================================================
# EXPORT STORAGE (local /tmp or Supabase Storage)
# ============================================================================

def _save_export(content_str: str, filename: str, profile: str) -> dict:
    """
    Persist an export. Returns {"url": "..."} (a time-limited signed URL) if
    Supabase Storage is configured for the profile, otherwise
    {"filePath": "/tmp/clay-exports/..."}.

    The bucket is PRIVATE — files are uploaded with the service_role key and
    accessed via signed URLs that expire after supabase_signed_url_ttl seconds
    (default: 7 days / 604800s).

    Supabase profile fields:
      supabase_url              — Project URL (e.g. https://xyz.supabase.co)
      supabase_service_key      — service_role key (Settings → API)
      supabase_bucket           — bucket name (must exist, can be private)
      supabase_signed_url_ttl   — optional, seconds (default: 604800 = 7 days)

    Setup:
      1. Supabase dashboard → Storage → New bucket (leave Public OFF)
      2. Project Settings → API → copy Project URL + service_role key
    """
    try:
        prof = _get_profile(profile)
        sb_url = (prof.get("supabase_url") or "").rstrip("/")
        sb_key = prof.get("supabase_service_key", "")
        sb_bucket = prof.get("supabase_bucket", "")
        sb_ttl = int(prof.get("supabase_signed_url_ttl", 604800))  # default 7 days

        if all([sb_url, sb_key, sb_bucket]):
            import requests as _requests
            auth_headers = {"Authorization": f"Bearer {sb_key}"}

            # 1. Upload file to private bucket
            content_type = "text/markdown; charset=utf-8" if filename.endswith(".md") else "application/json"
            upload_url = f"{sb_url}/storage/v1/object/{sb_bucket}/{filename}"
            resp = _requests.post(
                upload_url,
                headers={**auth_headers, "Content-Type": content_type, "x-upsert": "true"},
                data=content_str.encode("utf-8"),
                timeout=30,
            )
            resp.raise_for_status()

            # 2. Generate a signed URL (time-limited, no auth needed to open)
            sign_url = f"{sb_url}/storage/v1/object/sign/{sb_bucket}/{filename}"
            sign_resp = _requests.post(
                sign_url,
                headers={**auth_headers, "Content-Type": "application/json"},
                json={"expiresIn": sb_ttl},
                timeout=10,
            )
            sign_resp.raise_for_status()
            signed_token = sign_resp.json().get("signedURL", "")
            # signedURL is a relative path like /object/sign/bucket/file?token=...
            full_signed_url = f"{sb_url}/storage/v1{signed_token}" if signed_token.startswith("/") else signed_token

            return {"url": full_signed_url, "expiresIn": sb_ttl}
    except Exception as e:
        print(f"[Clay] Supabase Storage upload failed, falling back to /tmp: {e}", file=sys.stderr)

    export_dir = Path(tempfile.gettempdir()) / "clay-exports"
    export_dir.mkdir(parents=True, exist_ok=True)
    (export_dir / filename).write_text(content_str)
    return {"filePath": str(export_dir / filename)}


# ============================================================================
# HELPERS
# ============================================================================

def _fmt_schema(schema: dict) -> str:
    lines = [
        f"Table: {schema['table_name']} ({schema['table_id']})",
        f"First view ID: {schema['first_view_id']}",
        "",
        "Views:",
    ]
    for v in schema["views"]:
        lines.append(f"  {v['id']}  {v['name']}")
    lines += ["", "Fields (use field id in profiles.json):"]
    for f in schema["fields"]:
        lines.append(f"  {f['id']:30s}  {f['name']:40s}  type={f['type']}")
    return "\n".join(lines)


def _is_enrichment_terminal(cell_data: Any) -> bool:
    """Return True when an enrichment cell is no longer in-flight (done or errored)."""
    if not isinstance(cell_data, dict):
        return False
    status = (cell_data.get("metadata") or {}).get("status") or ""
    return status in ("SUCCESS", "SUCCESS_NO_DATA") or status.startswith("ERROR_")


def _get_enrichment_fullvalue(cell_data: Any) -> dict | None:
    """Extract fullValue from an enrichment action cell's externalContent."""
    if not isinstance(cell_data, dict):
        return None
    return (cell_data.get("externalContent") or {}).get("fullValue") or None


def _normalize_domain(raw: str) -> str:
    """Normalize a domain or URL to bare lowercase domain for comparison."""
    if not raw:
        return ""
    s = raw.strip().lower()
    s = re.sub(r'^https?://', '', s)
    s = re.sub(r'^www\.', '', s)
    s = s.split('/')[0].split('?')[0].split('#')[0].rstrip('.')
    return s


def _is_stale(
    account_name: str,
    account_domain: str,
    clay_company: str,
    clay_domain_raw: str,
) -> tuple[bool, str]:
    """
    Compare Clay's LinkedIn current employer to a Contact's SFDC Account.

    Returns (is_stale: bool, reason: str).
    Reason is one of: domain_match, domain_mismatch, name_match,
                      name_mismatch, no_clay_data.
    A contact with no Clay data is NOT marked stale (conservative — avoids
    false positives when Clay simply has no record for the person).
    """
    if not clay_company and not clay_domain_raw:
        return False, "no_clay_data"

    norm_acct = _normalize_domain(account_domain)
    norm_clay = _normalize_domain(clay_domain_raw)

    if norm_acct and norm_clay:
        if norm_acct == norm_clay:
            return False, "domain_match"
        return True, "domain_mismatch"

    if account_name and clay_company:
        a = account_name.strip().lower()
        c = clay_company.strip().lower()
        stopwords = {'inc', 'llc', 'ltd', 'corp', 'co', 'the', 'and', 'of'}
        if a == c or a in c or c in a:
            return False, "name_match"
        a_words = set(re.sub(r'[^a-z0-9 ]', '', a).split()) - stopwords
        c_words = set(re.sub(r'[^a-z0-9 ]', '', c).split())
        if a_words and len(a_words & c_words) / len(a_words) >= 0.5:
            return False, "name_match"
        return True, "name_mismatch"

    return False, "no_clay_data"


def _poll_for_enrichment(
    clay: ClayClient,
    table_id: str,
    view_id: str,
    domain_field_id: str,
    domain_to_sfdc_id: dict[str, str],
    enrich_field_id: str,
    poll_interval: int = 5,
    timeout: int = 90,
) -> list[dict]:
    """
    Poll Clay rows until enrichment is terminal for all submitted domains.
    Matches rows by domain value (local dict). Returns done rows.
    """
    pending_domains = set(domain_to_sfdc_id.keys())
    deadline = time.time() + timeout
    print(f"[Clay] Polling for {len(pending_domains)} records (timeout {timeout}s)...", file=sys.stderr)

    while time.time() < deadline:
        try:
            rows = clay.get_all_rows(table_id, view_id)
        except Exception as e:
            print(f"[Clay] Poll error (retrying): {e}", file=sys.stderr)
            time.sleep(poll_interval)
            continue

        done_rows = []
        for row in rows:
            cells = row.get("cells") or row.get("fields") or {}
            domain_val = clay.extract_cell_value(cells.get(domain_field_id))
            if domain_val not in pending_domains:
                continue
            if _is_enrichment_terminal(cells.get(enrich_field_id)):
                done_rows.append(row)

        print(f"[Clay] {len(done_rows)}/{len(pending_domains)} done", file=sys.stderr)
        if len(done_rows) >= len(pending_domains):
            return done_rows

        time.sleep(poll_interval)

    # Timeout — return whatever is available
    rows = clay.get_all_rows(table_id, view_id)
    return [
        row for row in rows
        if clay.extract_cell_value(
            (row.get("cells") or row.get("fields") or {}).get(domain_field_id)
        ) in pending_domains
    ]


def _poll_by_record_ids(
    clay: ClayClient,
    table_id: str,
    record_ids: list[str],
    enrich_field_id: str,
    timeout: int | None = None,
) -> dict[str, dict]:
    """
    Poll Clay records by ID until enrichment is terminal. Returns {rid: full_record}.
    Avoids get_all_rows scans — fetches each record individually.
    Timeout defaults to max(300, n * 8) seconds.
    """
    if timeout is None:
        timeout = max(300, len(record_ids) * 8)
    deadline = time.time() + timeout
    pending_ids = set(record_ids)
    enriched: dict[str, dict] = {}

    print(f"[Clay] Polling {len(pending_ids)} records by ID (timeout {timeout}s)...", file=sys.stderr)
    while time.time() < deadline and pending_ids:
        done_ids: set[str] = set()
        for rid in list(pending_ids):
            try:
                full_record = clay.get_record(table_id, rid)
                full_cells = full_record.get("cells") or {}
                if _is_enrichment_terminal(full_cells.get(enrich_field_id)):
                    enriched[rid] = full_record
                    done_ids.add(rid)
            except Exception:
                pass
        pending_ids -= done_ids
        if not pending_ids:
            print(f"[Clay] All {len(enriched)} records enriched.", file=sys.stderr)
            break
        print(f"[Clay] {len(enriched)}/{len(record_ids)} done, sleeping...", file=sys.stderr)
        time.sleep(5)

    if pending_ids:
        print(f"[Clay] Timeout: {len(pending_ids)} records still pending.", file=sys.stderr)
    return enriched


def _extract_record_ids(add_rows_results: list[dict]) -> list[str]:
    """Extract Clay record IDs from the list of add_rows() batch responses."""
    ids = []
    for result in add_rows_results:
        for rec in (result.get("records") or []):
            rid = rec.get("id") or ""
            if rid:
                ids.append(rid)
    return ids


# ============================================================================
# DISCOVERY TOOLS
# ============================================================================

def clay_list_profiles() -> str:
    """List configured Clay profiles and their setup status."""
    try:
        config = _load_config()
    except RuntimeError as e:
        return str(e)

    lines = []
    for name, prof in config.items():
        if name.startswith("_"):
            continue
        has_cookie = bool(prof.get("clay_cookie"))
        has_creds = bool(prof.get("clay_email") and prof.get("clay_password"))
        acct_table = prof.get("account_table_id", "")
        cont_table = prof.get("contact_table_id", "")
        has_supabase = all(prof.get(k) for k in ("supabase_url", "supabase_service_key", "supabase_bucket"))
        lines += [
            f"Profile: {name}",
            f"  credentials:     {'✓ cookie' if has_cookie else ('✓ email+password' if has_creds else '✗ missing')}",
            f"  account_table:   {acct_table or '✗ not set'}",
            f"  contact_table:   {cont_table or '✗ not set'}",
            f"  clay_workspace:  {prof.get('clay_workspace_id', '✗ not set')}",
            f"  storage:         {'✓ Supabase (' + prof.get('supabase_bucket','') + ')' if has_supabase else '✗ not set (exports go to /tmp)'}",
        ]
    return "\n".join(lines) if lines else "No profiles configured."


def clay_list_workspaces(profile: str = "myorg") -> str:
    """List all Clay workspaces accessible by this profile."""
    clay = _get_clay(profile)
    result = clay.list_workspaces()
    workspaces = result.get("results") or result.get("workspaces") or (result if isinstance(result, list) else [result])
    lines = [f"Workspaces for profile '{profile}':"]
    for ws in workspaces:
        lines.append(f"  id={ws.get('id')}  name={ws.get('name')}  plan={ws.get('billingPlanType', '')}")
    return "\n".join(lines)


def clay_list_tables(profile: str = "myorg", workspace_id: str = "0", workbook_id: str = "") -> str:
    """List tables and resources in a Clay workspace or workbook."""
    prof = _get_profile(profile)
    ws_id = int(workspace_id) if workspace_id and workspace_id != "0" else prof.get("clay_workspace_id")
    if not ws_id:
        return "workspace_id required (or set clay_workspace_id in profiles.json)"

    clay = _get_clay(profile)
    result = clay.list_resources(int(ws_id), parent_id=workbook_id)

    resources = result.get("resources") or (result if isinstance(result, list) else [])
    label = f"workbook {workbook_id}" if workbook_id else f"workspace {ws_id}"
    lines = [f"Resources in {label}:"]
    for r in resources:
        rtype = r.get("resourceType") or r.get("type", "")
        rid = r.get("id", "")
        rname = r.get("name", "")
        lines.append(f"  [{rtype:8s}]  {rid:35s}  {rname}")
    return "\n".join(lines)


def clay_get_table_schema(table_id: str, profile: str = "myorg") -> str:
    """Get full schema for a Clay table: field names, field IDs, types, and view IDs."""
    clay = _get_clay(profile)
    schema = clay.extract_schema(table_id)
    return _fmt_schema(schema)


# ============================================================================
# CORE API TOOLS
# ============================================================================

def clay_add_row(table_id: str, cells: dict, profile: str = "myorg") -> str:
    """Add a single row to a Clay table."""
    clay = _get_clay(profile)
    result = clay.add_row(table_id, cells)
    return f"Row added to {table_id}.\n{json.dumps(result, indent=2)}"


def clay_run_enrichment(
    table_id: str,
    view_id: str,
    field_ids: list[str],
    num_records: int = 0,
    profile: str = "myorg",
) -> str:
    """Trigger enrichment columns on rows in a Clay table view."""
    clay = _get_clay(profile)
    result = clay.run_enrichment(table_id, view_id, field_ids, num_records=num_records or None)
    return f"Enrichment triggered on {len(field_ids)} column(s).\n{json.dumps(result, indent=2)}"


def clay_get_rows(
    table_id: str,
    view_id: str,
    columns: list[str] | None = None,
    max_rows: int = 20,
    profile: str = "myorg",
) -> str:
    """Read rows from a Clay table view with column names resolved."""
    clay = _get_clay(profile)
    schema = clay.extract_schema(table_id)
    field_id_to_name = {f["id"]: f["name"] for f in schema["fields"]}
    filter_names = set(columns) if columns else None

    rows = clay.get_all_rows(table_id, view_id, max_rows=max_rows)

    results = []
    for row in rows:
        cells = row.get("cells") or row.get("fields") or {}
        record: dict = {"_record_id": row.get("id", "")}
        for field_id, cell_data in cells.items():
            col_name = field_id_to_name.get(field_id, field_id)
            if filter_names and col_name not in filter_names:
                continue
            record[col_name] = clay.extract_cell_value(cell_data)
        results.append(record)

    return json.dumps(results, indent=2)


def clay_export_table_data(
    table_id: str,
    view_id: str = "",
    max_rows: int = 0,
    columns: list[str] | None = None,
    profile: str = "myorg",
) -> str:
    """
    Export all rows from a Clay table to JSON. Uploads to R2 if configured,
    otherwise writes to /tmp. Returns summary + 3 sample rows.
    """
    clay = _get_clay(profile)
    schema = clay.extract_schema(table_id)
    field_id_to_name = {f["id"]: f["name"] for f in schema["fields"]}
    all_col_names = [f["name"] for f in schema["fields"]]

    resolved_view_id = view_id or schema.get("first_view_id") or ""
    if not resolved_view_id:
        return f"No view_id provided and couldn't detect first view for table {table_id}"

    filter_set = set(columns) if columns else None
    col_names = [c for c in all_col_names if not filter_set or c in filter_set]

    rows = clay.get_all_rows(table_id, resolved_view_id, max_rows=max_rows)
    all_rows = []
    for row in rows:
        cells = row.get("cells") or row.get("fields") or {}
        record: dict = {"_record_id": row.get("id", "")}
        for field_id, cell_data in cells.items():
            col_name = field_id_to_name.get(field_id, field_id)
            if filter_set and col_name not in filter_set:
                continue
            record[col_name] = clay.extract_cell_value(cell_data)
        all_rows.append(record)

    output = {
        "tableId": table_id,
        "tableName": schema["table_name"],
        "exportedAt": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "rowCount": len(all_rows),
        "columns": col_names,
        "rows": all_rows,
    }
    json_str = json.dumps(output, indent=2)

    filename = f"clay-export-{table_id}-{int(time.time())}.json"
    save_result = _save_export(json_str, filename, profile)
    location_key = "url" if "url" in save_result else "filePath"
    location_val = save_result[location_key]

    return json.dumps({
        location_key: location_val,
        "tableName": schema["table_name"],
        "tableId": table_id,
        "rowCount": len(all_rows),
        "columnCount": len(col_names),
        "columns": col_names,
        "fileSizeKB": round(len(json_str) / 1024),
        "sampleRows": all_rows[:3],
        "note": (
            f"Full data at {location_val}. "
            + ("Share URL directly." if "url" in save_result else "Use Read tool to inspect.")
        ),
    }, indent=2)


def clay_delete_rows(table_id: str, record_ids: list[str], profile: str = "myorg") -> str:
    """Delete specific rows from a Clay table by their record IDs."""
    clay = _get_clay(profile)
    result = clay.delete_rows(table_id, record_ids)
    return f"Deleted {len(record_ids)} rows from {table_id}.\n{json.dumps(result, indent=2)}"


def clay_delete_all_rows(table_id: str, view_id: str, profile: str = "myorg") -> str:
    """Delete ALL rows visible in a Clay table view."""
    clay = _get_clay(profile)
    # Fetch+delete in pages to avoid loading all IDs into memory (large tables hang)
    deleted = 0
    while True:
        rows = clay.get_rows(table_id, view_id, limit=100, offset=0)
        if not rows:
            break
        record_ids = [row.get("id") or row.get("_record_id") or "" for row in rows]
        record_ids = [rid for rid in record_ids if rid]
        if not record_ids:
            break
        clay.delete_rows(table_id, record_ids)
        deleted += len(record_ids)
        if len(rows) < 100:
            break
    return f"Deleted {deleted} rows from view {view_id} in table {table_id}."


# ============================================================================
# TABLE DOCUMENTATION
# ============================================================================

def clay_document_table(
    table_id: str,
    profile: str = "myorg",
    sample_rows: int = 3,
) -> str:
    """
    Generate structured Markdown documentation for a Clay table.
    Fetches full schema (including hidden columns), sample enriched rows,
    and externalContent.fullValue structure for enrichment action columns.
    Uploads to R2 if configured, otherwise writes to /tmp.
    """
    clay = _get_clay(profile)
    schema = clay.extract_rich_schema(table_id)

    table_name = schema["table_name"]
    views = schema["views"]
    fields = schema["fields"]
    first_view_id = schema["first_view_id"]
    field_id_to_name = {f["id"]: f["name"] for f in fields}

    hidden_count = sum(1 for f in fields if f.get("_hidden"))

    try:
        count_data = clay.count_rows(table_id)
        row_count = (
            count_data.get("tableTotalRecordsCount")
            or count_data.get("count")
            or count_data.get("total")
            or "unknown"
        )
    except Exception:
        row_count = "unknown"

    # Fetch sample rows + enrichment fullValues
    sample: list[dict] = []
    enrich_fullvalues: dict[str, dict] = {}  # field_id -> merged fullValue keys

    if first_view_id:
        raw_rows = clay.get_rows(table_id, first_view_id, limit=sample_rows)
        # Identify enrichment-type columns (check a few known type strings)
        enrich_types = {"enrichmentaction", "enrichment_action", "action", "enrichment"}
        enrich_field_ids = [
            f["id"] for f in fields
            if (f.get("type") or "").lower() in enrich_types
        ]

        for row in raw_rows:
            record_id = row.get("id") or ""
            cells = row.get("cells") or {}

            if record_id and enrich_field_ids:
                try:
                    full_record = clay.get_record(table_id, record_id)
                    full_cells = full_record.get("cells") or {}
                    for fid in enrich_field_ids:
                        fv = _get_enrichment_fullvalue(full_cells.get(fid))
                        if fv:
                            enrich_fullvalues.setdefault(fid, {}).update(fv)
                except Exception:
                    pass

            row_data: dict = {}
            for fid, cell_data in cells.items():
                col_name = field_id_to_name.get(fid, fid)
                val = clay.extract_cell_value(cell_data)
                if val is not None:
                    row_data[col_name] = val
            if row_data:
                sample.append(row_data)

    # Build Markdown
    lines: list[str] = [
        f"# Clay Table: {table_name}",
        "",
        f"**Table ID:** `{table_id}`  ",
        f"**Documented at:** {time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())}  ",
        f"**Total columns:** {len(fields)} ({hidden_count} hidden)  ",
        f"**Row count:** {row_count}",
        "",
        "---",
        "",
        "## Views",
        "",
        "| ID | Name |",
        "|----|------|",
    ]
    for v in views:
        lines.append(f"| `{v['id']}` | {v.get('name', '')} |")

    lines += [
        "",
        "---",
        "",
        "## Schema",
    ]
    if hidden_count:
        lines.append(
            f"\n> ⚠️ **{hidden_count} hidden column(s) detected.** "
            "These are not visible in the default Clay UI view but are present in the table and included below. "
            "Note: Clay may store visibility per-view — treat this as best-effort until confirmed with a live hidden-column test.\n"
        )
    lines += [
        "",
        "| Field ID | Name | Type | Hidden |",
        "|----------|------|------|--------|",
    ]
    for f in fields:
        hidden_str = "Yes" if f.get("_hidden") else "No"
        lines.append(f"| `{f['id']}` | {f.get('name', '')} | {f.get('type', '')} | {hidden_str} |")

    # Relationship detection — scan raw field data for cross-table links
    relation_type_keywords = {"relation", "reference", "link", "lookup"}
    relation_key_candidates = ("linkedTableId", "relatedTableId", "sourceTableId", "targetTableId")
    related_fields = []
    for f in fields:
        ftype = (f.get("type") or "").lower()
        linked_table = next((f.get(k) for k in relation_key_candidates if f.get(k)), None)
        if linked_table or any(kw in ftype for kw in relation_type_keywords):
            related_fields.append({
                "name": f.get("name", ""),
                "id": f.get("id", ""),
                "type": f.get("type", ""),
                "linked_table": linked_table or "unknown",
            })

    if related_fields:
        lines += [
            "",
            "---",
            "",
            "## Relationships",
            "",
            "Cross-table links detected in this table's schema:",
            "",
            "| Field | Field ID | Type | Linked Table ID |",
            "|-------|----------|------|-----------------|",
        ]
        for rf in related_fields:
            lines.append(f"| {rf['name']} | `{rf['id']}` | {rf['type']} | `{rf['linked_table']}` |")
        lines += [
            "",
            "> To document a linked table, run `clay_document_table` with its table ID.",
            "> To document all tables in a workbook, run `clay_list_tables(workbook_id=...)` then call `clay_document_table` for each.",
        ]

    if enrich_fullvalues:
        lines += ["", "---", "", "## Enrichment Columns — Full Value Structure"]
        for fid, fv in enrich_fullvalues.items():
            fname = field_id_to_name.get(fid, fid)
            fv_str = json.dumps(fv, indent=2)
            if len(fv_str) > 3000:
                fv_str = fv_str[:3000] + "\n  ... (truncated)"
            lines += [
                "",
                f"### {fname} (`{fid}`)",
                "",
                "Sample `externalContent.fullValue` keys (merged from sample rows):",
                "",
                "```json",
                fv_str,
                "```",
            ]

    if sample:
        lines += ["", "---", "", f"## Sample Rows ({len(sample)} rows)"]
        for i, row_data in enumerate(sample, 1):
            lines += ["", f"### Row {i}", "", "| Column | Value |", "|--------|-------|"]
            for col, val in list(row_data.items())[:25]:
                val_str = str(val)[:120].replace("|", "\\|").replace("\n", " ")
                lines.append(f"| {col} | {val_str} |")

    md_str = "\n".join(lines)
    filename = f"clay-doc-{table_id}-{int(time.time())}.md"
    save_result = _save_export(md_str, filename, profile)
    location_key = "url" if "url" in save_result else "filePath"
    location_val = save_result[location_key]

    return json.dumps({
        location_key: location_val,
        "tableName": table_name,
        "tableId": table_id,
        "columnCount": len(fields),
        "hiddenColumns": hidden_count,
        "rowCount": row_count,
        "enrichmentColumnsDocumented": len(enrich_fullvalues),
        "note": (
            f"Documentation at {location_val}. "
            + ("Share URL for client calls." if "url" in save_result else "Use Read tool to view.")
        ),
    }, indent=2)


# ============================================================================
# SFDC PIPELINE TOOLS
# ============================================================================

def clay_enrich_sfdc_accounts(
    profile: str = "myorg",
    soql_filter: str = "",
    account_ids: list[str] | None = None,
    limit: int = 50,
    cleanup: bool = True,
) -> str:
    """Full enrichment pipeline for Salesforce Accounts via Clay."""
    prof = _get_profile(profile)

    required = ["account_table_id", "account_view_id", "account_input_columns",
                "account_enrichment_column_ids", "account_enrichment_subfield_map"]
    missing = [k for k in required if not prof.get(k)]
    if missing:
        return (
            f"Profile '{profile}' is missing: {missing}\n"
            "Run clay_get_table_schema to find field IDs, then update profiles.json."
        )

    table_id = prof["account_table_id"]
    view_id = prof["account_view_id"]
    input_cols = prof["account_input_columns"]
    enrich_ids = prof["account_enrichment_column_ids"]
    enrich_field_id = enrich_ids[0]
    subfield_map = {k: v for k, v in prof["account_enrichment_subfield_map"].items() if not k.startswith("_")}

    domain_field = input_cols.get("domain", "")
    linkedin_field = input_cols.get("linkedin", "")

    if not domain_field:
        return "account_input_columns.domain is required in profiles.json"

    sf = _get_sf(profile)
    acct_field_map = prof.get("account_field_map", {})
    sfdc_domain = acct_field_map.get("domain", "Website")
    sfdc_linkedin = acct_field_map.get("linkedin", "LinkedIn_URL__c")

    extra = sorted(set([sfdc_domain, sfdc_linkedin]) - {"Id", "Name"})
    fields_soql = ", ".join(["Id", "Name"] + extra)

    if account_ids:
        id_list = ", ".join(f"'{i}'" for i in account_ids)
        where = f"Id IN ({id_list})"
    elif soql_filter:
        where = soql_filter
    else:
        return "Provide either soql_filter or account_ids."

    soql = f"SELECT {fields_soql} FROM Account WHERE {where} LIMIT {limit}"
    records = sf.query(soql)
    if not records:
        return "No Accounts matched the filter."

    clay = _get_clay(profile)
    domain_to_sfdc_id: dict[str, str] = {}
    cells_list = []
    domain_list = []  # parallel to cells_list for ID→domain mapping

    for r in records:
        domain_val = (r.get(sfdc_domain) or "").strip()
        linkedin_val = (r.get(sfdc_linkedin) or "").strip()
        if domain_val:
            domain_to_sfdc_id[domain_val] = r["Id"]
        cells: dict = {}
        if domain_field and domain_val:
            cells[domain_field] = domain_val
        if linkedin_field and linkedin_val:
            cells[linkedin_field] = linkedin_val
        cells_list.append(cells)
        domain_list.append(domain_val)

    if not domain_to_sfdc_id:
        return f"No accounts had a domain value in '{sfdc_domain}' — cannot enrich without a domain."

    # Process in batches to avoid Clay table overload and polling timeouts
    BATCH_SIZE = 200
    total_batches = (len(cells_list) + BATCH_SIZE - 1) // BATCH_SIZE
    all_sfdc_updates = []

    for batch_num, batch_start in enumerate(range(0, len(cells_list), BATCH_SIZE), 1):
        batch_cells = cells_list[batch_start:batch_start + BATCH_SIZE]
        batch_domains = domain_list[batch_start:batch_start + BATCH_SIZE]
        print(f"[ACCOUNT BATCH {batch_num}/{total_batches}] {len(batch_cells)} accounts...", file=sys.stderr)

        add_results = clay.add_rows(table_id, batch_cells)
        record_ids = _extract_record_ids(add_results)

        # Map record ID → sfdc_id via domain
        rid_to_sfdc_id: dict[str, str] = {}
        for i, rid in enumerate(record_ids):
            if i < len(batch_domains):
                d = batch_domains[i]
                sfdc_id = domain_to_sfdc_id.get(d)
                if sfdc_id:
                    rid_to_sfdc_id[rid] = sfdc_id

        print(f"[Clay] Triggering enrichment columns: {enrich_ids}", file=sys.stderr)
        clay.run_enrichment(table_id, view_id, enrich_ids, num_records=len(batch_cells))

        if not subfield_map:
            continue

        enriched = _poll_by_record_ids(clay, table_id, record_ids, enrich_field_id)

        for rid, full_record in enriched.items():
            sfdc_id = rid_to_sfdc_id.get(rid)
            if not sfdc_id:
                continue
            full_cells = full_record.get("cells") or {}
            fullvalue = _get_enrichment_fullvalue(full_cells.get(enrich_field_id)) or {}
            if not fullvalue:
                continue
            update = {"Id": sfdc_id}
            for subfield, sfdc_field in subfield_map.items():
                val = fullvalue.get(subfield)
                if val not in (None, "", []):
                    update[sfdc_field] = val
            if len(update) > 1:
                all_sfdc_updates.append(update)

    if not subfield_map:
        if cleanup:
            clay.delete_all_rows(table_id, view_id)
        return (
            f"Added {len(cells_list)} rows to Clay and triggered enrichment. "
            "No account_enrichment_subfield_map configured — skipping SFDC write-back. "
            "Use clay_export_table_data to inspect results."
        )

    sfdc_result = []
    if all_sfdc_updates:
        print(f"[Clay] Updating {len(all_sfdc_updates)} SFDC Accounts...", file=sys.stderr)
        sfdc_result = sf.bulk_update("Account", all_sfdc_updates)

    if cleanup:
        clay.delete_all_rows(table_id, view_id)

    successes = len([r for r in sfdc_result if r.get("success")])
    errors = [r for r in sfdc_result if not r.get("success")]
    return (
        f"Enriched {len(all_sfdc_updates)}/{len(records)} Accounts.\n"
        f"SFDC updated: {successes} records.\n"
        + (f"Errors: {errors}" if errors else "")
    )


def clay_enrich_sfdc_contacts(
    profile: str = "myorg",
    soql_filter: str = "",
    contact_ids: list[str] | None = None,
    limit: int = 50,
    cleanup: bool = True,
) -> str:
    """Full enrichment pipeline for Salesforce Contacts via Clay."""
    prof = _get_profile(profile)

    required = ["contact_table_id", "contact_view_id", "contact_input_columns",
                "contact_enrichment_column_ids"]
    missing = [k for k in required if not prof.get(k)]
    if missing:
        return (
            f"Profile '{profile}' is missing: {missing}\n"
            "Run clay_get_table_schema to find field IDs, then update profiles.json."
        )

    table_id = prof["contact_table_id"]
    view_id = prof["contact_view_id"]
    input_cols = prof["contact_input_columns"]
    enrich_ids = prof["contact_enrichment_column_ids"]
    enrich_field_id = enrich_ids[0]
    subfield_map = {k: v for k, v in (prof.get("contact_enrichment_subfield_map") or {}).items() if not k.startswith("_")}

    email_field = input_cols.get("email", "")
    linkedin_field = input_cols.get("linkedin", "")

    sf = _get_sf(profile)
    cont_field_map = prof.get("contact_field_map", {})
    sfdc_email = cont_field_map.get("email", "Email")
    sfdc_linkedin = cont_field_map.get("linkedin", "LinkedIn_URL__c")

    extra = sorted(set([sfdc_email, sfdc_linkedin]) - {"Id", "FirstName", "LastName"})
    fields_soql = ", ".join(["Id", "FirstName", "LastName"] + extra)

    if contact_ids:
        id_list = ", ".join(f"'{i}'" for i in contact_ids)
        where = f"Id IN ({id_list})"
    elif soql_filter:
        where = soql_filter
    else:
        return "Provide either soql_filter or contact_ids."

    soql = f"SELECT {fields_soql} FROM Contact WHERE {where} LIMIT {limit}"
    records = sf.query(soql)
    if not records:
        return "No Contacts matched the filter."

    clay = _get_clay(profile)
    email_to_sfdc_id: dict[str, str] = {}
    cells_list = []
    email_list = []  # parallel to cells_list for ID→email mapping

    for r in records:
        email_val = (r.get(sfdc_email) or "").strip()
        linkedin_val = (r.get(sfdc_linkedin) or "").strip()
        if email_val:
            email_to_sfdc_id[email_val] = r["Id"]
        cells: dict = {}
        if email_field and email_val:
            cells[email_field] = email_val
        if linkedin_field and linkedin_val:
            cells[linkedin_field] = linkedin_val
        cells_list.append(cells)
        email_list.append(email_val)

    if not email_to_sfdc_id:
        return f"No contacts had an email in '{sfdc_email}' — cannot match enrichment results."

    # Process in batches to avoid Clay table overload and polling timeouts
    BATCH_SIZE = 200
    total_batches = (len(cells_list) + BATCH_SIZE - 1) // BATCH_SIZE
    all_sfdc_updates = []

    for batch_num, batch_start in enumerate(range(0, len(cells_list), BATCH_SIZE), 1):
        batch_cells = cells_list[batch_start:batch_start + BATCH_SIZE]
        batch_emails = email_list[batch_start:batch_start + BATCH_SIZE]
        print(f"[CONTACT BATCH {batch_num}/{total_batches}] {len(batch_cells)} contacts...", file=sys.stderr)

        add_results = clay.add_rows(table_id, batch_cells)
        record_ids = _extract_record_ids(add_results)

        # Map record ID → sfdc_id via email
        rid_to_sfdc_id: dict[str, str] = {}
        for i, rid in enumerate(record_ids):
            if i < len(batch_emails):
                e = batch_emails[i]
                sfdc_id = email_to_sfdc_id.get(e)
                if sfdc_id:
                    rid_to_sfdc_id[rid] = sfdc_id

        print(f"[Clay] Triggering enrichment columns: {enrich_ids}", file=sys.stderr)
        clay.run_enrichment(table_id, view_id, enrich_ids, num_records=len(batch_cells))

        if not subfield_map:
            continue

        enriched = _poll_by_record_ids(clay, table_id, record_ids, enrich_field_id)

        for rid, full_record in enriched.items():
            sfdc_id = rid_to_sfdc_id.get(rid)
            if not sfdc_id:
                continue
            full_cells = full_record.get("cells") or {}
            fullvalue = _get_enrichment_fullvalue(full_cells.get(enrich_field_id)) or {}
            if not fullvalue:
                continue
            update = {"Id": sfdc_id}
            for subfield, sfdc_field in subfield_map.items():
                val = fullvalue.get(subfield)
                if val not in (None, "", []):
                    update[sfdc_field] = val
            if len(update) > 1:
                all_sfdc_updates.append(update)

    if not subfield_map:
        if cleanup:
            clay.delete_all_rows(table_id, view_id)
        return (
            f"Added {len(cells_list)} rows to Clay and triggered enrichment. "
            "No contact_enrichment_subfield_map configured — skipping SFDC write-back. "
            "Use clay_export_table_data to inspect results."
        )

    sfdc_result = []
    if all_sfdc_updates:
        print(f"[Clay] Updating {len(all_sfdc_updates)} SFDC Contacts...", file=sys.stderr)
        sfdc_result = sf.bulk_update("Contact", all_sfdc_updates)

    if cleanup:
        clay.delete_all_rows(table_id, view_id)

    successes = len([r for r in sfdc_result if r.get("success")])
    errors = [r for r in sfdc_result if not r.get("success")]
    return (
        f"Enriched {len(all_sfdc_updates)}/{len(records)} Contacts.\n"
        f"SFDC updated: {successes} records.\n"
        + (f"Errors: {errors}" if errors else "")
    )


def clay_enrich_accounts_with_gaps(
    profile: str = "myorg",
    limit: int = 50,
    cleanup: bool = True,
) -> str:
    """Enrich Accounts with blank NumberOfEmployees or Industry (must have domain)."""
    return clay_enrich_sfdc_accounts(
        profile=profile,
        soql_filter=(
            "(NumberOfEmployees = null OR Industry = null) "
            "AND (Domain2__c != null OR Website != null)"
        ),
        limit=limit,
        cleanup=cleanup,
    )


def clay_enrich_contacts_with_gaps(
    profile: str = "myorg",
    limit: int = 50,
    cleanup: bool = True,
) -> str:
    """Enrich Contacts with blank Title but a valid email."""
    return clay_enrich_sfdc_contacts(
        profile=profile,
        soql_filter="Title = null AND Email != null",
        limit=limit,
        cleanup=cleanup,
    )


def clay_phone_waterfall_sfdc_contacts(
    profile: str = "myorg",
    soql_filter: str = "",
    contact_ids: list[str] | None = None,
    limit: int = 50,
    cleanup: bool = True,
) -> str:
    """
    Enrich Salesforce Contacts with missing Phone via Clay's Phone Waterfall.

    Triggers the built-in Phone Waterfall action column (which chains ~9 providers)
    and writes the result back to the Contact Phone field in SFDC.

    Args:
        profile:      Profile name (default: "myorg")
        soql_filter:  SOQL WHERE clause to target contacts (default: email+phone gap)
        contact_ids:  Explicit list of Contact IDs (alternative to soql_filter)
        limit:        Max contacts per run (default: 50)
        cleanup:      Delete Clay rows after enrichment (default: True)
    """
    prof = _get_profile(profile)

    table_id = prof.get("contact_table_id", "")
    view_id = prof.get("contact_view_id", "")
    email_field = (prof.get("contact_input_columns") or {}).get("email", "")
    waterfall_col = prof.get("contact_phone_waterfall_column_id", "")
    phone_formula_field = prof.get("contact_phone_formula_field_id", "")

    missing = [k for k, v in {
        "contact_table_id": table_id,
        "contact_view_id": view_id,
        "contact_input_columns.email": email_field,
        "contact_phone_waterfall_column_id": waterfall_col,
        "contact_phone_formula_field_id": phone_formula_field,
    }.items() if not v]
    if missing:
        return (
            f"Profile '{profile}' is missing: {missing}\n"
            "Add contact_phone_waterfall_column_id and contact_phone_formula_field_id to profiles.json."
        )

    sf = _get_sf(profile)
    clay = _get_clay(profile)

    cont_field_map = prof.get("contact_field_map", {})
    sfdc_email = cont_field_map.get("email", "Email")

    if contact_ids:
        id_list = ", ".join(f"'{i}'" for i in contact_ids)
        where = f"Id IN ({id_list})"
    elif soql_filter:
        where = soql_filter
    else:
        where = f"{sfdc_email} != null AND Phone = null"

    soql = f"SELECT Id, FirstName, LastName, {sfdc_email} FROM Contact WHERE {where} LIMIT {limit}"
    records = sf.query(soql)
    if not records:
        return "No Contacts matched the filter."

    email_to_sfdc_id: dict[str, str] = {}
    cells_list = []
    email_list = []

    for r in records:
        email_val = (r.get(sfdc_email) or "").strip()
        if not email_val:
            continue
        email_to_sfdc_id[email_val] = r["Id"]
        cells_list.append({email_field: email_val})
        email_list.append(email_val)

    if not email_to_sfdc_id:
        return "No contacts had a valid email — cannot run waterfall."

    BATCH_SIZE = 50  # waterfall is slow; keep batches small
    total_batches = (len(cells_list) + BATCH_SIZE - 1) // BATCH_SIZE
    all_sfdc_updates: list[dict] = []

    for batch_num, batch_start in enumerate(range(0, len(cells_list), BATCH_SIZE), 1):
        batch_cells = cells_list[batch_start:batch_start + BATCH_SIZE]
        batch_emails = email_list[batch_start:batch_start + BATCH_SIZE]
        print(f"[WATERFALL BATCH {batch_num}/{total_batches}] {len(batch_cells)} contacts...", file=sys.stderr)

        add_results = clay.add_rows(table_id, batch_cells)
        record_ids = _extract_record_ids(add_results)

        rid_to_sfdc_id: dict[str, str] = {}
        for i, rid in enumerate(record_ids):
            if i < len(batch_emails):
                sfdc_id = email_to_sfdc_id.get(batch_emails[i])
                if sfdc_id:
                    rid_to_sfdc_id[rid] = sfdc_id

        print(f"[Clay] Triggering phone waterfall column: {waterfall_col}", file=sys.stderr)
        clay.run_enrichment(table_id, view_id, [waterfall_col], num_records=len(batch_cells))

        # Poll until the waterfall action column reaches terminal state
        enriched = _poll_by_record_ids(
            clay, table_id, record_ids, waterfall_col,
            timeout=max(300, len(batch_cells) * 30),  # waterfalls can take ~30s/record
        )

        for rid, full_record in enriched.items():
            sfdc_id = rid_to_sfdc_id.get(rid)
            if not sfdc_id:
                continue
            full_cells = full_record.get("cells") or {}
            # Read from the waterfall action column directly:
            # value is the phone number string when status=SUCCESS,
            # or "❌ No phone found" / None when SUCCESS_NO_DATA
            cell = full_cells.get(waterfall_col) or {}
            status = (cell.get("metadata") or {}).get("status", "")
            if status == "SUCCESS":
                phone_val = str(cell.get("value") or "").strip()
                # Strip any leading emoji/status prefix (e.g. "✅ +1234..." → "+1234...")
                if " " in phone_val:
                    parts = phone_val.split(" ", 1)
                    if not parts[0].startswith("+") and parts[1].startswith("+"):
                        phone_val = parts[1].strip()
                if phone_val and phone_val.startswith("+"):
                    all_sfdc_updates.append({"Id": sfdc_id, "Phone": phone_val})

    sfdc_result: list = []
    if all_sfdc_updates:
        print(f"[Clay] Updating {len(all_sfdc_updates)} SFDC Contacts with Phone...", file=sys.stderr)
        sfdc_result = sf.bulk_update("Contact", all_sfdc_updates)

    if cleanup:
        clay.delete_all_rows(table_id, view_id)

    successes = len([r for r in sfdc_result if r.get("success")])
    errors = [r for r in sfdc_result if not r.get("success")]
    return (
        f"Phone Waterfall: found phone for {len(all_sfdc_updates)}/{len(records)} Contacts.\n"
        f"SFDC updated: {successes} records.\n"
        + (f"Errors: {errors}" if errors else "")
    )


def clay_check_stale_contacts(
    profile: str = "myorg",
    soql_filter: str = "",
    contact_ids: list[str] | None = None,
    limit: int = 200,
    sample_size: int = 50,
    cleanup: bool = True,
) -> str:
    """
    Enrich Contacts via Clay LinkedIn and check whether their current employer
    still matches their SFDC Account. Returns a staleness report without
    writing anything back to Salesforce.
    """
    prof = _get_profile(profile)

    required = ["contact_table_id", "contact_view_id", "contact_input_columns",
                "contact_enrichment_column_ids"]
    missing = [k for k in required if not prof.get(k)]
    if missing:
        return (
            f"Profile '{profile}' is missing: {missing}\n"
            "Run clay_get_table_schema to find field IDs, then update profiles.json."
        )

    table_id = prof["contact_table_id"]
    view_id = prof["contact_view_id"]
    input_cols = prof["contact_input_columns"]
    enrich_ids = prof["contact_enrichment_column_ids"]
    enrich_field_id = enrich_ids[0]
    email_field = input_cols.get("email", "")

    sf = _get_sf(profile)
    cont_field_map = prof.get("contact_field_map", {})
    sfdc_email = cont_field_map.get("email", "Email")

    if contact_ids:
        id_list = ", ".join(f"'{i}'" for i in contact_ids)
        where = f"Id IN ({id_list})"
    elif soql_filter:
        where = soql_filter
    else:
        return "Provide either soql_filter or contact_ids."

    soql = (
        f"SELECT Id, FirstName, LastName, {sfdc_email}, AccountId, "
        f"Account.Name, Account.Domain2__c, Account.Website "
        f"FROM Contact WHERE {where} AND {sfdc_email} != null LIMIT {limit}"
    )
    records = sf.query(soql)
    if not records:
        return "No Contacts matched the filter."

    clay = _get_clay(profile)
    email_to_contact: dict[str, dict] = {}
    cells_list = []

    for r in records:
        email_val = (r.get(sfdc_email) or "").strip()
        if not email_val:
            continue
        acct = r.get("Account") or {}
        acct_domain = (acct.get("Domain2__c") or acct.get("Website") or "").strip()
        email_to_contact[email_val] = {
            "sfdc_id": r["Id"],
            "name": f"{r.get('FirstName') or ''} {r.get('LastName') or ''}".strip(),
            "email": email_val,
            "account_id": r.get("AccountId") or "",
            "account_name": acct.get("Name") or "",
            "account_domain": acct_domain,
        }
        cells: dict = {}
        if email_field:
            cells[email_field] = email_val
        cells_list.append(cells)

    if not email_to_contact:
        return f"No contacts had a value in '{sfdc_email}' — cannot run enrichment."

    print(f"[Clay] Adding {len(cells_list)} Contact rows for staleness check...", file=sys.stderr)
    add_responses = clay.add_rows(table_id, cells_list)

    # Extract record IDs from add_rows response — no table scan needed.
    # add_responses is a list of batch responses, each {"records": [{"id": ..., "cells": ...}, ...]}
    rid_to_email: dict[str, str] = {}
    email_list = list(email_to_contact.keys())  # same order as cells_list
    row_idx = 0
    for batch_resp in add_responses:
        for record in (batch_resp or {}).get("records", []):
            rid = record.get("id") or ""
            if rid and row_idx < len(email_list):
                email_val = email_list[row_idx]
                if email_val in email_to_contact:
                    rid_to_email[rid] = email_val
            row_idx += 1

    if not rid_to_email:
        return "Could not extract record IDs from add_rows response — check Clay API response format."

    print(f"[Clay] Triggering enrichment columns: {enrich_ids}", file=sys.stderr)
    clay.run_enrichment(table_id, view_id, enrich_ids, num_records=len(cells_list))

    # Poll by individual record ID — no repeated full-table scans.
    poll_timeout = max(90, len(rid_to_email) * 8)
    deadline = time.time() + poll_timeout
    pending_ids = set(rid_to_email.keys())
    enriched_records: dict[str, dict] = {}  # rid → full record

    print(f"[Clay] Polling {len(pending_ids)} records by ID (timeout {poll_timeout}s)...", file=sys.stderr)
    while time.time() < deadline and pending_ids:
        done_ids: set[str] = set()
        for rid in list(pending_ids):
            try:
                full_record = clay.get_record(table_id, rid)
                full_cells = full_record.get("cells") or {}
                if _is_enrichment_terminal(full_cells.get(enrich_field_id)):
                    enriched_records[rid] = full_record
                    done_ids.add(rid)
            except Exception:
                pass
        pending_ids -= done_ids
        if not pending_ids:
            print(f"[Clay] All {len(enriched_records)} records enriched.", file=sys.stderr)
            break
        print(f"[Clay] {len(enriched_records)}/{len(rid_to_email)} done", file=sys.stderr)
        time.sleep(5)

    if pending_ids:
        print(f"[Clay] Timeout: {len(pending_ids)} records still pending.", file=sys.stderr)

    stale_list: list[dict] = []
    no_data_count = 0
    non_stale_count = 0
    matched_account_ids: set[str] = set()
    stale_account_ids: set[str] = set()

    for rid, full_record in enriched_records.items():
        email_val = rid_to_email.get(rid, "")
        contact_data = email_to_contact.get(email_val)
        if not contact_data:
            continue

        full_cells = full_record.get("cells") or {}
        fullvalue = _get_enrichment_fullvalue(full_cells.get(enrich_field_id)) or {}

        latest_exp = fullvalue.get("latest_experience") or {}
        current_exps = fullvalue.get("current_experience") or []
        current_exp = current_exps[0] if current_exps else {}

        clay_company = (
            latest_exp.get("company")
            or current_exp.get("company")
            or fullvalue.get("org")
            or ""
        )
        clay_domain_raw = (
            latest_exp.get("company_domain")
            or current_exp.get("company_domain")
            or ""
        )

        is_stale_flag, reason = _is_stale(
            account_name=contact_data["account_name"],
            account_domain=contact_data["account_domain"],
            clay_company=clay_company,
            clay_domain_raw=clay_domain_raw,
        )

        acct_id = contact_data["account_id"]
        if reason == "no_clay_data":
            no_data_count += 1
            if acct_id:
                matched_account_ids.add(acct_id)
        elif is_stale_flag:
            stale_list.append({
                "contact_id": contact_data["sfdc_id"],
                "contact_name": contact_data["name"],
                "contact_email": email_val,
                "account_id": acct_id,
                "account_name": contact_data["account_name"],
                "account_domain": contact_data["account_domain"],
                "clay_company": clay_company,
                "clay_company_domain": _normalize_domain(clay_domain_raw),
                "mismatch_reason": reason,
            })
            if acct_id:
                stale_account_ids.add(acct_id)
        else:
            non_stale_count += 1
            if acct_id:
                matched_account_ids.add(acct_id)

    matched_account_ids -= stale_account_ids

    if cleanup:
        # Only delete the rows we added (by ID) — avoids slow full-table scan.
        added_ids = list(rid_to_email.keys())
        if added_ids:
            clay.delete_rows(table_id, added_ids)

    total = len(rid_to_email)
    stale_count = len(stale_list)
    stale_pct = round(stale_count / total * 100, 1) if total else 0.0

    result = {
        "summary": {
            "total_contacts_checked": total,
            "enriched": total - no_data_count,
            "no_data": no_data_count,
            "stale_contacts": stale_count,
            "stale_pct": stale_pct,
            "non_stale_contacts": non_stale_count,
            "matched_account_ids_count": len(matched_account_ids),
            "stale_only_account_ids_count": len(stale_account_ids - matched_account_ids),
        },
        "matched_account_ids": sorted(matched_account_ids),
        "stale_account_ids": sorted(stale_account_ids - matched_account_ids),
        "stale_sample": stale_list[:sample_size],
        "note": (
            "Pass matched_account_ids to clay_enrich_sfdc_accounts(account_ids=...) "
            "to enrich only validated Accounts. Review stale_sample before deciding "
            "next steps for stale contacts."
        ),
    }
    return json.dumps(result, indent=2)


# ============================================================================
# Apollo Direct Enrichment
# ============================================================================

def apollo_enrich_sfdc_contacts(
    profile: str = "myorg",
    soql_filter: str = "",
    contact_ids: list[str] | None = None,
    fields: list[str] | None = None,
    limit: int = 50,
) -> str:
    """
    Enrich Salesforce Contacts directly via Apollo People Match API.

    Calls Apollo's /v1/people/match endpoint for each contact and writes
    Phone, Title, and/or Email back to SFDC — only filling blank fields.

    Args:
        profile:      Profile name (default: "myorg")
        soql_filter:  SOQL WHERE clause to target contacts
        contact_ids:  Explicit list of Contact IDs (alternative to soql_filter)
        fields:       SFDC fields to fill — default ["Phone", "Title", "Email"]
        limit:        Max contacts to enrich per run (default: 50)
    """
    import urllib.request
    import urllib.error

    prof = _get_profile(profile)
    api_key = prof.get("apollo_api_key", "")
    if not api_key:
        return (
            f"Profile '{profile}' is missing 'apollo_api_key' in profiles.json.\n"
            "Add: \"apollo_api_key\": \"<your key>\" to the profile."
        )

    if fields is None:
        fields = ["Phone", "Title", "Email"]

    sf = _get_sf(profile)

    # Build SOQL
    if contact_ids:
        id_list = ", ".join(f"'{i}'" for i in contact_ids)
        where = f"Id IN ({id_list})"
    elif soql_filter:
        where = soql_filter
    else:
        where = "(Phone = null OR Title = null OR Email = null)"

    soql = (
        f"SELECT Id, FirstName, LastName, Email, Phone, Title, Account.Name "
        f"FROM Contact WHERE {where} LIMIT {limit}"
    )
    records = sf.query(soql)
    if not records:
        return "No Contacts matched the filter."

    updates: list[dict] = []
    matched = 0
    no_match = 0

    for r in records:
        email = (r.get("Email") or "").strip()
        first = (r.get("FirstName") or "").strip()
        last = (r.get("LastName") or "").strip()
        account_name = ((r.get("Account") or {}).get("Name") or "").strip()

        if not email and not (first and last):
            no_match += 1
            continue

        payload: dict = {}
        if email:
            payload["email"] = email
        if first:
            payload["first_name"] = first
        if last:
            payload["last_name"] = last
        if account_name:
            payload["organization_name"] = account_name

        try:
            req = urllib.request.Request(
                "https://api.apollo.io/v1/people/match",
                data=json.dumps(payload).encode(),
                headers={
                    "Content-Type": "application/json",
                    "Cache-Control": "no-cache",
                    "X-Api-Key": api_key,
                    "User-Agent": "Mozilla/5.0 (compatible; enrichment-script/1.0)",
                    "Accept": "application/json",
                },
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=15) as resp:
                data = json.loads(resp.read())
        except urllib.error.HTTPError as e:
            print(f"[Apollo] HTTP {e.code} for {email}: {e.read()}", file=sys.stderr)
            time.sleep(1.0)
            continue
        except Exception as e:
            print(f"[Apollo] Error for {email}: {e}", file=sys.stderr)
            time.sleep(0.5)
            continue

        person = data.get("person") or {}
        if not person:
            no_match += 1
            time.sleep(0.3)
            continue

        update: dict = {"Id": r["Id"]}

        if "Phone" in fields and not r.get("Phone"):
            phones = person.get("phone_numbers") or []
            work_phones = [p for p in phones if (p.get("type") or "").startswith("work")]
            pick = work_phones or phones
            if pick:
                num = pick[0].get("sanitized_number") or pick[0].get("raw_number") or ""
                if num:
                    update["Phone"] = num

        if "Title" in fields and not r.get("Title"):
            title = (person.get("title") or "").strip()
            if title:
                update["Title"] = title

        if "Email" in fields and not r.get("Email"):
            verified = (person.get("email") or "").strip()
            if verified:
                update["Email"] = verified

        if len(update) > 1:
            updates.append(update)
            matched += 1

        time.sleep(0.35)  # stay under Apollo's ~200 req/min rate limit

    sfdc_result: list = []
    if updates:
        print(f"[Apollo] Updating {len(updates)} SFDC Contacts...", file=sys.stderr)
        sfdc_result = sf.bulk_update("Contact", updates)

    successes = len([x for x in sfdc_result if x.get("success")])
    errors = [x for x in sfdc_result if not x.get("success")]

    return (
        f"Apollo enriched {matched}/{len(records)} Contacts "
        f"({no_match} had no usable identifier).\n"
        f"SFDC updated: {successes} records.\n"
        + (f"Errors: {errors}" if errors else "")
    )


def apollo_enrich_sfdc_accounts(
    profile: str = "myorg",
    soql_filter: str = "",
    account_ids: list[str] | None = None,
    fields: list[str] | None = None,
    limit: int = 50,
) -> str:
    """
    Enrich Salesforce Accounts directly via Apollo Organization Enrich API.

    Calls Apollo's /v1/organizations/enrich for each account (by domain) and
    writes NumberOfEmployees, Industry, and/or Description back to SFDC —
    only filling fields that are currently blank.

    Args:
        profile:      Profile name (default: "myorg")
        soql_filter:  SOQL WHERE clause to target accounts
        account_ids:  Explicit list of Account IDs (alternative to soql_filter)
        fields:       SFDC fields to fill — default ["NumberOfEmployees", "Industry", "Description"]
        limit:        Max accounts to enrich per run (default: 50)
    """
    import urllib.request
    import urllib.error
    import urllib.parse

    prof = _get_profile(profile)
    api_key = prof.get("apollo_api_key", "")
    if not api_key:
        return (
            f"Profile '{profile}' is missing 'apollo_api_key' in profiles.json.\n"
            "Add: \"apollo_api_key\": \"<your key>\" to the profile."
        )

    if fields is None:
        fields = ["NumberOfEmployees", "Industry", "Description"]

    sf = _get_sf(profile)

    # Build SOQL
    if account_ids:
        id_list = ", ".join(f"'{i}'" for i in account_ids)
        where = f"Id IN ({id_list})"
    elif soql_filter:
        where = soql_filter
    else:
        where = "Website != null AND (NumberOfEmployees = null OR Industry = null)"

    soql = (
        f"SELECT Id, Name, Website, NumberOfEmployees, Industry, Description "
        f"FROM Account WHERE {where} LIMIT {limit}"
    )
    records = sf.query(soql)
    if not records:
        return "No Accounts matched the filter."

    def _extract_domain(website: str) -> str:
        """Strip protocol, www, and path from a URL to get bare domain."""
        w = website.strip().lower()
        if "://" not in w:
            w = "https://" + w
        parsed = urllib.parse.urlparse(w)
        domain = parsed.hostname or ""
        if domain.startswith("www."):
            domain = domain[4:]
        return domain

    updates: list[dict] = []
    matched = 0
    no_match = 0

    for r in records:
        website = (r.get("Website") or "").strip()
        if not website:
            no_match += 1
            continue

        domain = _extract_domain(website)
        if not domain:
            no_match += 1
            continue

        enrich_url = (
            f"https://api.apollo.io/v1/organizations/enrich"
            f"?domain={urllib.parse.quote(domain)}"
        )
        try:
            req = urllib.request.Request(
                enrich_url,
                headers={
                    "Cache-Control": "no-cache",
                    "Content-Type": "application/json",
                    "X-Api-Key": api_key,
                    "User-Agent": "Mozilla/5.0 (compatible; enrichment-script/1.0)",
                    "Accept": "application/json",
                },
                method="GET",
            )
            with urllib.request.urlopen(req, timeout=15) as resp:
                data = json.loads(resp.read())
        except urllib.error.HTTPError as e:
            print(f"[Apollo] HTTP {e.code} for {domain}: {e.read()}", file=sys.stderr)
            time.sleep(1.0)
            continue
        except Exception as e:
            print(f"[Apollo] Error for {domain}: {e}", file=sys.stderr)
            time.sleep(0.5)
            continue

        org = data.get("organization") or {}
        if not org:
            no_match += 1
            time.sleep(0.3)
            continue

        update: dict = {"Id": r["Id"]}

        if "NumberOfEmployees" in fields and not r.get("NumberOfEmployees"):
            emp = org.get("estimated_num_employees")
            if emp:
                update["NumberOfEmployees"] = int(emp)

        if "Industry" in fields and not r.get("Industry"):
            industry = (org.get("industry") or "").strip()
            if industry:
                update["Industry"] = industry

        if "Description" in fields and not r.get("Description"):
            desc = (
                org.get("short_description")
                or org.get("seo_description")
                or ""
            ).strip()
            if desc:
                update["Description"] = desc

        if len(update) > 1:
            updates.append(update)
            matched += 1

        time.sleep(0.35)  # stay under Apollo rate limits

    sfdc_result: list = []
    if updates:
        print(f"[Apollo] Updating {len(updates)} SFDC Accounts...", file=sys.stderr)
        sfdc_result = sf.bulk_update("Account", updates)

    successes = len([x for x in sfdc_result if x.get("success")])
    errors = [x for x in sfdc_result if not x.get("success")]

    return (
        f"Apollo enriched {matched}/{len(records)} Accounts "
        f"({no_match} had no domain/match).\n"
        f"SFDC updated: {successes} records.\n"
        + (f"Errors: {errors}" if errors else "")
    )
