from typing import Any

from mcp.server.mcpserver import MCPServer

from medical_ai.config import get_settings
from medical_ai.mcp_server import tools

mcp = MCPServer("medical-ai-platform")


@mcp.tool()
def get_database_schema() -> dict[str, Any]:
    """Return the allowlisted inpatient schema and allowed operations."""

    return tools.get_database_schema()


@mcp.tool()
def get_distinct_values(table: str, field: str, limit: int = 100) -> dict[str, Any]:
    """Return distinct values for an allowlisted field, capped by configuration."""

    return tools.get_distinct_values(table=table, field=field, limit=limit)


@mcp.tool()
def query_medical_data(query_spec: dict[str, Any]) -> dict[str, Any]:
    """Run a validated structured QuerySpec against MySQL."""

    return tools.query_medical_data(query_spec=query_spec)


def main() -> None:
    settings = get_settings()
    if not settings.mcp_allow_unscoped_tools:
        raise RuntimeError(
            "MCP tools are disabled until an authenticated AccessContext adapter is configured"
        )
    if settings.mcp_transport == "stdio":
        mcp.run()
        return

    if settings.mcp_transport == "streamable-http":
        mcp.run(
            transport="streamable-http",
            host=settings.mcp_host,
            port=settings.mcp_port,
            json_response=True,
            stateless_http=True,
        )
        return

    mcp.run(transport="sse", host=settings.mcp_host, port=settings.mcp_port)


if __name__ == "__main__":
    main()
