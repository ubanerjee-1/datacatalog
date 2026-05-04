#!/usr/bin/env python3
"""
Databricks Schema Extractor

Extracts SYSTEM.INFORMATION_SCHEMA.TABLES and SCHEMAS from multiple
Databricks workspaces and consolidates them into CSV files.

Usage:
    python extract_schemas.py [workspaces.txt]
"""

import csv
import os
import re
import sys
from pathlib import Path
from datetime import datetime

def check_prerequisites():
    """Check that all prerequisites are met."""
    errors = []
    
    # Check Python version
    if sys.version_info < (3, 8):
        errors.append(f"Python 3.8+ required. Found: {sys.version}")
    
    # Check databricks-sdk is installed
    try:
        import databricks.sdk
    except ImportError:
        errors.append(
            "databricks-sdk not installed. Run: pip install databricks-sdk"
        )
    
    if errors:
        print("Prerequisite check failed:")
        for err in errors:
            print(f"  - {err}")
        sys.exit(1)
    
    print("Prerequisites OK")

def get_workspace_id(url: str) -> str:
    """Extract workspace ID from URL like https://adb-1234567890123456.7.azuredatabricks.net"""
    match = re.search(r'adb-(\d+)', url)
    if match:
        return f"adb-{match.group(1)}"
    # Fallback: use hostname
    return url.replace('https://', '').replace('http://', '').split('.')[0]

def find_profile_by_host(workspace_url: str) -> str:
    """Find an existing profile that matches the workspace URL."""
    import configparser
    
    config_path = Path.home() / ".databrickscfg"
    if not config_path.exists():
        return None
    
    # Normalize the URL for comparison
    workspace_url = workspace_url.rstrip('/').lower()
    if not workspace_url.startswith('https://'):
        workspace_url = f"https://{workspace_url}"
    
    config = configparser.ConfigParser()
    config.read(config_path)
    
    for section in config.sections():
        if 'host' in config[section]:
            host = config[section]['host'].rstrip('/').lower()
            if not host.startswith('https://'):
                host = f"https://{host}"
            if host == workspace_url:
                return section
    
    return None

def get_workspace_client(workspace_url: str):
    """Get a Databricks WorkspaceClient, using existing profile or OAuth."""
    from databricks.sdk import WorkspaceClient
    from databricks.sdk.config import Config
    
    workspace_url = workspace_url.rstrip('/')
    
    # Check for existing profile by host
    profile_name = find_profile_by_host(workspace_url)
    
    if profile_name:
        print(f"  Using existing profile: {profile_name}")
        return WorkspaceClient(profile=profile_name)
    else:
        print(f"  No profile found. Initiating OAuth login...")
        print(f"  A browser window will open for authentication.")
        # Use OAuth U2M (user-to-machine) flow
        config = Config(
            host=workspace_url,
            auth_type="external-browser"
        )
        return WorkspaceClient(config=config)

def execute_sql(client, sql: str) -> list:
    """Execute SQL and return results as list of dicts."""
    from databricks.sdk.service.sql import StatementState
    
    # Get a SQL warehouse
    warehouses = list(client.warehouses.list())
    if not warehouses:
        raise Exception("No SQL warehouses found in this workspace")
    
    # Prefer a running warehouse
    warehouse = None
    for wh in warehouses:
        if wh.state and wh.state.value == "RUNNING":
            warehouse = wh
            break
    if not warehouse:
        warehouse = warehouses[0]
    
    print(f"  Using warehouse: {warehouse.name}")
    
    # Execute statement (wait_timeout must be 0 or 5-50 seconds)
    response = client.statement_execution.execute_statement(
        warehouse_id=warehouse.id,
        statement=sql,
        wait_timeout="50s"
    )
    
    if response.status.state != StatementState.SUCCEEDED:
        raise Exception(f"Query failed: {response.status.error}")
    
    # Convert to list of dicts
    if not response.manifest or not response.result:
        return []
    
    columns = [col.name for col in response.manifest.schema.columns]
    rows = []
    for row_data in response.result.data_array or []:
        rows.append(dict(zip(columns, row_data)))
    
    return rows

