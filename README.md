# Clay Enrichment MCP Server

A FastMCP Python server that lets Claude control Clay — add rows, trigger enrichment, read results, and optionally write back to Salesforce. No webhooks, no Chrome extension.

Ask Claude to add domains to a Clay table, trigger enrichment, and pull back the results. If you have Salesforce API access, Claude can write the data back automatically. If not, use Clay's built-in **Update Record** action to push results to Salesforce from within Clay itself.

---

## Why This Exists

Clay's UI is great for manual enrichment but doesn't expose a clean API for automated pipelines. This server reverse-engineers Clay's internal `api.clay.com/v3/` endpoints to:

- Programmatically add rows to any Clay table
- Trigger enrichment columns in **full mode** (not preview)
- Poll for results and extract the complete `externalContent.fullValue` payload
- Optionally write results back to Salesforce via the Bulk API

**Salesforce is optional.** The core tools work with any Clay table regardless of where your data lives. If you use Clay's built-in **Update Record** action, Clay handles the Salesforce write-back itself — no API credentials needed on your end.

Each client (Clay workspace + optional Salesforce org) is a named **profile** in a single config file — swap `profile="myorg"` for `profile="acme"` to target a different workspace.

---

## Architecture

```
Claude (MCP client)
       │
       ▼
server.py  (FastMCP — 13 tools)
  ├── clay_client.py  (Clay API: auth, rows, enrichment, polling)
  └── simple_salesforce  (SFDC: SOQL queries, Bulk API updates)
       │
       ├──▶  api.clay.com/v3/  (Clay internal API)
       └──▶  login.salesforce.com  (SFDC REST/Bulk API)
```

### Enrichment Pipeline (accounts)

```
1. SOQL query  →  accounts with gaps (blank Industry / Employees / Website)
2. Build local  domain → sfdc_id  dict
3. POST /v3/tables/{id}/records  →  add rows to Clay (50/batch)
4. PATCH /v3/tables/{id}/run  →  trigger enrichment, preview: false  ← CRITICAL
5. Poll GET /v3/tables/{id}/views/{viewId}/records  →  check metadata.status
6. For each terminal row: GET /v3/tables/{id}/records/{recordId}  →  full data
7. Extract externalContent.fullValue sub-fields via subfield_map
8. SFDC Bulk API update
9. DELETE all rows  →  clean table
```

> **Key insight:** The list endpoint (`GET /records?limit=…`) strips `externalContent` from cells. You must fetch each enriched row individually with `GET /records/{recordId}` to get the full enrichment payload.

---

## Prerequisites

- Python 3.11+
- A Clay account with at least one enrichment table set up (Enrich Company or Enrich Person action column)
- Claude Desktop (or any MCP client)
- **Salesforce API access** *(optional)* — only needed if you want Claude to write enrichment results back to Salesforce directly. If you use Clay's built-in **Update Record** action instead, you can skip this entirely.

---

## Quick Start — Using with Claude Chat (no coding required)

These tools work inside **Claude Desktop** — the same chat you use every day, not just Claude Code. Once set up, just type naturally: *"Enrich accounts with blank Industry"* and Claude will call the right tool automatically.

### Step 1 — Download Claude Desktop

