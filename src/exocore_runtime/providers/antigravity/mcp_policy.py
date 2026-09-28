"""Runtime-owned identity for the generation-private ExoCore MCP server.

The exposed tool names and eager policy are supplied by ExoCore in each strict
``TurnRequest.runtime_mcp_tools`` manifest. Runtime deliberately keeps no
mirror of that tool surface. Execution authority remains in ExoCore's MCP
server; this module owns only the stable AGY server identity.
"""

from __future__ import annotations

MCP_SERVER_NAME = "exocore-memory"