def extract_from_workspace(workspace_url: str, output_dir: Path) -> tuple:
    """Extract tables and schemas from a single workspace."""
    workspace_url = workspace_url.strip()
    if not workspace_url:
        return None, None
    
    workspace_id = get_workspace_id(workspace_url)
    
    print(f"\nProcessing: {workspace_url}")
    print(f"  Workspace ID: {workspace_id}")
    
    try:
        client = get_workspace_client(workspace_url)
        
        # Query SCHEMAS
        print("  Querying INFORMATION_SCHEMA.SCHEMATA...")
        schemas_sql = """
        SELECT * FROM SYSTEM.INFORMATION_SCHEMA.SCHEMATA
        WHERE catalog_name NOT IN ('system', '__databricks_internal')
        """
        schemas = execute_sql(client, schemas_sql)
        print(f"  Found {len(schemas)} schemas")
        
        # Query TABLES
        print("  Querying INFORMATION_SCHEMA.TABLES...")
        tables_sql = """
        SELECT * FROM SYSTEM.INFORMATION_SCHEMA.TABLES
        WHERE table_catalog NOT IN ('system', '__databricks_internal')
        """
        tables = execute_sql(client, tables_sql)
        print(f"  Found {len(tables)} tables")
        
        # Add workspace column
        for row in schemas:
            row['workspace_url'] = workspace_url
        for row in tables:
            row['workspace_url'] = workspace_url
        
        # Save per-workspace CSVs
        if schemas:
            schemas_file = output_dir / f"{workspace_id}_schemas.csv"
            save_csv(schemas, schemas_file)
            print(f"  Saved: {schemas_file.name}")
        
        if tables:
            tables_file = output_dir / f"{workspace_id}_tables.csv"
            save_csv(tables, tables_file)
            print(f"  Saved: {tables_file.name}")
        
        return schemas, tables
        
    except Exception as e:
        print(f"  ERROR: {e}")
        return None, None

def save_csv(data: list, filepath: Path):
    """Save list of dicts to CSV."""
    if not data:
        return
    
    fieldnames = list(data[0].keys())
    with open(filepath, 'w', newline='', encoding='utf-8') as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(data)

def consolidate_results(all_schemas: list, all_tables: list, output_dir: Path):
    """Save consolidated CSV files."""
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    
    if all_schemas:
        # Move workspace_url to first column
        fieldnames = ['workspace_url'] + [k for k in all_schemas[0].keys() if k != 'workspace_url']
        filepath = output_dir / f"all_schemas.csv"
        with open(filepath, 'w', newline='', encoding='utf-8') as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            writer.writeheader()
            writer.writerows(all_schemas)
        print(f"\nConsolidated schemas: {filepath} ({len(all_schemas)} rows)")
    
    if all_tables:
        fieldnames = ['workspace_url'] + [k for k in all_tables[0].keys() if k != 'workspace_url']
        filepath = output_dir / f"all_tables.csv"
        with open(filepath, 'w', newline='', encoding='utf-8') as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            writer.writeheader()
            writer.writerows(all_tables)
        print(f"Consolidated tables: {filepath} ({len(all_tables)} rows)")

def main():
    print("=" * 60)
    print("Databricks Schema Extractor")
    print("=" * 60)
    
    # Check prerequisites
    check_prerequisites()
    
    # Determine input file
    script_dir = Path(__file__).parent
    input_file = Path(sys.argv[1]) if len(sys.argv) > 1 else script_dir / "workspaces.txt"
    
    if not input_file.exists():
        print(f"\nError: Input file not found: {input_file}")
        print("Create a workspaces.txt file with one workspace URL per line.")
        sys.exit(1)
    
    # Read workspace URLs
    workspaces = [line.strip() for line in input_file.read_text().splitlines() 
                  if line.strip() and not line.strip().startswith('#')]
    
    if not workspaces:
        print(f"\nError: No workspace URLs found in {input_file}")
        sys.exit(1)
    
    print(f"\nFound {len(workspaces)} workspace(s) to process")
    
    # Setup output directory
    output_dir = script_dir / "output"
    output_dir.mkdir(exist_ok=True)
    
    # Process each workspace
    all_schemas = []
    all_tables = []
    success_count = 0
    
    for workspace_url in workspaces:
        schemas, tables = extract_from_workspace(workspace_url, output_dir)
        if schemas is not None:
            all_schemas.extend(schemas)
            success_count += 1
        if tables is not None:
            all_tables.extend(tables)
    
    # Consolidate results
    if all_schemas or all_tables:
        consolidate_results(all_schemas, all_tables, output_dir)
    
    # Summary
    print("\n" + "=" * 60)
    print("Summary")
    print("=" * 60)
    print(f"Workspaces processed: {success_count}/{len(workspaces)}")
    print(f"Total schemas: {len(all_schemas)}")
    print(f"Total tables: {len(all_tables)}")
    print(f"Output directory: {output_dir}")

if __name__ == "__main__":
    main()
