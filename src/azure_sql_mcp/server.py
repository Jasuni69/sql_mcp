import json
import logging
import os
import struct
import subprocess
from contextlib import contextmanager
from typing import Any

import pyodbc
from mcp import types
from mcp.server import Server
from mcp.server.stdio import stdio_server

logger = logging.getLogger(__name__)


def _get_az_token() -> str:
    result = subprocess.run(
        ["az", "account", "get-access-token", "--resource", "https://database.windows.net", "--query", "accessToken", "-o", "tsv"],
        capture_output=True, text=True, check=True,
    )
    return result.stdout.strip()


def _build_conn_str() -> str:
    server = os.environ["AZURE_SQL_SERVER"]
    database = os.environ["AZURE_SQL_DATABASE"]
    trust_cert = os.environ.get("AZURE_SQL_TRUST_CERT", "no").lower()
    return (
        f"DRIVER={{ODBC Driver 18 for SQL Server}};"
        f"SERVER={server};"
        f"DATABASE={database};"
        f"Encrypt=yes;"
        f"TrustServerCertificate={trust_cert};"
    )


@contextmanager
def get_connection():
    auth = os.environ.get("AZURE_SQL_AUTH", "sql").lower()
    conn_str = _build_conn_str()
    conn = None
    try:
        if auth == "az_cli":
            token = _get_az_token()
            token_bytes = token.encode("utf-16-le")
            token_struct = struct.pack(f"<I{len(token_bytes)}s", len(token_bytes), token_bytes)
            conn = pyodbc.connect(conn_str, attrs_before={1256: token_struct})
        else:
            user = os.environ["AZURE_SQL_USER"]
            password = os.environ["AZURE_SQL_PASSWORD"]
            conn = pyodbc.connect(conn_str + f"UID={user};PWD={password};")
        yield conn
    finally:
        if conn:
            conn.close()


def _rows_to_text(cursor: pyodbc.Cursor) -> str:
    columns = [desc[0] for desc in cursor.description]
    rows = cursor.fetchall()
    if not rows:
        return "(no rows)"
    col_widths = [max(len(col), max((len(str(row[i])) for row in rows), default=0)) for i, col in enumerate(columns)]
    sep = "+-" + "-+-".join("-" * w for w in col_widths) + "-+"
    header = "| " + " | ".join(col.ljust(col_widths[i]) for i, col in enumerate(columns)) + " |"
    lines = [sep, header, sep]
    for row in rows:
        lines.append("| " + " | ".join(str(v).ljust(col_widths[i]) if v is not None else "NULL".ljust(col_widths[i]) for i, v in enumerate(row)) + " |")
    lines.append(sep)
    lines.append(f"({len(rows)} row{'s' if len(rows) != 1 else ''})")
    return "\n".join(lines)


app = Server("azure-sql-mcp")


