# AGY 1.4.x 兼容门禁放宽

> Issue: XinyiS7/ExoCore#64（P1 · area:runtime · bug）
> **状态：已被 Alicia 决定取代（2026-10-16）：不做 §2 真机取样与新 fixture/回放测试；只放宽门禁到 `<1.5` 并把 `bad_version` 改戳 1.5.0，真实功能验证由 Alicia 自己在现有会话中手动完成；agent 不建临时 G045 会话、消息或数据库数据，不做 capture / probe / 真实 G045 回合。** 下文保留为 R2 原稿。
> **范围：** 仅 ExoCore-Runtime 单仓；不涉及 ExoCore / Desktop、wire 形状、`RUNTIME_CAPABILITIES`、Argon。
> **动因：** 官方 AGY 已自动升级到 1.4.0，现行门禁 `>=1.1.20,<1.4` 在 preflight 拒绝（`agy_version_unsupported`），Endpoint 7（`gemini-3.8-flash` → `gemini-3.8-flash-high`）不可用。Alicia 要求先恢复现有 Gemini 3.8 在 AGY 上可用。
> **前例：** `Plan/AGY_1_3_Compatibility_Gate_Plan.md`（1.3 放宽，Runtime `97fc5dd`）；本文不修改该文件。

---

## 0. 目标与非目标

### 0.1 目标

1. 在隔离 profile 下对官方 `agy.exe` 1.4.0 做真机取样，覆盖 preflight（version / models / `/quota`）与四类回合（text / tool / tool-failure / same-session resume），全部经 Runtime 自己的生产解析器回放。
2. 取样证据成立后，把信封从 `>=1.1.20,<1.4` 放宽到 `>=1.1.20,<1.5`，并留下脱敏 fixture、回放测试与门禁契约测试。

### 0.2 非目标

- 不改 `RESOLVER_POLICY_REVISION` / `SECURITY_POLICY_REVISION` / `LAUNCH_ENVIRONMENT_REVISION`、agent 声明、deny policy、启动 env、argv。
- 不改 `RUNTIME_CAPABILITIES`、wire 形状、MCP manifest、附件、产物、Stop/retire、mailbox 行为。
- 不碰 ExoCore / Desktop 任何文件；不动 pane 8 后台、不重启正在运行的 Runtime（生效重启由 Alicia 用 `run-runtime` 决定）。
- 不解除 fail-closed：**1.5.x 仍必须被拒绝**，且拒绝发生在 `models` / `/quota` 之前、写 stdin 之前。
- 不为“取样未覆盖”的面（真实 MCP 步骤、`generate_image` 子代理产物、附件、Stop）宣称兼容；它们留在 §6。

### 0.3 停止条件（取样阶段发现任一项即停工回报，不放宽门禁）

- headless stdout 不再是纯 NDJSON（例如出现 1.4.0 新增 OSC 7501 转义序列）；
- init 契约或 `step_update` / `tool` / `result` 的已知事件语义变化，导致生产解析器报错或已知投影变化；
- 取样中既有 usage 五键（`input_tokens` / `output_tokens` / `thinking_tokens` / `cache_read_tokens` / `total_tokens`）缺失，或其类型/语义不再可投影（上游新增的额外键本身不构成失败：现 parser 忽略未知键）；
- `/quota` 不再是零消耗哨兵，或 `_parse_quota` / `_parse_models` 解析失败；
- `gemini-3.8-flash-high` 不在 `agy models` 中；
- `--conversation <id>` 续聊不返回同一 `conversation_id`。

上述任一项需要改适配器 / env / 解析器的，属范围外，另起 Plan 交 Solaire，而不是在本 Plan 内顺手修。

---

## 1. 上游变更窗口（`agy changelog` 离线权威，1.3.1–1.4.0）

与本适配器可能相关、需取样确认的条目：