Download and install **Claude Desktop** (Mac or Windows) from [claude.ai/download](https://claude.ai/download). This is the desktop app, not the browser version — the browser version cannot run local tools.

### Step 2 — Install Python

Open **Terminal** (Mac: press `Cmd+Space`, type "Terminal", press Enter) and run:

```bash
# Check if Python 3.11+ is already installed
python3 --version
```

If you see `Python 3.11` or higher, skip ahead. Otherwise, download Python from [python.org/downloads](https://www.python.org/downloads/) and install it.

### Step 3 — Download and install this server

Copy and paste these commands into Terminal one at a time:

```bash
# 1. Download the code
git clone https://github.com/tungjustin07/clay-mcp.git
cd clay-mcp

# 2. Install dependencies
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

# 3. Note the full path — you'll need it in Step 5
pwd
```

The `pwd` command prints something like `/Users/yourname/clay-mcp`. Copy that output.

### Step 4 — Create your config file

```bash
mkdir -p ~/.clay-mcp
cp profiles.json.template ~/.clay-mcp/profiles.json
open ~/.clay-mcp/profiles.json
```

This opens the file in a text editor. Fill in your values (see [Configuration](#configuration) below for what each field means).

The profile key (e.g. `"myorg"`) is just a nickname — use whatever makes sense for your Salesforce org.

### Step 5 — Connect to Claude Desktop

Open this file in a text editor:

```bash
open ~/Library/Application\ Support/Claude/claude_desktop_config.json
```

Replace `/path/to/clay-mcp` with the path from Step 3's `pwd` output:

```json
{
  "mcpServers": {
    "clay-mcp": {
      "command": "/path/to/clay-mcp/.venv/bin/python3",
      "args": ["/path/to/clay-mcp/server.py"]
    }
  }
}
```

**Example** (if `pwd` showed `/Users/jane/clay-mcp`):

```json
{
  "mcpServers": {
    "clay-mcp": {
      "command": "/Users/jane/clay-mcp/.venv/bin/python3",
      "args": ["/Users/jane/clay-mcp/server.py"]
    }
  }
}
```

Save the file, then **quit and reopen Claude Desktop**. You should now see a hammer icon (🔨) in the chat input — that means the tools loaded successfully.

### Step 6 — Test it

In a Claude chat, type:

```
clay_list_profiles
```

Claude will call the tool and show your configured profiles. If it works, you're done.

---

## Full Installation

```bash
git clone https://github.com/tungjustin07/clay-mcp.git
cd clay-mcp
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
```

---

## Configuration

All config lives in **`~/.clay-mcp/profiles.json`** — one file, multiple named profiles. Nothing sensitive is stored in the repo.

### Step 1 — Get your Clay session cookie

1. Log in to [app.clay.com](https://app.clay.com)
2. Open DevTools → Application → Cookies → `app.clay.com`
3. Copy the `claysession` cookie value

### Step 2 — Create `~/.clay-mcp/profiles.json`

Copy `profiles.json.template` from this repo and fill in your values:

```bash
mkdir -p ~/.clay-mcp
cp profiles.json.template ~/.clay-mcp/profiles.json
```

Minimal working example:

```json
{
  "myorg": {
    "clay_cookie": "claysession=s%3Ayour-value-here",
    "clay_workspace_id": 12345,

    "sf_username": "you@yourorg.com",
    "sf_password": "yourpassword",
    "sf_security_token": "yourtoken",

    "account_table_id": "t_xxxxxxxx",
    "account_view_id":  "gv_xxxxxxxx",
    "account_input_columns": {
      "domain": "f_xxxxxxxx"
    },
    "account_enrichment_column_ids": ["f_xxxxxxxx"],
    "account_enrichment_subfield_map": {
      "industry":       "Industry",
      "employee_count": "NumberOfEmployees",
      "url":            "Account_Linkedin_URL__c",
      "website":        "Website",
      "description":    "Description"
    },
    "account_field_map": {
      "domain": "Domain2__c"
    },

    "contact_table_id": "t_xxxxxxxx",
    "contact_view_id":  "gv_xxxxxxxx",
    "contact_input_columns": {
      "email": "f_xxxxxxxx"
    },
    "contact_enrichment_column_ids": ["f_xxxxxxxx"],
    "contact_enrichment_subfield_map": {
      "title":       "Title",
      "linkedinUrl": "Contact_Linkedin_URL2__c"
    },
    "contact_field_map": {
      "email": "Email"
    }
  }
}
```

### Step 3 — Find your Clay field IDs

Use the discovery tools (see below) to fill in the field IDs:

```
clay_list_workspaces        →  find clay_workspace_id
clay_list_tables            →  find account_table_id, contact_table_id
clay_get_table_schema       →  find view IDs and all field IDs
clay_export_table_data      →  inspect enrichment output to discover subfield_map keys
```

### Step 4 — Register with Claude Desktop

Add to `~/Library/Application Support/Claude/claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "clay-mcp": {
      "command": "/path/to/clay-mcp/.venv/bin/python",
      "args": ["/path/to/clay-mcp/server.py"]
    }
  }
}
```

Restart Claude Desktop after any `server.py` changes.

### Step 5 — Supabase Storage (optional, recommended)

Export files and table documentation are uploaded to a **private** Supabase Storage bucket. The server generates time-limited **signed URLs** (default 7 days) so links are shareable without making the bucket public.

**Setup:**
1. [Supabase dashboard](https://app.supabase.com) → Storage → **New bucket** → name it `clay-exports` (leave Public **off**)
2. Project Settings → API → copy **Project URL** → `supabase_url`
3. Project Settings → API → copy **`service_role`** key (not `anon`) → `supabase_service_key`

Add to your profile in `~/.clay-mcp/profiles.json`:
```json
"supabase_url": "https://your-project-id.supabase.co",
"supabase_service_key": "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9...",
"supabase_bucket": "clay-exports"
```

Optionally set `"supabase_signed_url_ttl": 86400` to change the signed URL lifetime (default: 604800 = 7 days).

Once configured, `clay_export_table_data` and `clay_document_table` return `{"url": "https://...signed-url...", "expiresIn": 604800}` instead of `{"filePath": "/tmp/..."}`.

### Multiple clients

Add more profiles to the same `profiles.json`. Each profile has its own Clay cookie, Salesforce org, and table/field mappings:

```json
{
  "myorg": { ... },
  "acme_corp": {
    "clay_cookie": "claysession=s%3Adifferent-cookie",
    "sf_username": "admin@acme.com",
    ...
  }
}
```

Pass `profile="acme_corp"` to any tool to target that client.

---

## Tools Reference

### Discovery

| Tool | What it does |
|---|---|
| `clay_list_profiles` | Show all configured profiles and their setup status |
| `clay_list_workspaces` | List Clay workspaces — find your `clay_workspace_id` |
| `clay_list_tables` | List tables in a workspace or workbook |
| `clay_get_table_schema` | Full schema for a table: field names, field IDs, types, view IDs |

### Core API

| Tool | What it does |
|---|---|
| `clay_add_row` | Add a single row to any Clay table |
| `clay_run_enrichment` | Trigger enrichment columns on a view (async) |
| `clay_get_rows` | Read rows from a view with column names resolved |
| `clay_export_table_data` | Export all rows to JSON. Uploads to Supabase and returns a URL if configured; otherwise writes to `/tmp/clay-exports/` |
| `clay_delete_rows` | Delete specific rows by record ID |
| `clay_delete_all_rows` | Bulk-clear all rows in a view |
| `clay_document_table` | Generate Markdown documentation for a table (schema, hidden columns, relationships, enrichment output structure). Uploads to Supabase or `/tmp` |

### Pipeline (SFDC-specific)

| Tool | What it does |
|---|---|
| `clay_enrich_sfdc_accounts` | Full pipeline: query SFDC → enrich via Clay → write back |
| `clay_enrich_sfdc_contacts` | Same for Contacts (matched by email) |
| `clay_enrich_accounts_with_gaps` | Enrich accounts where Industry, Employees, or Website is blank |
| `clay_enrich_contacts_with_gaps` | Enrich contacts where Email and Title are both blank |
| `clay_check_stale_contacts` | Check whether contacts' current employer (per LinkedIn) still matches their SFDC Account |

---

## Use Cases

### 1. One-off account enrichment

Enrich a specific Salesforce account by ID:

```
clay_enrich_sfdc_accounts(
  profile="myorg",
  account_ids=["001xx000000xxxxAAA"]
)
```

### 2. Batch fill gaps

Enrich up to 50 accounts missing Industry, Employees, or Website, where a domain is available:

```
clay_enrich_accounts_with_gaps(profile="myorg", limit=50)
```

### 3. Custom SOQL filter

Enrich accounts in a specific segment:

```
clay_enrich_sfdc_accounts(
  profile="myorg",
  soql_filter="BillingCountry = 'AU' AND NumberOfEmployees = null AND Website != null",
  limit=100
)
```

### 4. Contact enrichment

Enrich contacts with blank Title or LinkedIn URL:

```
clay_enrich_sfdc_contacts(
  profile="myorg",
  soql_filter="Title = null AND Email != null",
  limit=50
)
```

### 5. Inspect enrichment output structure

Before configuring a new enrichment column, see exactly what sub-fields Clay returns:

```
clay_export_table_data(
  table_id="t_yourTableId",
  view_id="gv_yourViewId",
  profile="myorg"
)
```

Read the exported file (path returned in the response) to inspect `externalContent.fullValue`. The keys you find become entries in your `account_enrichment_subfield_map` or `contact_enrichment_subfield_map`.

### 6. Multi-client usage

The same Claude instance can enrich accounts for multiple Salesforce orgs:

```
clay_enrich_accounts_with_gaps(profile="myorg", limit=25)
clay_enrich_accounts_with_gaps(profile="acme_corp", limit=25)
```

### 7. Dry run (no cleanup)

Enrich and leave rows in the Clay table for manual inspection before writing to SFDC:

```
clay_enrich_sfdc_accounts(
  profile="myorg",
  account_ids=["001xx000000xxxxAAA"],
  cleanup=False
)
```

Then inspect with `clay_get_rows` or `clay_export_table_data`, and manually delete with `clay_delete_all_rows` when done.

---

## Configuration Reference

### Profile fields

| Field | Required | Description |
|---|---|---|
| `clay_cookie` | Yes* | `claysession=…` cookie from browser DevTools |
| `clay_email` + `clay_password` | Yes* | Alternative to cookie — auto-refreshes |
| `clay_workspace_id` | Yes | Numeric workspace ID from `clay_list_workspaces` |
| `sf_username` | For SFDC pipeline | Salesforce login email |
| `sf_password` | For SFDC pipeline | Salesforce password |
| `sf_security_token` | For SFDC pipeline | Salesforce security token (Setup → My Security Token) |
| `account_table_id` | For accounts | Clay table ID (e.g. `t_abc123`) |
| `account_view_id` | For accounts | Clay view ID (e.g. `gv_abc123`) |
| `account_input_columns.domain` | For accounts | Field ID of the domain/URL column Clay reads as input |
| `account_enrichment_column_ids` | For accounts | List of field IDs of enrichment action columns to trigger |
| `account_enrichment_subfield_map` | For accounts | Maps Clay `fullValue` sub-field keys → SFDC field API names |
| `account_field_map.domain` | For accounts | SFDC field to read domain from (e.g. `Domain2__c`, `Website`) |
| `contact_table_id` | For contacts | Clay table ID |
| `contact_view_id` | For contacts | Clay view ID |
| `contact_input_columns.email` | For contacts | Field ID of the email column |
| `contact_enrichment_column_ids` | For contacts | Enrichment column field IDs |
| `contact_enrichment_subfield_map` | For contacts | Maps Clay `fullValue` sub-field keys → SFDC field API names |
| `contact_field_map.email` | For contacts | SFDC field to read email from (e.g. `Email`) |

*One of `clay_cookie` or `clay_email`+`clay_password` is required.

### Enrich Company — known `fullValue` sub-fields

Discovered from live enrichment output. Use these as keys in `account_enrichment_subfield_map`:

| Sub-field | Example value | Suggested SFDC field |
|---|---|---|
| `industry` | `"IT Services and IT Consulting"` | `Industry` |
| `employee_count` | `1308` | `NumberOfEmployees` |
| `url` | `"https://www.linkedin.com/company/..."` | `Account_Linkedin_URL__c` |
| `website` | `"https://example.com"` | `Website` |
| `description` | `"Company description..."` | `Description` |
| `country` | `"US"` | `BillingCountry` |
| `name` | `"Acme Corp"` | — |
| `founded` | `2010` | — |
| `annual_revenue` | `5000000` | `AnnualRevenue` |

To discover sub-fields for your specific enrichment action or a custom action, run `clay_export_table_data` and inspect the raw output file.

---

## Known Gotchas

**`preview: False` is mandatory.** Without it, Clay's PATCH `/run` endpoint returns preview-mode data — only company name and logo. The `preview: False` flag in the request body is what forces a full enrichment run. This is the single most important thing to get right.

**List endpoint strips `externalContent`.** `GET /v3/tables/{id}/views/{viewId}/records` does not include the `externalContent.fullValue` payload. Always use `GET /v3/tables/{id}/records/{recordId}` for each row after polling to get the full data.

**`isPreview: true` is misleading.** The `metadata.isPreview` flag in a cell persists as `true` even after a successful full enrichment. Ignore it — check `externalContent.fullValue` presence and `metadata.status` (`SUCCESS`, `SUCCESS_NO_DATA`, or `ERROR_*`) instead.

**Formula columns don't work for API-added rows.** Formula columns in Clay extract from imported SFDC source data, not from enrichment action output. If you add rows via the API, formula columns will be empty. Always read directly from `externalContent.fullValue` using the subfield map.

**Clay session cookie expires.** If you use `clay_cookie`, refresh it from browser DevTools when you get 401 errors. If you use `clay_email` + `clay_password`, the server handles re-authentication and caches the session automatically.

**Not all domains are in Clay's database.** Small businesses, personal brands, and niche domains often return `SUCCESS_NO_DATA`. Expect a hit rate of ~50-70% depending on your account segment.

---

## Security

- `~/.clay-mcp/profiles.json` contains live credentials — never commit it
- `.env` (if used as legacy fallback) is excluded via `.gitignore`
- Committed files contain no secrets: `server.py`, `clay_client.py`, `requirements.txt`, `profiles.json.template`, `.env.template`

---

## File Structure

```
clay-mcp/
├── server.py               # FastMCP server — all 13 tools + pipeline logic
├── clay_client.py          # Clay API client (auth, rows, enrichment, polling)
├── requirements.txt        # Python dependencies
├── profiles.json.template  # Config template — copy to ~/.clay-mcp/profiles.json
├── .env.template           # Legacy env var fallback template
└── .gitignore              # Excludes .env, profiles.json, __pycache__

# Not in repo:
~/.clay-mcp/profiles.json        # Your actual config with credentials
~/.clay-mcp/session_cache.json   # Auto-managed auth cache (email/password flow only)
```
