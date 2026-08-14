#!/usr/bin/env python3
"""
Clay Enrichment MCP Server — thin tool registration shell.

All implementation logic lives in pipeline.py, which is reloaded on every
tool call. This means you can edit pipeline.py and changes take effect
immediately — no Claude Desktop restart required.

The only time a restart IS needed is when you add or remove a tool here
(i.e. add a new @mcp.tool() stub). That should be rare.

Config: ~/.clay-mcp/profiles.json  (see profiles.json.template)
"""

import importlib
import clay_client as _clay_client_module
import pipeline as _pipeline_module
from fastmcp import FastMCP

mcp = FastMCP("clay-enrichment")


def _impl():
    """Reload pipeline.py and clay_client.py so changes take effect without restarting."""
    importlib.reload(_clay_client_module)
    importlib.reload(_pipeline_module)
    return _pipeline_module


# ============================================================================
# Discovery
# ============================================================================

@mcp.tool()
def clay_list_profiles() -> str:
    """
    List configured Clay profiles and their setup status.
    Shows credentials, table IDs, and whether R2 storage is configured.
    """
    return _impl().clay_list_profiles()


@mcp.tool()
def clay_list_workspaces(profile: str = "myorg") -> str:
    """
    List all Clay workspaces accessible by this profile.
    Use this to find your clay_workspace_id for profiles.json.
    """
    return _impl().clay_list_workspaces(profile)


@mcp.tool()
def clay_list_tables(profile: str = "myorg", workspace_id: str = "0", workbook_id: str = "") -> str:
    """
    List tables and resources in a Clay workspace or inside a specific workbook.
    Use this to find your table IDs for profiles.json.

    Args:
        profile: Profile name (default: "myorg")
        workspace_id: Numeric workspace ID (uses profiles.json value if omitted)
        workbook_id: Workbook ID to drill into (e.g. "wb_0t8xif3RpNiPNs6mn8Y")
    """
    return _impl().clay_list_tables(profile, workspace_id, workbook_id)


@mcp.tool()
def clay_get_table_schema(table_id: str, profile: str = "myorg") -> str:
    """
    Get full schema for a Clay table: field names, field IDs, types, and view IDs.
    Use this to find the column IDs needed for profiles.json configuration.

    Args:
        table_id: Clay table ID (e.g. "t_abc123")
        profile: Profile name (default: "myorg")
    """
    return _impl().clay_get_table_schema(table_id, profile)


# ============================================================================
# Core API
# ============================================================================

@mcp.tool()
def clay_add_row(table_id: str, cells: dict, profile: str = "myorg") -> str:
    """
    Add a single row to a Clay table with specific field values.

    Args:
        table_id: Clay table ID (e.g. "t_abc123")
        cells: Dict mapping field IDs to values, e.g. {"f_col123": "example.com"}
        profile: Profile name (default: "myorg")
    """
    return _impl().clay_add_row(table_id, cells, profile)


@mcp.tool()
def clay_run_enrichment(
    table_id: str,
    view_id: str,
    field_ids: list[str],
    num_records: int = 0,
    profile: str = "myorg",
) -> str:
    """
    Trigger one or more enrichment columns on rows in a Clay table view.
    Enrichment runs asynchronously — use clay_get_rows or clay_export_table_data to poll.

    Args:
        table_id: Clay table ID
        view_id: View ID (get from clay_get_table_schema)
        field_ids: List of enrichment field/column IDs to run
        num_records: How many records to enrich (0 = all in view)
        profile: Profile name (default: "myorg")
    """
    return _impl().clay_run_enrichment(table_id, view_id, field_ids, num_records, profile)


@mcp.tool()
def clay_get_rows(
    table_id: str,
    view_id: str,
    columns: list[str] | None = None,
    max_rows: int = 20,
    profile: str = "myorg",
) -> str:
    """
    Read rows from a Clay table view. Returns enriched data with column names.
    For large tables, prefer clay_export_table_data which writes to file/R2.

    Args:
        table_id: Clay table ID
        view_id: View ID
        columns: Only include these column names (default: all)
        max_rows: Max rows to return (default: 20)
        profile: Profile name (default: "myorg")
    """
    return _impl().clay_get_rows(table_id, view_id, columns, max_rows, profile)


@mcp.tool()
def clay_search_tables(
    query: str,
    field_name: str = "",
    table_ids: list[str] | None = None,
    workspace_id: str = "0",
    max_rows_per_table: int = 500,
    max_results: int = 50,
    profile: str = "myorg",
) -> str:
    """
    Search for a value across all tables in a Clay workspace.

    Scans every row in every table (in parallel) and returns matches where any
    field contains the query string. Useful for finding which table a record
    lives in, or checking if a domain/email exists anywhere in Clay.

    Args:
        query: Value to search for (case-insensitive substring match)
        field_name: Only search columns with this name (default: all columns)
        table_ids: Limit search to these table IDs (default: all tables in workspace)
        workspace_id: Workspace to search (uses profiles.json value if omitted)
        max_rows_per_table: Max rows to scan per table (default: 500)
        max_results: Stop after finding this many matches (default: 50)
        profile: Profile name (default: "myorg")
    """
    return _impl().clay_search_tables(query, field_name, table_ids, workspace_id, max_rows_per_table, max_results, profile)