@app.list_resources()
async def list_resources() -> list[types.Resource]:
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT TABLE_SCHEMA, TABLE_NAME
            FROM INFORMATION_SCHEMA.TABLES
            WHERE TABLE_TYPE = 'BASE TABLE'
            ORDER BY TABLE_SCHEMA, TABLE_NAME
        """)
        return [
            types.Resource(
                uri=f"mssql://{row[0]}.{row[1]}",
                name=f"{row[0]}.{row[1]}",
                description=f"Table {row[0]}.{row[1]}",
                mimeType="text/plain",
            )
            for row in cursor.fetchall()
        ]


@app.read_resource()
async def read_resource(uri: types.AnyUrl) -> str:
    path = str(uri).replace("mssql://", "")
    parts = path.split(".", 1)
    if len(parts) != 2:
        raise ValueError(f"Invalid resource URI: {uri}")
    schema, table = parts
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(f"SELECT TOP 100 * FROM [{schema}].[{table}]")
        return _rows_to_text(cursor)


@app.list_tools()
async def list_tools() -> list[types.Tool]:
    return [
        types.Tool(
            name="list_schemas",
            description="List all schemas in the database",
            inputSchema={"type": "object", "properties": {}},
        ),
        types.Tool(
            name="list_tables",
            description="List all tables, optionally filtered by schema",
            inputSchema={
                "type": "object",
                "properties": {
                    "schema": {"type": "string", "description": "Schema name to filter by (optional)"},
                },
            },
        ),
        types.Tool(
            name="describe_table",
            description="Get columns, data types, nullability, and primary keys for a table",
            inputSchema={
                "type": "object",
                "properties": {
                    "schema": {"type": "string", "description": "Schema name"},
                    "table": {"type": "string", "description": "Table name"},
                },
                "required": ["schema", "table"],
            },
        ),
        types.Tool(
            name="sample_table",
            description="Get the first N rows from a table (default 50, max 500)",
            inputSchema={
                "type": "object",
                "properties": {
                    "schema": {"type": "string", "description": "Schema name"},
                    "table": {"type": "string", "description": "Table name"},
                    "rows": {"type": "integer", "description": "Number of rows to return (default 50, max 500)"},
                },
                "required": ["schema", "table"],
            },
        ),
        types.Tool(
            name="execute_query",
            description="Execute a SQL query against the database",
            inputSchema={
                "type": "object",
                "properties": {
                    "sql": {"type": "string", "description": "SQL query to execute"},
                },
                "required": ["sql"],
            },
        ),
    ]


@app.call_tool()
async def call_tool(name: str, arguments: dict[str, Any]) -> list[types.TextContent]:
    with get_connection() as conn:
        cursor = conn.cursor()

        if name == "list_schemas":
            cursor.execute("SELECT SCHEMA_NAME FROM INFORMATION_SCHEMA.SCHEMATA ORDER BY SCHEMA_NAME")
            schemas = [row[0] for row in cursor.fetchall()]
            output = "\n".join(schemas)

        elif name == "list_tables":
            schema = arguments.get("schema")
            if schema:
                cursor.execute("""
                    SELECT TABLE_SCHEMA, TABLE_NAME
                    FROM INFORMATION_SCHEMA.TABLES
                    WHERE TABLE_TYPE = 'BASE TABLE' AND TABLE_SCHEMA = ?
                    ORDER BY TABLE_NAME
                """, schema)
            else:
                cursor.execute("""
                    SELECT TABLE_SCHEMA, TABLE_NAME
                    FROM INFORMATION_SCHEMA.TABLES
                    WHERE TABLE_TYPE = 'BASE TABLE'
                    ORDER BY TABLE_SCHEMA, TABLE_NAME
                """)
            tables = cursor.fetchall()
            output = "\n".join(f"{row[0]}.{row[1]}" for row in tables)
            output += f"\n\n({len(tables)} tables)"

        elif name == "describe_table":
            schema = arguments["schema"]
            table = arguments["table"]
            cursor.execute("""
                SELECT
                    c.COLUMN_NAME,
                    c.DATA_TYPE,
                    COALESCE(CAST(c.CHARACTER_MAXIMUM_LENGTH AS VARCHAR), CAST(c.NUMERIC_PRECISION AS VARCHAR) + ',' + CAST(c.NUMERIC_SCALE AS VARCHAR), '') AS SIZE,
                    c.IS_NULLABLE,
                    CASE WHEN pk.COLUMN_NAME IS NOT NULL THEN 'YES' ELSE 'NO' END AS IS_PK
                FROM INFORMATION_SCHEMA.COLUMNS c
                LEFT JOIN (
                    SELECT ku.COLUMN_NAME
                    FROM INFORMATION_SCHEMA.TABLE_CONSTRAINTS tc
                    JOIN INFORMATION_SCHEMA.KEY_COLUMN_USAGE ku
                        ON tc.CONSTRAINT_NAME = ku.CONSTRAINT_NAME
                        AND tc.TABLE_SCHEMA = ku.TABLE_SCHEMA
                        AND tc.TABLE_NAME = ku.TABLE_NAME
                    WHERE tc.CONSTRAINT_TYPE = 'PRIMARY KEY'
                      AND tc.TABLE_SCHEMA = ?
                      AND tc.TABLE_NAME = ?
                ) pk ON c.COLUMN_NAME = pk.COLUMN_NAME
                WHERE c.TABLE_SCHEMA = ? AND c.TABLE_NAME = ?
                ORDER BY c.ORDINAL_POSITION
            """, schema, table, schema, table)
            output = _rows_to_text(cursor)

        elif name == "sample_table":
            schema = arguments["schema"]
            table = arguments["table"]
            n = min(int(arguments.get("rows", 50)), 500)
            cursor.execute(f"SELECT TOP {n} * FROM [{schema}].[{table}]")
            output = _rows_to_text(cursor)

        elif name == "execute_query":
            cursor.execute(arguments["sql"])
            if cursor.description:
                output = _rows_to_text(cursor)
            else:
                output = f"{cursor.rowcount} row(s) affected"

        else:
            raise ValueError(f"Unknown tool: {name}")

        return [types.TextContent(type="text", text=output)]


async def main() -> None:
    logging.basicConfig(level=logging.INFO)
    async with stdio_server() as (read_stream, write_stream):
        await app.run(read_stream, write_stream, app.create_initialization_options())