| 版本 | 条目 | 取样核对点 |
|---|---|---|
| 1.4.0 | OSC 7501 终端状态上报（CLI 先探测终端支持） | 管道 stdout 下不得出现非 NDJSON 字节；若出现即触发 §0.3 停止条件 |
| 1.4.0 | Eligibility check 对临时错误重试、不再锁会话 | 仅行为改善；不改错误映射 |
| 1.3.6 | 模型连接 5 分钟无数据自动取消并重试 | 与 Runtime idle 60s / hard 1800s 不冲突（Runtime idle 更早触发）；仅记录 |
| 1.3.4 | `--agent` 名不匹配时 headless exit 1 | Runtime 总传自建 agent；取样确认正常 agent 照常被承认 |
| 1.3.4 | `--model` 可省 `--effort` | Runtime 显式传 `--effort high`；不受影响，仅记录 |
| 1.3.3 | MCP `_meta antigravity/alwaysLoad`、个人配额立即报错 | 不在本次取样面；记录 |
| 1.3.0–1.3.6 | `/diff`、`/plugin`、Remote Control、交互 UI 若干 | 交互面，headless 不涉及 |

1.3.0 取样后的结论（`step_update` → `result`、工具 `ACTIVE`→`DONE`/`ERROR`、usage 五键、零消耗 `/quota`）是本次回放断言的对照基线。

---

## 2. 取样方法（真实调用，最小量）

### 2.1 隔离与路径

- 取样脚本放在会话 scratchpad，不入库；原始 stdout/stderr 也只留在 scratchpad。
- 每次调用的 `HOME` / `USERPROFILE` / `APPDATA` / `LOCALAPPDATA` / `XDG_*` / `TEMP` / `TMP` 全部重定向到一个新建的临时 profile 根；环境由 Runtime 自己的 `_isolated_environment()` 生成（过滤敏感变量、拒绝 `_FORBIDDEN_AUTH_ENV`），认证走官方 keyring（`account_default`），不复制任何凭据文件。
- 写入 profile 的 settings / agent 声明来自 Runtime 生产渲染器（与 `ensure_generation` 相同产物），不手写简化版。
- 所有 argv 以 Python 列表直接传给 `subprocess`，不经 Git Bash（避免 MSYS 把 `/quota` 改写成路径）。
- 不读取、不打印、不落盘任何 `.env`、token 或 keyring 内容。
- **不碰运行中的 Runtime（:8766）**：取样与 §4.4 均使用独立新建的临时 provider data root / profile / workspace，严禁读写运行中实例的 data root、SQLite 或其 AGY 子进程。

### 2.1a 证据生命周期

- 原始 stdout / stderr 逐步保存在 scratchpad 取样目录，另写一份 `capture_manifest.json`：AGY 版本、每步 argv 形状（路径与 id 以占位符表示）、每步 exit code、每份 raw 与对应 sanitized fixture 的 SHA256、脱敏扫描结果。
- 原始数据与 manifest 保留到 acceptance 完成，供验收独立核对 fixture 来源；验收结束后删除。原始数据、manifest 与取样脚本均不提交。

### 2.2 取样矩阵

模型：生产 slug `gemini-3.8-flash-high`（`--effort high`）。argv 与生产 `_spawn()` 一致（`--agent <generated> --model ... --effort high --input-format stream-json --output-format stream-json --print-timeout 1800s --dangerously-skip-permissions`，续聊加 `--conversation <id>`）。

| # | 面 | 方式 | 预计模型调用 |
|---|---|---|---|
| P1 | `--version` | 期望 `1.4.0` | 0 |
| P2 | `models` | `_parse_models` 解析；断言含 `gemini-3.8-flash-high` | 0 |
| P3 | `/quota` | `_parse_quota` 解析；断言 `num_turns=0`、usage 全零、`command.name=usage` | 0 |
| T1 | 文本轮 | 固定回声提示（如要求只回 `probe-ok`） | 1 |
| T2 | 工具成功轮 | 要求 `view_file` 读取 workspace 内一份取样专用小文件 | 1 |
| T3 | 工具失败轮 | 要求 `view_file` 读取 workspace 内不存在的文件 → `state=ERROR` | 1 |
| T4 | 同会话续聊 | 以 T1 的 `conversation_id` 加 `--conversation`，再发一句回声 | 1 |

