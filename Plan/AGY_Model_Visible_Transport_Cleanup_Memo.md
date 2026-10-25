# AGY 模型侧内部结构名清理 — Behavior Memo

- 日期：2026-10-25
- 施工：砚（claude-opus-5-5）
- 决议：Alicia 2026-10-25 对话内拍板（删 transport 段、stdin 改纯 User/Assistant 交错、`tools:` 白名单保留）

## Goal

模型（桑德罗）不应看到 ExoCore / Runtime 的内部结构名。现状三处泄漏：

1. 首轮 stdin：`ExoCorePriorContinuity v1` 头 + `SyntheticFakePair user/assistant`、`BufferTurns`、
   `HistoricalTurn N role=…`、`CurrentUserMessage` 标签。
2. 后续轮 continuity delta：`HistoricalTurn N role=…` / `CurrentUserMessage`。
3. agent.md 末尾 `## ExoCore Continuity Transport` 段（引用上述名词 + transport/bootstrap/fingerprint/hydration）。

## Intended behavior

- stdin 只出现 `--- User ---` / `--- Assistant ---` 交错段（user 段可带 ` · <timestamp>`），最后一段 User 即当前消息。
  - fake pair 与 buffer 对齐 native 冷路径：`assistant = fake_ai + "\n\n" + buffer_turns`（同一 Assistant 段）。
  - 无版本头、无说明行。
  - 后续轮无 delta、无附件时仍为裸 `user_message`（不变）。
- agent.md 新形状 = frontmatter（name / description / `tools:` 白名单）+ system prompt + 可选项目规则，无 transport 段。
- 既有 generation 的 agent.md（任一历史工具集 + 旧 transport 尾）作为 legacy 形状被识别，并经既有升级路径
  原位重写为新形状；`SECURITY_POLICY_REVISION` bump 到 `agy-tool-perm-v7`，让 live 进程重新取声明。

## Scope

- 仅 `ExoCore-Runtime`：`providers/antigravity/renderer.py`、`adapter.py`（legacy 识别循环）、`capabilities.py`（revision）、对应测试与 `tests/fixtures/fake_agy.py`。
- 不动：ExoCore 侧 bootstrap_context 结构与 key 名（内部结构，不进模型）、`tools:` 白名单、deny/allow 策略、
  不可达的 `AdditionalBootstrapData JSON` 兜底（ExoCore 合同只允许三个 key）。

## Validation

- `python.exe -m unittest discover -s tests -v` 全量。
- 渲染单测改为新格式的正向精确断言；legacy 升级单测/集成测覆盖“旧 transport 尾 × 历史工具集”→ 新形状。
- 真实 agy 会话观察由 Alicia 做（新开会话首轮 + 已有会话续聊）。
