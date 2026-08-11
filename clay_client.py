"""
Clay API Client
Authenticates directly with Clay's internal API.
No Chrome extension required.

Auth options (in profiles.json):
  1. clay_cookie — paste your claysession cookie from browser DevTools (simplest)
  2. clay_email + clay_password — logs in automatically, caches cookie ~24h

To get your cookie: clay.com → DevTools → Application → Cookies → claysession → copy Value
Then set in profiles.json: "clay_cookie": "claysession=s%3Axyz..."
"""

import http.client
import json
import sys
import time
from pathlib import Path
from typing import Any

CLAY_API_HOST = "api.clay.com"
SESSION_CACHE_FILE = Path.home() / ".clay-mcp" / "session_cache.json"

# Clay frontend version header (required by Clay API)
CLAY_FRONTEND_VERSION = "2025.02.02"


# =============================================================================
# SESSION CACHE
# =============================================================================

def _load_cache() -> dict:
    if SESSION_CACHE_FILE.exists():
        try:
            return json.loads(SESSION_CACHE_FILE.read_text())
        except Exception:
            pass
    return {}


def _save_cache(cache: dict) -> None:
    SESSION_CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
    SESSION_CACHE_FILE.write_text(json.dumps(cache, indent=2))


# =============================================================================
# CLAY CLIENT
# =============================================================================

