"""Frozen MCP identity facts shared by the AGY renderer and NDJSON normalizer.

The Runtime exposes exactly one generation-private MCP server, and AGY hides a
Memory tool's real identity inside its generic ``call_mcp_tool`` dispatcher
(``tool_info.parameters``). Both facts live here as a minimal shared source so
the renderer's scoped allow policy and the normalizer's display projection read
the same values and no other module keeps a second copy of the server literal.

``MCP_TOOL_DISPLAY_ALLOWLIST`` mirrors
``ExoCore/engines/mcp/servers/memory/schema.py::RUNTIME_BINDING_TOOL_NAMES``
(same names, same order). It is a *display* allowlist and never an execution
permission source: execution authority stays with the ExoCore Memory MCP server
(its ``RUNTIME_BINDING_TOOL_NAMES`` declaration and ToolExecutor), not with this
Runtime. A future tool that becomes executable still needs an explicit update
here before its name may be projected into a tool lifecycle frame.
"""

from __future__ import annotations

MCP_SERVER_NAME = "exocore-memory"

MCP_TOOL_DISPLAY_ALLOWLIST: tuple[str, ...] = (
    "register",
    "memory_plasmid",
    "chronicle",
    "memory_search",
    "private_log",
)