合计 4 次模型回合，0 次图片 / MCP / 搜索。任何一步失败不自动重试：先记录原始输出，判断是否属于 §0.3 停止条件。

### 2.3 每次取样的检查

- stdout 逐行 `json.loads` 全部成功（无前缀/后缀字节）；stderr 单独记录。
- init：`event=init`、顶层 `conversation_id`、`init.model=gemini-3.8-flash-high`、`init.agent` 为生成的 agent 名、`AGENT_TOOLS ⊆ init.tools`；init 之后、写 stdin 之前保持静默。
- 回合事件全部经 `parse_init` + `AgyTurnNormalizer.consume` 投影，不抛异常；投影序列与 1.3.0 同类回合一致（文本：`lifecycle → content_delta… → usage → done`；工具：工具生命周期 `ACTIVE→DONE` / `ERROR→tool_error`）。
- usage：取样中既有五键继续存在且类型/语义可投影；额外上游键只记录，不构成失败（现 parser 忽略未知键、也允许五键缺失，所以这一条由取样检查断言，不靠 parser 拒绝）。

### 2.4 脱敏

fixture 只保留真实事件形状和字段名；以下值替换为固定占位符：

- `conversation_id` → `11111111-2222-3333-4444-555555555555`（与 1.3.0 fixture 同一占位）；
- 工作区路径 → `<workspace>`；generation / agent 名中的 id → `<generation>`；
- 任何用户目录、盘符、机器名、账号/邮箱、请求 id、trace id → 占位符；
- 文件内容只用取样专用的无意义字符串。

入库前 `grep` 校验 fixture 不含 `C:\\`、`Users`、`@`、原始 UUID 形状（除占位符）。

---

## 3. 施工内容

| 文件 | 改动 |
|---|---|
| `src/exocore_runtime/providers/antigravity/process.py` | 门禁 `(major, minor) >= (1, 4)` → `>= (1, 5)`；注释补记 1.4.0 取样证据，保留“跨小版本必须先有新 capture”的纪律 |
| `src/exocore_runtime/providers/antigravity/ndjson.py` | 模块 docstring → `>=1.1.20,<1.5` |
| `AGENTS.md` | Boundaries 中信封 → `>=1.1.20,<1.5`，证据清单加 “1.4.0 text/tool/tool-failure/resume turns” |
| `README.md` | 信封与证据清单同步；“the next minor still fails closed” 语义不变（现指 1.5.x） |
| `tests/fixtures/fake_agy.py` | 新增 `version_1_4_0` / `version_1_4_9`（接受）；`bad_version` 改戳 `1.5.0`，注释同步（下一小版本 = 1.5.0） |
| `tests/integration/test_antigravity_adapter.py` | 新增 1.4.0 接受、1.4.9 接受用例；启动故障映射表注释同步（`bad_version` = 1.5.0 → `agy_version_unsupported`）；既有 1.3.0 / 1.3.9 接受用例保留 |
| `tests/fixtures/agy_1_4_0_success.jsonl`（新） | T1 文本轮脱敏取样（含 init） |
| `tests/fixtures/agy_1_4_0_tool_success.jsonl`（新） | T2 |
| `tests/fixtures/agy_1_4_0_tool_failure.jsonl`（新） | T3 |
| `tests/fixtures/agy_1_4_0_resume.jsonl`（新） | T4；init 的 `conversation_id` 与 T1 占位一致 |
| `tests/unit/test_antigravity_components.py` | 新增 1.4.0 文本回放（`parse_init` 以 `gemini-3.8-flash-high` 校验、`AGENT_TOOLS ⊆ init.tools`、事件序列与 usage 精确值）与续聊身份回放（T4 init 的会话 id == T1） |
| `tests/unit/test_ndjson_tool_lifecycle.py` | 新增 1.4.0 工具成功 / 失败回放，断言与 1.2.5 同类用例相同的生命周期投影 |