class ClayClient:
    """
    Thin wrapper around Clay's internal /v3/ API.
    Supports static cookie auth or email/password auto-login.
    """

    def __init__(
        self,
        profile_name: str = "default",
        cookie: str = "",
        email: str = "",
        password: str = "",
    ):
        self._profile_name = profile_name
        self._static_cookie = cookie.strip()  # static cookie — used as-is, never refreshed
        self._email = email
        self._password = password
        self._cookie: str | None = None
        self._expiry: float = 0.0

    # ------------------------------------------------------------------
    # Auth
    # ------------------------------------------------------------------

    def _ensure_session(self) -> None:
        """Use static cookie, cached session, or re-authenticate as needed."""
        # Static cookie — use as-is forever (user manages refresh)
        if self._static_cookie:
            self._cookie = self._static_cookie
            return

        refresh_threshold = 30 * 60  # refresh 30min before expiry

        # Valid in-memory session
        if self._cookie and time.time() < self._expiry - refresh_threshold:
            return

        # Try restoring from cache
        cache = _load_cache()
        cached = cache.get(self._profile_name)
        if cached and time.time() < cached.get("expiry", 0) - refresh_threshold:
            self._cookie = cached["cookie"]
            self._expiry = cached["expiry"]
            print(f"[Clay] Restored session for '{self._profile_name}'", file=sys.stderr)
            return

        # Fresh login
        if not (self._email and self._password):
            raise RuntimeError(
                f"Clay auth failed for profile '{self._profile_name}': "
                "set clay_cookie (from browser DevTools) or clay_email + clay_password in profiles.json"
            )
        print(f"[Clay] Logging in as {self._email}...", file=sys.stderr)
        self._cookie, self._expiry = self._login_with_password()
        cache[self._profile_name] = {"cookie": self._cookie, "expiry": self._expiry}
        _save_cache(cache)
        print(f"[Clay] Authenticated '{self._profile_name}'", file=sys.stderr)

    def _login_with_password(self) -> tuple[str, float]:
        """POST /v3/auth/login → (claysession cookie, expiry timestamp)"""
        body = json.dumps({"email": self._email, "password": self._password, "source": None}).encode()
        conn = http.client.HTTPSConnection(CLAY_API_HOST, timeout=15)
        conn.request(
            "POST", "/v3/auth/login",
            body=body,
            headers={"Content-Type": "application/json", "Content-Length": str(len(body))},
        )
        resp = conn.getresponse()
        raw = resp.read().decode()
        conn.close()

        # Extract claysession cookie from Set-Cookie headers
        set_cookie = resp.getheader("set-cookie", "")
        # http.client returns only the first Set-Cookie; handle multi-header case
        cookie_header = None
        for line in set_cookie.split("\n"):
            if line.strip().startswith("claysession="):
                cookie_header = line.strip()
                break
        if not cookie_header:
            # Try parsing body for error message
            try:
                body_json = json.loads(raw)
                raise RuntimeError(f"Clay login failed: {body_json.get('message') or body_json.get('error') or raw[:200]}")
            except (json.JSONDecodeError, KeyError):
                raise RuntimeError(f"Clay login failed — no session cookie. HTTP {resp.status}: {raw[:200]}")

        cookie = cookie_header.split(";")[0]  # "claysession=s%3Axyz..."

        # Parse Expires if present, otherwise default to 24h
        expiry = time.time() + 24 * 60 * 60
        for part in cookie_header.split(";"):
            part = part.strip()
            if part.lower().startswith("expires="):
                try:
                    import email.utils
                    expiry = email.utils.parsedate_to_datetime(part[8:]).timestamp()
                except Exception:
                    pass
                break

        return cookie, expiry

    # ------------------------------------------------------------------
    # HTTP
    # ------------------------------------------------------------------

    def _request(
        self,
        method: str,
        path: str,
        body: Any = None,
        content_type: str = "application/json",
        _retry: bool = True,
    ) -> Any:
        self._ensure_session()

        payload: bytes | None = None
        if body is not None:
            payload = (json.dumps(body) if not isinstance(body, (str, bytes)) else body)
            if isinstance(payload, str):
                payload = payload.encode()

        headers: dict[str, str] = {
            "Accept": "application/json",
            "Cookie": self._cookie,
            "X-Clay-Frontend-Version": CLAY_FRONTEND_VERSION,
        }
        if payload:
            headers["Content-Type"] = content_type
            headers["Content-Length"] = str(len(payload))

        conn = http.client.HTTPSConnection(CLAY_API_HOST, timeout=30)
        conn.request(method, path, body=payload, headers=headers)
        resp = conn.getresponse()
        raw = resp.read().decode()
        conn.close()

        # Session expired — clear and retry once
        if resp.status == 401 and _retry:
            print(f"[Clay] Session expired, re-authenticating...", file=sys.stderr)
            self._cookie = None
            self._expiry = 0.0
            cache = _load_cache()
            cache.pop(self._profile_name, None)
            _save_cache(cache)
            return self._request(method, path, body, content_type, _retry=False)

        if resp.status >= 400:
            try:
                err = json.loads(raw)
                msg = err.get("message") or err.get("error") or raw[:300]
            except Exception:
                msg = raw[:300]
            raise RuntimeError(f"Clay API {method} {path} → HTTP {resp.status}: {msg}")

        if not raw.strip():
            return {}
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            return raw

    # ------------------------------------------------------------------
    # Workspace / Table discovery
    # ------------------------------------------------------------------

    def list_workspaces(self) -> list[dict]:
        return self._request("GET", "/v3/my-workspaces")

    def get_table(self, table_id: str) -> dict:
        return self._request("GET", f"/v3/tables/{table_id}")

    def get_record(self, table_id: str, record_id: str) -> dict:
        """Fetch a single record with full content (includes externalContent.fullValue)."""
        return self._request("GET", f"/v3/tables/{table_id}/records/{record_id}")

    def list_resources(self, workspace_id: int, parent_id: str = "", parent_type: str = "WORKBOOK") -> dict:
        body: dict = {"parentResource": {"id": parent_id, "type": parent_type.upper()} if parent_id else None}
        return self._request("POST", f"/v3/workspaces/{workspace_id}/resources_v2/", body)

    def count_rows(self, table_id: str) -> dict:
        return self._request("GET", f"/v3/tables/{table_id}/count")

    def list_sources(self, table_id: str) -> dict:
        return self._request("GET", f"/v3/sources?tableId={table_id}")

    # ------------------------------------------------------------------
    # Row operations
    # ------------------------------------------------------------------

    def add_row(self, table_id: str, cells: dict) -> dict:
        """
        Add a single row to a Clay table.
        cells: {column_field_id: value, ...}
        """
        import uuid
        row_id = str(uuid.uuid4())
        return self._request("POST", f"/v3/tables/{table_id}/records", {
            "records": [{"id": row_id, "cells": cells}]
        })

    def add_rows(self, table_id: str, rows: list[dict]) -> list[dict]:
        """
        Add multiple rows. Returns list of API results.
        rows: list of cells dicts ({field_id: value})
        """
        import uuid
        records = [{"id": str(uuid.uuid4()), "cells": cells} for cells in rows]
        # Clay accepts records in batches
        BATCH = 50
        results = []
        for i in range(0, len(records), BATCH):
            batch = records[i:i + BATCH]
            result = self._request("POST", f"/v3/tables/{table_id}/records", {"records": batch})
            results.append(result)
        return results

    def get_rows(
        self,
        table_id: str,
        view_id: str,
        limit: int = 100,
        offset: int = 0,
    ) -> list[dict]:
        """Fetch rows from a table view. Returns list of record dicts."""
        resp = self._request("GET", f"/v3/tables/{table_id}/views/{view_id}/records?limit={limit}&offset={offset}")
        if isinstance(resp, list):
            return resp
        return resp.get("records") or resp.get("results") or resp.get("data") or []

    def get_all_rows(self, table_id: str, view_id: str, max_rows: int = 0) -> list[dict]:
        """Paginate through all rows in a view."""
        all_rows: list[dict] = []
        offset = 0
        batch = 100
        while True:
            limit = batch if not max_rows else min(batch, max_rows - len(all_rows))
            if limit <= 0:
                break
            rows = self.get_rows(table_id, view_id, limit=limit, offset=offset)
            all_rows.extend(rows)
            if len(rows) < limit:
                break
            offset += len(rows)
            if max_rows and len(all_rows) >= max_rows:
                break
        return all_rows

    def delete_rows(self, table_id: str, record_ids: list[str]) -> dict:
        return self._request("DELETE", f"/v3/tables/{table_id}/records", {"recordIds": record_ids})

    def delete_all_rows(self, table_id: str, view_id: str) -> dict:
        """Delete all rows visible in a view — fetches and deletes in pages to handle large tables."""
        deleted = 0
        result: dict = {}
        while True:
            rows = self.get_rows(table_id, view_id, limit=100, offset=0)
            if not rows:
                break
            record_ids = [row.get("id") or row.get("_record_id") or "" for row in rows]
            record_ids = [rid for rid in record_ids if rid]
            if not record_ids:
                break
            result = self._request("DELETE", f"/v3/tables/{table_id}/records", {"recordIds": record_ids})
            deleted += len(record_ids)
            print(f"[Clay] Deleted {deleted} rows so far...", file=sys.stderr)
            if len(rows) < 100:
                break
        return {"deleted": deleted}

    # ------------------------------------------------------------------
    # Enrichment
    # ------------------------------------------------------------------

    def run_enrichment(
        self,
        table_id: str,
        view_id: str,
        field_ids: list[str],
        num_records: int | None = None,
    ) -> dict:
        """
        Trigger one or more enrichment columns on a view.
        PATCH /v3/tables/{id}/run
        Content-Type: application/x-www-form-urlencoded (Clay quirk — body is JSON string)
        """
        body: dict = {
            "fieldIds": field_ids,
            "runRecords": {
                "viewIdTopRecords": {
                    "viewId": view_id,
                    **({"numRecords": num_records} if num_records else {}),
                }
            },
            "callerName": "API",
            "preview": False,
        }
        return self._request(
            "PATCH",
            f"/v3/tables/{table_id}/run",
            body,
            content_type="application/json",
        )

    # ------------------------------------------------------------------
    # Schema helpers
    # ------------------------------------------------------------------

    def extract_schema(self, table_id: str) -> dict:
        """
        Return a human-readable schema: table name, view IDs, field list.
        Each field: {id, name, type}
        """
        data = self.get_table(table_id)
        table = data.get("table") or data
        fields = data.get("fields") or table.get("fields") or []
        views = table.get("views") or []

        return {
            "table_id": table_id,
            "table_name": table.get("name", ""),
            "views": [{"id": v.get("id"), "name": v.get("name")} for v in views],
            "first_view_id": table.get("firstViewId") or (views[0].get("id") if views else None),
            "fields": [
                {
                    "id": f.get("id"),
                    "name": f.get("name"),
                    "type": f.get("type"),
                }
                for f in fields
                if f.get("id") not in ("f_created_at", "f_updated_at")
            ],
        }

    def extract_rich_schema(self, table_id: str) -> dict:
        """
        Like extract_schema() but returns all raw field metadata, including
        visibility flags (hidden, isHidden, etc.) and system columns.
        Each field dict contains all keys returned by the Clay API, plus a
        normalized '_hidden' bool that checks all known visibility flag names.
        Used by clay_document_table to detect hidden columns.
        """
        data = self.get_table(table_id)
        table = data.get("table") or data
        fields = data.get("fields") or table.get("fields") or []
        views = table.get("views") or []

        processed_fields = []
        for f in fields:
            field_entry = dict(f)  # preserve all raw API keys
            hidden = (
                f.get("hidden")
                or f.get("isHidden")
                or f.get("is_hidden")
                or False
            )
            field_entry["_hidden"] = bool(hidden)
            processed_fields.append(field_entry)

        return {
            "table_id": table_id,
            "table_name": table.get("name", ""),
            "views": [{"id": v.get("id"), "name": v.get("name")} for v in views],
            "first_view_id": table.get("firstViewId") or (views[0].get("id") if views else None),
            "fields": processed_fields,
        }

    def extract_cell_value(self, cell_data: Any) -> Any:
        """Clay cells are {value: ..., metadata: {...}} — extract the value."""
        if cell_data is None:
            return None
        if isinstance(cell_data, dict):
            if "metadata" in cell_data:
                return cell_data.get("value")
        return cell_data
