"""The MCP endpoint at ``/mcp`` (package 10, ``MCP_ENABLED``).

Nothing here imports ``fastmcp`` at module level except the modules that ``build_mcp`` imports when
it runs, so a deployment with ``MCP_ENABLED=false`` never loads it.
"""