### 3.1 冻结口径（建议）

1. 信封上界是**排他小版本**：`1.4.x` 全接受，`1.5.0` 起拒绝；取样版本号不代表上界。
2. 拒绝路径不变：越界仍在 `models` / `/quota` 之前、写 stdin 之前 fatal。
3. 只有 §2 全部通过、无 §0.3 停止条件时才改门禁；生产代码改动只限 §3 表中门禁条件、注释与 docstring。
4. fixture 按 §2.4 脱敏；不提交原始取样与取样脚本。
5. 提交写 `refs XinyiS7/ExoCore#64`，不写 fixes/closes。

---

## 4. 验证

1. **红-绿：** 先加 1.4.0 / 1.4.9 接受用例与 `bad_version=1.5.0` 拒绝用例，门禁仍为 `<1.4` 时两个接受用例必须红；改门禁后转绿。另把门禁临时改成 `<1.6` 时 1.5.0 拒绝用例必须红，然后恢复。红绿输出记入交付说明。
2. 新回放用例在当前解析器上直接绿（证明解析器无需改动）。
3. `python.exe -m unittest discover -s tests -v` 全量；与改动前基线对比（改动前先跑一次记录数字，既有环境 skip 原样保留）。
4. **生产路径确认：** 改动后用真实 `agy.exe` 1.4.0 走 `AgyProcessConfig(require_official_executable=True)` → `AntigravityAdapter.prepare_turn`（或直接 `supervisor.ensure(layout)`）。`Service.ensure_generation` 只 stage state/artifacts、不触发 preflight，不能作为门禁证据。`supervisor.ensure` 会 preflight，随后启动并缓存 AGY 进程、等到 init。
   - 使用独立临时 provider data root / profile / workspace，不碰运行中 :8766 的数据根与进程。
   - 0 次模型回合、不写 stdin。
   - 断言门禁放行、`available_model_slugs` 含 `gemini-3.8-flash-high`、`quota_snapshot` 被生产解析器填充、init 返回的 provider session 合法。
   - `finally` 中依次 `close_binding(force=True)` / `retire` / `shutdown`；之后断言无残留 `agy` 子进程（按本步启动的 pid 与进程树核对），临时 provider / profile / workspace 目录已删除。
5. `git diff --check`；fixture 脱敏 grep。

## 5. 交付

- **冻结前：** 本 Plan 单独提交并 push（`docs(plan): ... refs XinyiS7/ExoCore#64`），把 commit hash 与 blob hash 交 Solaire 作为冻结基线。
- **施工：** Runtime 单仓提交（施工 + 测试 + fixture 一个提交；docs 若分开亦可），作者 `claude-opus-5-5 <agent@exocore.local>`，提交即 push。
- issue #64 评论 Plan 路径、提交号、取样摘要（各面结论、调用次数、红绿与全量数字）。
- 生效需重启 Runtime；是否/何时重启由 Alicia 决定，施工方不动运行中的服务。

## 6. 本次不做、留给后续

- 真实 MCP `call_mcp_tool` 步骤在 1.4 上的形状。
- `generate_image`（1.2.16 起 `image-generator` 子代理）产物捕获在 1.4 上的真机证据。
- 当前轮附件、Stop / retire、mailbox hook 在 1.4 上的行为。
- 是否在启动 env 中显式设 `AGY_CLI_OSC7501=off`（仅在取样显示需要时另起 Plan；会改 `LAUNCH_ENVIRONMENT_REVISION`）。