@mcp.tool()
def clay_export_table_data(
    table_id: str,
    view_id: str = "",
    max_rows: int = 0,
    columns: list[str] | None = None,
    profile: str = "myorg",
) -> str:
    """
    Export all rows from a Clay table to JSON. Uploads to Cloudflare R2 and
    returns a URL if R2 is configured; otherwise writes to /tmp and returns
    a file path. Returns a summary + 3 sample rows for quick inspection.

    Args:
        table_id: Clay table ID (e.g. "t_abc123")
        view_id: View ID (auto-detects first view if omitted)
        max_rows: Max rows to export (0 = all)
        columns: Only include these column names (default: all)
        profile: Profile name (default: "myorg")
    """
    return _impl().clay_export_table_data(table_id, view_id, max_rows, columns, profile)


@mcp.tool()
def clay_delete_rows(table_id: str, record_ids: list[str], profile: str = "myorg") -> str:
    """
    Delete specific rows from a Clay table by their record IDs.

    Args:
        table_id: Clay table ID
        record_ids: List of Clay record IDs to delete (from _record_id in clay_get_rows)
        profile: Profile name (default: "myorg")
    """
    return _impl().clay_delete_rows(table_id, record_ids, profile)


@mcp.tool()
def clay_delete_all_rows(table_id: str, view_id: str, profile: str = "myorg") -> str:
    """
    Delete ALL rows visible in a Clay table view (bulk clear).
    Faster than clay_delete_rows for cleaning up passthrough tables after enrichment.

    Args:
        table_id: Clay table ID
        view_id: View ID — all rows visible in this view will be deleted
        profile: Profile name (default: "myorg")
    """
    return _impl().clay_delete_all_rows(table_id, view_id, profile)


@mcp.tool()
def clay_document_table(table_id: str, profile: str = "myorg", sample_rows: int = 3) -> str:
    """
    Generate structured Markdown documentation for a Clay table.

    Fetches the full schema (including hidden columns), sample enriched rows,
    and the externalContent.fullValue structure for each enrichment action column.
    Useful for understanding complex tables before a client call.

    Uploads to Cloudflare R2 and returns a URL if R2 is configured in profiles.json;
    otherwise writes to /tmp and returns the file path.

    Args:
        table_id: Clay table ID (e.g. "t_abc123")
        profile: Profile name (default: "myorg")
        sample_rows: Number of rows to include as examples (default: 3)
    """
    return _impl().clay_document_table(table_id, profile, sample_rows)


# ============================================================================
# Pipeline (SFDC-specific)
# ============================================================================

@mcp.tool()
def clay_enrich_sfdc_accounts(
    profile: str = "myorg",
    soql_filter: str = "",
    account_ids: list[str] | None = None,
    limit: int = 50,
    cleanup: bool = True,
) -> str:
    """
    Full enrichment pipeline for Salesforce Accounts via Clay.

    Flow:
      1. Query SFDC for accounts (domain + LinkedIn fields)
      2. Add rows to Clay Account enrichment table
      3. Trigger enrichment columns
      4. Poll for results (~1-2 min), matched by domain
      5. Bulk update SFDC with enriched data
      6. Clean up Clay rows (if cleanup=True)

    Args:
        profile: Profile name (default: "myorg")
        soql_filter: SOQL WHERE clause, e.g. "NumberOfEmployees = null"
        account_ids: Explicit list of Account IDs (alternative to soql_filter)
        limit: Max accounts to enrich in one run (default: 50)
        cleanup: Delete Clay rows after enrichment (default: True)
    """
    return _impl().clay_enrich_sfdc_accounts(profile, soql_filter, account_ids, limit, cleanup)


@mcp.tool()
def clay_enrich_sfdc_contacts(
    profile: str = "myorg",
    soql_filter: str = "",
    contact_ids: list[str] | None = None,
    limit: int = 50,
    cleanup: bool = True,
) -> str:
    """
    Full enrichment pipeline for Salesforce Contacts via Clay.

    Flow:
      1. Query SFDC for contacts (email + LinkedIn fields)
      2. Add rows to Clay Contact enrichment table
      3. Trigger enrichment columns
      4. Poll for results (~1-2 min), matched by email
      5. Bulk update SFDC with enriched data
      6. Clean up Clay rows (if cleanup=True)

    Args:
        profile: Profile name (default: "myorg")
        soql_filter: SOQL WHERE clause
        contact_ids: Explicit list of Contact IDs
        limit: Max contacts to enrich (default: 50)
        cleanup: Delete Clay rows after enrichment (default: True)
    """
    return _impl().clay_enrich_sfdc_contacts(profile, soql_filter, contact_ids, limit, cleanup)


