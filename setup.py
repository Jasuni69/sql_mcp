"""Setup script for Azure SQL MCP server."""
import json
import subprocess
import sys
from pathlib import Path


def main():
    print("=== Azure SQL MCP Setup ===\n")

    # Install dependencies
    print("Installing package...")
    subprocess.run([sys.executable, "-m", "pip", "install", "."], check=True)
    print()

    # Auth type
    auth = ""
    while auth not in ("sql", "az_cli"):
        auth = input("Auth type (sql / az_cli): ").strip().lower()

    # Server and database
    server = input("Server (e.g. myserver.database.windows.net): ").strip()
    database = input("Database name: ").strip()

    env = {
        "AZURE_SQL_AUTH": auth,
        "AZURE_SQL_SERVER": server,
        "AZURE_SQL_DATABASE": database,
    }

    if auth == "sql":
        env["AZURE_SQL_USER"] = input("Username: ").strip()
        env["AZURE_SQL_PASSWORD"] = input("Password: ").strip()

    # Write .mcp.json
    config = {
        "mcpServers": {
            "azure-sql-mcp": {
                "command": "python",
                "args": ["-m", "azure_sql_mcp"],
                "env": env,
            }
        }
    }

    mcp_path = Path(__file__).parent / ".mcp.json"
    mcp_path.write_text(json.dumps(config, indent=2))
    print(f"\nWrote {mcp_path}")
    print("Done. Restart Claude Code to pick up the MCP server.")


if __name__ == "__main__":
    main()
