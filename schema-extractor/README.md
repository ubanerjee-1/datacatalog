# Databricks Schema Extractor

A lightweight tool to extract table and schema metadata from multiple Databricks workspaces.

## Prerequisites

- **Python 3.8 or higher**
  - Check your version: `python --version` or `python3 --version`
  - Download from: https://www.python.org/downloads/

## Quick Start

### 1. Install Dependencies

**Windows (Command Prompt or PowerShell):**
```cmd
pip install -r requirements.txt
```

**Mac/Linux (Terminal):**
```bash
pip3 install -r requirements.txt
```

### 2. Edit Workspace List

Open `workspaces.txt` and add your Databricks workspace URLs (one per line):

```
https://adb-1234567890123456.7.azuredatabricks.net
https://adb-2345678901234567.8.azuredatabricks.net
```

Lines starting with `#` are treated as comments and ignored.

### 3. Run the Script

**Windows:**
```cmd
python extract_schemas.py
```

**Mac/Linux:**
```bash
python3 extract_schemas.py
```

## How Authentication Works

For each workspace, the script will:

1. **Check for existing credentials** - If you've previously authenticated to this workspace using the Databricks CLI, those credentials will be reused automatically.

2. **OAuth browser login** - If no existing credentials are found, a browser window will open asking you to log in to Databricks. After logging in, the script will continue automatically.

You only need to authenticate once per workspace. Credentials are stored securely in your home directory.

## Output Files

All output files are saved to the `output/` folder:

| File | Description |
|------|-------------|
| `{workspace_id}_schemas.csv` | Schemas from a single workspace |
| `{workspace_id}_tables.csv` | Tables from a single workspace |
| `all_schemas.csv` | All schemas consolidated with `workspace_url` column |
| `all_tables.csv` | All tables consolidated with `workspace_url` column |

## Troubleshooting

### "No SQL warehouses found"

The script requires at least one SQL Warehouse in your workspace. Ask your Databricks admin to:
- Create a SQL Warehouse, OR
- Grant you access to an existing one

### "databricks-sdk not installed"

Run the install command again:
```
pip install -r requirements.txt
```

Or install directly:
```
pip install databricks-sdk
```

### Browser doesn't open for authentication

Try running the script from a regular terminal (not inside an IDE). Make sure you have a default browser configured on your system.

### "Permission denied" errors

You need appropriate permissions in the Databricks workspace:
- Access to `SYSTEM.INFORMATION_SCHEMA`
- Access to at least one SQL Warehouse

### Script exits with no output

Check that:
1. `workspaces.txt` contains valid URLs
2. URLs are not commented out (starting with `#`)
3. You have network access to the workspaces

## What Data is Extracted?

### INFORMATION_SCHEMA.SCHEMATA
- All schemas (databases) in Unity Catalog
- Excludes system catalogs

### INFORMATION_SCHEMA.TABLES
- All tables and views in Unity Catalog
- Includes metadata like table type, owner, creation time
- Excludes system tables

## Support

If you encounter issues, please share:
1. The error message from the terminal
2. Your Python version (`python --version`)
3. Your operating system (Windows/Mac)
