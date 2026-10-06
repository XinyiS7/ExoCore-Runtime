# AGY 1.3.x 兼容门禁放宽

> **状态：Alicia 已授权施工（2026-10-06）；本文即施工契约。**
> **范围：** 仅 ExoCore-Runtime 单仓；不涉及 ExoCore、不涉及通信契约、不涉及 capability 列表。
> **动因：** AGY 已自动升级到 1.3.0，现行门禁 `>=1.1.20,<1.3` 主动拒绝，Runtime 端 provider 不可用（`agy_version_unsupported`）。

---

## 0. 目标与非目标

### 0.1 目标

把兼容信封从 `>=1.1.20,<1.3` 放宽到 `>=1.1.20,<1.4`，并同时留下对应版本的实机取样证据与门禁契约测试。

### 0.2 非目标

- 不改 `RESOLVER_POLICY_REVISION` / `SECURITY_POLICY_REVISION` / `LAUNCH_ENVIRONMENT_REVISION`（本次不触碰 agent 声明、权限策略或启动环境）。
- 不改 `RUNTIME_CAPABILITIES`、wire 形状、MCP manifest 契约、附件与产物行为。
- 不因“顺手”改动 ExoCore 侧任何代码。
- 不解除 fail-closed：**1.4.x 仍必须被拒绝**，且拒绝必须发生在写 stdin 之前。

---

## 1. 放宽依据（真机 1.3.0 实测，2026-10-06）

实测方式：隔离 generation-private profile（`HOME`/`APPDATA`/`XDG_*`/`TEMP` 全部重定向），按 Runtime 生产 argv/env 直接驱动官方 `agy.exe` 1.3.0，并把原始 stdout 交给 Runtime 自己的 `parse_init` / `AgyTurnNormalizer` 解析。

| 面 | 结论 |
|---|---|
| `--version` / `models` / `/quota` 预检 | 通过；`/quota` 仍是零消耗哨兵（`num_turns=0`、usage 全零、`command.name=usage`） |
| `_parse_quota` 形状 | 仍含 `Gemini Models` 组与 `5h`/`weekly` buckets；新增的 `Claude and GPT models` 组按现有实现被忽略，不需要改代码 |
| `_parse_models` | 新模型 slug（含 claude/gpt 系列）仍匹配既有 slug 正则；`gemini-3.1-pro-high` 仍在 |
| init 契约 | `event=init`、顶层 `conversation_id`、`init.model=gemini-3.1-pro-high`；init 后到首次写 stdin 之间保持静默 |
| 文本轮 | `step_update` → `result` 解析通过；usage 五个键未变 |
| 工具轮 | `tool` `ACTIVE`→`DONE`、`tool_name=view_file`、`duration_seconds` 正常 |
| 工具失败 | `state=ERROR` → `tool_error`（changelog 的 `Errored`→`Failed` 只是显示标题，机器状态未变） |
| 同会话续聊 | `--conversation <id>` 返回同一 `conversation_id` |
| Runtime 全量确定性测试（改动前） | 319 项：318 通过 + 1 项既有 host-symlink 环境跳过 |

上游 changelog（`agy changelog`，离线权威）在 1.2.8–1.3.0 窗口内与本适配器相关的条目：1.2.16 生图改由内置 `image-generator` 子代理执行（见 §4 后续）、1.2.10 步骤标题 `Errored`→`Failed`（显示层，已实测不影响）、1.2.9 headless 等待后台任务至 `--print-timeout`（其 30 分钟上限高于本仓 180s hard timeout）、1.2.7 默认工具面退役 `find_by_name`/`grep_search`/`list_dir`（`AGENT_TOOLS` 未声明它们，且显式 `tools:` 声明实测仍被承认）。

---

## 2. 施工内容

| 文件 | 改动 |
|---|---|
| `src/exocore_runtime/providers/antigravity/process.py` | 门禁条件 `<1.3` → `<1.4`，并更新上方注释：记录 1.3.0 取样证据，保留“跨小版本必须有一次新 capture”的纪律 |
| `src/exocore_runtime/providers/antigravity/ndjson.py` | 模块 docstring 信封措辞 → `>=1.1.20,<1.4` |
| `AGENTS.md` / `README.md` | 信封与证据清单更新；README 中“1.3.x 仍 fail closed”改为“1.4.x 仍 fail closed” |
| `tests/fixtures/fake_agy.py` | 新增 `version_1_3_0`（应被接受）与 `version_1_4_0`（应被拒绝）场景；`bad_version` 依旧指向被拒绝的越界版本 |
| `tests/integration/test_antigravity_adapter.py` | 新增 1.3.0 接受用例与 1.4.0 边界拒绝用例；既有启动故障映射表保持语义（越界版本 → `agy_version_unsupported`） |
| `tests/fixtures/agy_1_3_0_success.jsonl`（新） | 1.3.0 真实回合的脱敏取样，作为后续升级的 wire 基线 |
| `tests/unit/test_antigravity_components.py` | 新增取样回放用例，锁定 1.3.0 事件投影 |

### 2.1 冻结口径

1. 信封上界是**排他小版本**：`1.3.x` 全接受，`1.4.0` 起拒绝。取样版本号不代表上界。
2. 拒绝路径不得变化：越界仍在 `models`/`/quota` 之前、写 stdin 之前 fatal。
3. 工作区中已存在的三份未提交措辞改动（`AGENTS.md`、`README.md`、`ndjson.py`，内容是把信封写成 `>=1.1.20,<1.3`）与本次改动同一话题，随本次一起收敛到 `<1.4`，不单独保留半成品措辞。
4. 取样 fixture 必须脱敏：会话 id、工作区路径、生成 id 一律替换为占位符；保留真实事件形状与字段名。

---

## 3. 验证方式

1. `python.exe -m unittest discover -s tests -v` 全绿（1 项既有环境 skip 除外）。
2. 越界拒绝用例保持红-绿语义：把门禁临时改回 `<1.3` 时，1.3.0 接受用例必须变红。
3. 改动后用**真实 agy.exe 1.3.0** 走生产代码路径（`AgyProcessConfig(require_official_executable=True)` → `AntigravityAdapter` → `ensure_generation`）确认门禁放行且 `available_model_slugs` / `quota_snapshot` 被生产解析器填充。
4. `git diff --check`。

## 4. 本次不做、留给后续（需在双端运行期间验证）

- 真实 MCP `call_mcp_tool` 步骤形状与短名投影。
- `generate_image` 产物捕获：1.2.16 改为子代理执行后，产物捕获路径必须重新取一次真机证据（已由 Alicia 指定用 preset 8 做生图测试）。
- 当前轮附件、Stop / retire、mailbox hook。
