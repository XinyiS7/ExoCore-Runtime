"""Frozen MCP identity and tool-surface facts shared by the AGY renderer, the
generation mcp-config renderer, and the NDJSON normalizer.

The Runtime exposes exactly one generation-private MCP server, AGY hides a
Memory tool's real identity inside its generic ``call_mcp_tool`` dispatcher
(``tool_info.parameters``), and an eager tool surfaces under a derived
``mcp_<server>_<tool>`` name. All of those facts live here as one minimal shared
source so no other module keeps a second copy of the server literal or of a
tool-name list.

``MCP_ENABLED_TOOL_NAMES`` mirrors
``ExoCore/engines/mcp/servers/memory/schema.py::RUNTIME_BINDING_TOOL_NAMES``
(same names, same order); ``MCP_EAGER_TOOL_NAMES`` is the subset the generated
``mcp_config.json`` marks ``eager`` (schema in context, one-level call) so the
model does not need a descriptor ``view_file`` round for its core tools. Both
tuples are *visibility and display* facts and never an execution permission
source: execution authority stays with the ExoCore Memory MCP server (its
``RUNTIME_BINDING_TOOL_NAMES`` declaration and ToolExecutor), not with this
Runtime. A future tool that becomes executable still needs an explicit update
here before its name may be projected into a tool lifecycle frame.

Three consumers derive from these two tuples - never write a second name list:

1. ``adapter.AntigravityAdapter._expected_mcp_config`` -> ``enabledTools`` plus
   one ``tools.<name>.eager`` entry per eager name;
2. the lazy display projection of ``call_mcp_tool`` in ``ndjson``;
3. the eager display projection of ``mcp_<server>_<tool>`` in ``ndjson``.

改动指南（Change Guide）
------------------------
- ``mcp_config.json`` 条目字段必须按 AGY 实测 schema 写。字段名或类型写错**不会**
  报错：该 server 会被整条丢弃，只在
  ``<profile>/.gemini/antigravity-cli/log/cli-*.log`` 留一条 E 级警告，模型侧
  只表现为「这个 MCP 服务器不见了」。改本文件或 ``_expected_mcp_config`` 前先跑
  ``ExoCore/Plan/Acceptance_Probes/agy_mcp_entry_schema_probe.py``（零 token：
  真实连接 + descriptor 物化 + schema oracle）。
- ``enabledTools`` 是**模型可见性过滤**，不是安全边界：该 server 仍会连接、
  descriptor 仍会物化，真正执行授权仍在 ExoCore 侧（白名单 + ``mcp(server/tool)``）。
- ``tools.<name>.eager`` 改变**线上形状**：eager 调用的 ``tool_info.parameters``
  是裸参数（无 ``ServerName``/``ToolName``/``Arguments`` 包装），调用名是
  ``mcp_<server>_<tool>``。本 Runtime 目前只消费 ``tool_name``；未来做参数或结果
  展示时不得按 lazy 形状解析。
- 改动这些常量会改变 generation 的 ``execution_options``（见
  ``capabilities.SECURITY_POLICY_REVISION``）：生效需要一次 AGY 进程 dispose +
  respawn，provider session 仍按 ``--conversation`` 恢复，且 ``mcp_config`` 不属于
  generation identity（旧 generation 会被当前 canonical 值自愈，不触发 rebase）。
"""

from __future__ import annotations

MCP_SERVER_NAME = "exocore-memory"

MCP_ENABLED_TOOL_NAMES: tuple[str, ...] = (
    "register",
    "memory_plasmid",
    "chronicle",
    "memory_search",
    "private_log",
    "schedule_wakeup",
    "heartbeat_policy",
    "use_skill",
    "trace_self",
)

MCP_EAGER_TOOL_NAMES: tuple[str, ...] = (
    "register",
    "memory_plasmid",
    "chronicle",
    "memory_search",
    "private_log",
)