@mcp.tool()
def clay_enrich_accounts_with_gaps(
    profile: str = "myorg",
    limit: int = 50,
    cleanup: bool = True,
) -> str:
    """
    Enrich Salesforce Accounts that have blank NumberOfEmployees, Industry, or Website.
    Only targets accounts that have a domain (Domain2__c or Website) to enrich against.

    Args:
        profile: Profile name (default: "myorg")
        limit: Max accounts to enrich (default: 50)
        cleanup: Delete Clay rows after enrichment (default: True)
    """
    return _impl().clay_enrich_accounts_with_gaps(profile, limit, cleanup)


@mcp.tool()
def clay_enrich_contacts_with_gaps(
    profile: str = "myorg",
    limit: int = 50,
    cleanup: bool = True,
) -> str:
    """
    Enrich Salesforce Contacts that have both Email and Title blank.

    Args:
        profile: Profile name (default: "myorg")
        limit: Max contacts to enrich (default: 50)
        cleanup: Delete Clay rows after enrichment (default: True)
    """
    return _impl().clay_enrich_contacts_with_gaps(profile, limit, cleanup)


@mcp.tool()
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
    (per LinkedIn) still matches their SFDC Account. Returns a staleness report
    WITHOUT writing anything back to Salesforce.

    Also returns matched_account_ids (non-stale) for passing directly to
    clay_enrich_sfdc_accounts to enrich only validated Accounts.

    Args:
        profile:     Profile name (default: "myorg")
        soql_filter: SOQL WHERE clause targeting contacts to check
        contact_ids: Explicit Contact ID list (alternative to soql_filter)
        limit:       Max contacts to check per call (default: 200)
        sample_size: Max stale entries in the returned detail list (default: 50)
        cleanup:     Delete Clay rows after check (default: True)
    """
    return _impl().clay_check_stale_contacts(profile, soql_filter, contact_ids, limit, sample_size, cleanup)


@mcp.tool()
def apollo_enrich_sfdc_contacts(
    profile: str = "myorg",
    soql_filter: str = "",
    contact_ids: list[str] | None = None,
    fields: list[str] | None = None,
    limit: int = 50,
) -> str:
    """
    Enrich Salesforce Contacts directly via Apollo People Match API (no Clay required).

    Calls Apollo's /v1/people/match for each contact and writes Phone, Title,
    and/or Email back to SFDC — only filling fields that are currently blank.

    Args:
        profile:      Profile name (default: "myorg")
        soql_filter:  SOQL WHERE clause, e.g. "Account.Type = 'Customer'"
        contact_ids:  Explicit list of Contact IDs (alternative to soql_filter)
        fields:       SFDC fields to fill — default ["Phone", "Title", "Email"]
        limit:        Max contacts to enrich per run (default: 50)
    """
    return _impl().apollo_enrich_sfdc_contacts(profile, soql_filter, contact_ids, fields, limit)


@mcp.tool()
def apollo_enrich_sfdc_accounts(
    profile: str = "myorg",
    soql_filter: str = "",
    account_ids: list[str] | None = None,
    fields: list[str] | None = None,
    limit: int = 50,
) -> str:
    """
    Enrich Salesforce Accounts directly via Apollo Organization Enrich API (no Clay required).

    Calls Apollo's /v1/organizations/enrich for each account (by domain) and writes
    NumberOfEmployees, Industry, and/or Description back to SFDC — only filling fields
    that are currently blank.

    Args:
        profile:      Profile name (default: "myorg")
        soql_filter:  SOQL WHERE clause, e.g. "Type = 'Customer'"
        account_ids:  Explicit list of Account IDs (alternative to soql_filter)
        fields:       SFDC fields to fill — default ["NumberOfEmployees", "Industry", "Description"]
        limit:        Max accounts to enrich per run (default: 50)
    """
    return _impl().apollo_enrich_sfdc_accounts(profile, soql_filter, account_ids, fields, limit)


@mcp.tool()
def clay_phone_waterfall_sfdc_contacts(
    profile: str = "myorg",
    soql_filter: str = "",
    contact_ids: list[str] | None = None,
    limit: int = 50,
    cleanup: bool = True,
) -> str:
    """
    Enrich Salesforce Contacts with missing Phone via Clay's Phone Waterfall (no Apollo).

    Triggers the built-in Phone Waterfall action column (~9 providers: Kaspr, Lusha, Wiza,
    PDL, Prospeo, etc.) and writes results back to Contact.Phone in SFDC.

    Args:
        profile:      Profile name (default: "myorg")
        soql_filter:  SOQL WHERE clause, e.g. "Account.Type = 'Customer' AND Phone = null"
        contact_ids:  Explicit list of Contact IDs (alternative to soql_filter)
        limit:        Max contacts per run (default: 50)
        cleanup:      Delete Clay rows after enrichment (default: True)
    """
    return _impl().clay_phone_waterfall_sfdc_contacts(profile, soql_filter, contact_ids, limit, cleanup)


if __name__ == "__main__":
    mcp.run()
