# AGY 会话工作目录外置（cwd → D:/Alicia）

> **状态：r2，审核 PLAN PASS [gpt-5.6-sol / Solaire]；Gate 0 已完成（加载），D2 已裁决为共享 [Alicia]；§3 未施工。**
> **施工授权分段：** 第一段只执行 §5.0 Gate 0（不改生产代码）。若 Gate 0 证实 ambient 加载，在 Alicia 对 D2 作出明确裁决前不得改任何生产代码；Gate 0 结论为不加载时方可进入 §3 施工。
> **范围：** 仅 ExoCore-Runtime 单仓 + Alicia 本机 `run-runtime` 别名一行 env。不涉及 ExoCore、wire 形状、`RUNTIME_CAPABILITIES`、MCP manifest 契约。
> **待办登记：** 施工前在 `XinyiS7/ExoCore` 开 `area:runtime` issue，提交一律 `refs XinyiS7/ExoCore#N`。

---

## 0. 目标与非目标

### 0.0 术语（全文与 AGENTS.md 统一）

- **staging workspace**：generation root 下的私有 `workspace/` 目录（附件 staging、inspections、`AGENTS.md` 镜像）。始终 generation-private，本计划不改。
- **process work_dir**：AGY 子进程的 cwd。本计划把它从 staging workspace 解耦为可配置的外部目录。外置后它**不是** generation-private。

### 0.1 目标

AGY 会话子进程（及同一 supervisor 的预检子进程）的 **cwd** 改为一个可配置的外部工作目录；Alicia 本机配置为 `D:/Alicia`。模型未写绝对路径的普通 shell 命令 / `write_to_file` 相对路径产物因此落在 `D:/Alicia`，并在 generation retire 后**保留**。

### 0.2 非目标

- 不移动任何 generation-private 状态：`profile/`（HOME/APPDATA/XDG/TEMP 重定向、brain、conversations、hooks、settings、agent.md、mcp_config）、`mailbox/`、`control/`、`workspace/`（附件 staging、inspections、`AGENTS.md` 镜像）、`generated_artifacts/` 全部留在 C 盘 provider data root 的 generation root 内。
- 不改 retire：仍只 `rmtree(generation root)`。
- 不改 `generated_artifacts` 的捕获范围（仍只接受 generation root 内文件）。
- 不改权限策略（`ALLOW_POLICY`/`DENY_POLICY`/`AGENT_TOOLS`）、不改 `SECURITY_POLICY_REVISION` / `LAUNCH_ENVIRONMENT_REVISION`（理由见 §4.3）。
- 不做“普通文件收编/导出/清理”任何机制。

---

## 1. 已核验事实（源码 + 现场，2026-10-10）

| # | 事实 | 位置 |
|---|---|---|
| F1 | spawn 的 `cwd=layout.workspace`；预检 `--version`/`models`/`/quota` 也用 `layout.workspace` 作 cwd | `process.py::_spawn`、`ensure()`→`_preflight(layout.profile, layout.workspace)` |
| F2 | `GenerationLayout.workspace` 由 adapter 固定为 `root / "workspace"`；`workspace` 字段在生产源码中的**唯一**消费者是上述 cwd（附件 / inspections / ControlStore 各自从 generation root 直接拼路径，不读 layout） | `adapter.py::_layout_from_metadata`；全仓 grep `layout.workspace` |
| F3 | retire 只 `shutil.rmtree(self._generation_root(binding_id))`；普通会话结束不 retire | `adapter.py::retire` |
| F4 | 附件与 inspections 以**绝对路径**注入 envelope，与 cwd 无关 | `attachments.py`、`prepare_turn` |
| F5 | `AGENTS.md` 镜像由 `CanonicalControlStore` 维护在私有 `workspace/`；模型上下文只来自 agent.md 内渲染的规则（1.2.5 CP3-A 证据称 headless 不加载 cwd 规则文件；1.3.x 是否仍如此以 F13 + Gate 0 为准） | `control.py`、`renderer.py` 注释 |
| F6 | `generate_image` 的真实落盘在 `profile/.gemini/antigravity-cli/brain/<session>/…`（2026-10-01 实测），不在 cwd；捕获经 `_require_unlinked_path` 拒绝 generation root 外路径（`artifact_path_outside_generation`） | `generated_artifacts.py`；`ExoCore/Plan/AGY_Image_Artifact_Probe_Evidence_2026-10-01.md` |
| F7 | `_require_profile_only_mcp_control(profile, workspace)` 拒绝 **cwd 侧** `.agents/mcp_config.json` 与 `.agents/plugins`（含 reparse）——它守的就是“AGY 会从 cwd 加载的工作区定制” | `adapter.py`（restore 与 verify 两处调用） |
| F8 | 权限策略对文件读写 / 命令无路径限制（`read_file`/`write_file`/`command` 命名空间早已解禁），AGY 今天就能凭绝对路径写 `D:/Alicia` | `renderer.py` ALLOW/DENY 注释 |
| F9 | `D:/Alicia/.agents/` 现存，仅含空的 `skills/`；无 `mcp_config.json`、`plugins/`、`AGENTS.md`、`GEMINI.md`、`.gemini/` | 现场 `ls` |
| F10 | 既有配置抽象：`RuntimeConfig` 字段 + `EXOCORE_RUNTIME_*` env（`from_env`），`__post_init__` 在 bind 前校验；`create_app` 把 config 注入 `AntigravityAdapter` / `AgyProcessConfig` | `config.py`、`api.py::create_app` |
| F11 | 既有测试证据通道：`tests/fixtures/fake_agy.py` 在 `spawn` 证据里记录 `cwd=os.getcwd()`，init 帧也带 `init.cwd`；`test_antigravity_adapter.py` 已有 workspace `.agents` MCP 源拒绝表 | fixtures / integration |
| F12 | `run-runtime` 别名当前只设 TOKEN/HOST/PORT | `~/.bashrc` |

| F13 | AGY 官方 changelog（本机已装 1.3.3，`agy changelog` 离线权威）：工作区 `.agents/` 是 ambient 定制源——`skills/`、`rules`、`agents/`（headless `--agent` 下也会发现）、`hooks.json`（workspace-local hooks）、`skills.json`/`rules.json`/`plugins.json` 清单；清单从 cwd 起向上逐级加载至 project root；Markdown 自定义 agent **默认继承** ambient skills/rules/subagents，frontmatter `inheritCustomizations` 开关可整体决定是否继承；另有 workspace trust 概念 | `agy changelog`（条目原文见施工证据） |
| F14 | `D:/` 与 `D:/Alicia` 均非 git 仓、`D:/.agents` 不存在；cwd=`D:/Alicia` 时唯一 ambient 源是 `D:/Alicia/.agents/`（现仅空 `skills/`）。现有 Runtime agent.md frontmatter 只有 `name`/`description`/`tools`，未设 `inheritCustomizations` | 现场 `ls`；`renderer.py::_agent_markdown_prefix` |

**Scout 结论与检索范围** [claude-opus-5-5 / 砚]**：** 全仓 grep `workspace` / `cwd` / `layout.workspace` / `GenerationLayout`（`src/` 与 `tests/`）、读 `config.py`、`api.py::create_app`、`process.py`（`GenerationLayout`/`ensure`/`_preflight`/`_spawn`）、`adapter.py`（`_restore_security_artifacts`/`_verify_security_artifacts`/`_layout_from_metadata`/`retire`）、`control.py`、`attachments.py`、`generated_artifacts.py`、`capabilities.py`。无现成 work-dir 抽象；`RuntimeConfig`→`create_app`→`AntigravityAdapter`→`GenerationLayout`→spawn 这条既有注入链完整，只需在链上加一个值，不另造机制。

---

## 2. 两类产物的语义（冻结口径）

| | 普通文件 | Runtime 自动捕获 artifact |
|---|---|---|
| 来源 | 模型经 `run_command` / `write_to_file` 以相对路径（或自选绝对路径）写出 | `generate_image` DONE step 中 `Generated image is saved at …` 指向的文件 |
| 落点 | cwd = `D:/Alicia`（或模型自选位置） | AGY 写入 profile brain（generation root 内）→ Runtime 快照到 `generated_artifacts/` |
| 所有权 | Alicia 的普通文件；Runtime **不登记、不导出、不清理、不追踪** | Runtime generation-private 快照，仅凭不透明 ref 导出 |
| retire | 不受影响 | 随 generation root 删除 |
| 越界 | — | 指向 generation root 外 → 保持现状：有界 `failed` 事件（`artifact_path_outside_generation`），不改写 provider 终态、不重跑；**不因 cwd 外置而放宽** |

---

## 3. 施工内容（最小改动）

| 文件 | 改动 |
|---|---|
| `src/exocore_runtime/config.py` | `RuntimeConfig` 新增 `agy_work_dir: Path \| None = None`；`from_env` 读 `EXOCORE_RUNTIME_AGY_WORK_DIR`；`__post_init__`：非 `None` 时必须是绝对路径且为已存在目录，否则 `ValueError`（bind 前失败）；`__repr__` 标 `'[PRIVATE]'` |
| `src/exocore_runtime/api.py` | `create_app` 把 `config.agy_work_dir` 传给 `AntigravityAdapter(work_dir=…)` |
| `src/exocore_runtime/providers/antigravity/process.py` | `GenerationLayout` 新增 `work_dir: Path`；`_spawn` 用 `cwd=layout.work_dir`；`ensure()` 预检改传 `layout.work_dir`（`_preflight` 形参名随之改为 `work_dir`）。`workspace` 字段若施工时确认已无其他读者则删除（F2），不保留死字段 |
| `src/exocore_runtime/providers/antigravity/adapter.py` | 构造参数 `work_dir: Path \| None = None`，存为 `self.work_dir`（`resolve()` 后）；`_layout_from_metadata` 填 `work_dir = self.work_dir or root / "workspace"`；`_require_profile_only_mcp_control` 的两处调用改为检查**实际 cwd**（同一 `work_dir` 值）而非固定的私有 workspace。私有 `workspace/` 的创建、reparse 校验、ControlStore 镜像全部不动 |
| `AGENTS.md`（Runtime） | 边界条款改写，按 §0.0 术语拆成两句：①“staging workspace（generation root 下 `workspace/`）与 profile/mailbox/control/temp/cache 等保持 generation-private，retire 只删 generation root”；②“AGY process work_dir（cwd）默认即 staging workspace；配置 `EXOCORE_RUNTIME_AGY_WORK_DIR` 后为外部目录，**不属于** generation-private，其中普通文件归用户，Runtime 不登记、不导出、不清理”。②另附 D2 的裁决结论（ambient `.agents` 共享或隔离） |
| `README.md` | env 表补 `EXOCORE_RUNTIME_AGY_WORK_DIR` 一行，措辞用 “process work_dir” |
| `adapter.py` 模块 docstring“落盘/边界”条、`GenerationLayout` 字段注释 | 同上两种术语；`work_dir` 字段注释写明“AGY 进程 cwd，可能在 generation root 外” |
| `~/.bashrc` `run-runtime` | 加 `EXOCORE_RUNTIME_AGY_WORK_DIR=D:/Alicia`（本机部署项，不入仓） |

### 3.1 错误语义（显式上抛，无回退）

- 配置值非法（相对路径 / 不存在 / 非目录）→ 启动期 `ValueError`，Runtime 不 bind。
- 运行期该目录消失或不可作 cwd → 维持现有映射：预检子进程失败上抛对应预检错误码；spawn 失败 `agy_spawn_failed`（fatal_generation）。**不**静默退回私有 workspace。
- `work_dir/.agents/mcp_config.json`、`work_dir/.agents/plugins` 存在或为 reparse → 现有 `agy_uncontrolled_mcp_source_forbidden`（fatal_generation），在 spawn 前。
- 续聊：旧 conversation 在新 cwd 下若无法以同一 `conversation_id` 恢复 → 现有 `provider_session_unavailable` / `resume_identity_mismatch` 原样上抛；**不**自动开新会话冒充续聊。注意：显式报错只是禁止伪成功，**不等于验收通过**——续聊连续性是 §5.0 的硬门禁。

### 3.2 未配置时

`agy_work_dir is None` → cwd 仍为私有 `workspace/`。这不是失败回退，而是“未声明外置”的既有行为；它让现有测试与非 Alicia 部署零改动。“默认 D:/Alicia”由本机 `run-runtime` 别名承担，源码不写机器常量（与 `memory_mcp_root` 的“without machine constants”先例一致）。→ 见 §6 决策 D1。

---

## 4. 边界与不变量

1. **retire 半径不变**：只删 generation root；`work_dir` 在其外，天然不被触及。
2. **私有状态不外泄**：profile/mailbox/control/workspace/generated_artifacts 的路径计算全部仍以 generation root 为基，不读 `work_dir`。
3. **文件访问面不扩张**：cwd 只改变相对路径的默认解析点；权限策略本就允许绝对路径访问 `D:/Alicia`（F8）。因此不碰 `SECURITY_POLICY_REVISION`。此条只覆盖文件读写；ambient `.agents` 定制（hooks/agents/rules/skills）是否进入控制面由 Gate 0 + D2 决定——若 D2 选共享，即为 Alicia 明确批准的边界变更，本条不得被引用为“能力面未扩张”。
4. **工作区定制守卫跟随真实 cwd**：AGY 从 cwd 加载的 `.agents` MCP/插件源仍在 spawn 前被拒；守卫对象从“私有 workspace”改为“实际 cwd”，范围不加不减。除 MCP/插件之外的 ambient 源（skills/rules/agents/hooks/清单）如何处置，完全由 §5.0 Gate 0 证据 + D2 裁决决定，本计划不预设新守卫。
5. **artifact 捕获范围不变**（§2）。
6. **单一 cwd 来源**：预检与会话进程用同一个 `layout.work_dir`。

### 4.3 为何不 bump `LAUNCH_ENVIRONMENT_REVISION`

该修订号进入持久化 `process_options` 并参与 live 进程复用判断；bump 会让存量 generation 的已存 options 被判 `agy_process_options_unsupported`，需要额外迁移。而 `work_dir` 是进程级启动配置，只能随 Runtime 重启生效；重启时 Job Object 已终结全部旧 AGY 进程，不存在“旧 cwd 进程被复用”的窗口。故 bump 无收益，剃掉。

---

## 5. 验证目标

### 5.0 硬门禁（任何一项不过 → 本方案 FAIL，停工诊断，不进入提交）

**真机隔离纪律（适用于本节与 §5.2 全部真机步骤）：** 只用临时 `EXOCORE_RUNTIME_STATE_PATH` + 临时 `EXOCORE_RUNTIME_PROVIDER_DATA_ROOT`（均在 scratch 目录、非 C 盘现用 providers 根）启动的一次性 Runtime 实例，在其中新建专用测试 binding。**严禁**对 Alessandro 或任何现用 binding 执行 retire / 续聊探针；现用 Runtime 实例不参与。`D:/Alicia` 下的外部探针文件 / 目录在每步验证后立即删除，并在证据中记录删除结果。

- **Gate 0 — ambient 来源核实（施工前，先于任何代码改动）** [claude-opus-5-5 / 砚]**。** F13 已从官方 changelog 得到“cwd 及其祖先的 `.agents/` 会被加载、Markdown agent 默认继承”的文字证据，但未在 Runtime 的 headless + 隔离 profile + 生产 agent.md 条件下实测（workspace trust 可能另有门槛）。最小探针：一次性实例、cwd 指向临时目录，在其 `.agents/` 下各放一个带唯一标记的 skill 与 rule（不放 hooks / MCP / plugins，避免执行面），按生产 argv 起一个 headless 会话，问模型可见的 skills/rules 清单并比对标记；优先尝试零模型回合的列举方式（若 `-p /skills` 之类与 `/quota` 同为零回合哨兵则用之），否则消耗一次回合。结论二选一写入证据：
  - **不加载** → 删除 D2，§3 不变。
  - **加载** → 交 Alicia 按 D2 裁决后才可施工。
- **Gate 1 — 存量续聊连续性** [gpt-5.6-sol / Solaire]**。** 在一次性实例中，先以“未配置 work_dir”（cwd = staging workspace）新建测试 binding 并完成一回合；重启该实例并配置外部 work_dir（一个临时外部目录，非 `D:/Alicia` 也可，行为等价）；对同一 binding 续聊。**通过条件：** 同一 `conversation_id` 正常恢复且回合成功。返回 `provider_session_unavailable` / `resume_identity_mismatch` / 新 conversation_id 一律判 FAIL——显式报错只证明未伪成功，不构成验收通过。

### 5.1 确定性测试（fake AGY，复用 `spawn` / `init.cwd` 证据与既有 `.agents` 拒绝表）

1. 配置 `work_dir` 时：会话 spawn 证据的 cwd == 该目录；预检子进程 cwd == 该目录。
2. 未配置时：cwd == staging workspace（回归现状）。
3. `work_dir` 内 `.agents/mcp_config.json` / `.agents/plugins` / reparse → `agy_uncontrolled_mcp_source_forbidden`，且发生在 spawn 之前（fake 无 spawn 证据）；复用现有 `workspace-direct` / `workspace-plugin` 表，把目标目录换成外部 work_dir。
4. retire 后：generation root 消失；`work_dir` 内预置的普通文件与目录完好。
5. 附件 materialize 与 inspection 路径仍在 staging workspace 下、envelope 中为绝对路径；ControlStore `AGENTS.md` 镜像仍在 staging workspace 并照常 heal（既有用例应不改即过，作为回归即可）。
6. `RuntimeConfig`：相对路径、不存在路径、文件路径 → `ValueError`；`repr` 不泄露路径；`from_env` 读取 env。
7. `generated_artifacts` 外部路径拒绝：**不新增用例**，`tests/unit/test_generated_artifacts.py` 已有 `artifact_path_outside_generation` 回归，本次捕获链零改动。
8. 全量 `python.exe -m unittest discover -s tests -v` 无失败（既有 host-symlink 环境跳过除外），`git diff --check` 干净。

### 5.2 真机冒烟（隔离纪律同 §5.0；施工证据写入本文末，不入测试代码）

1. 一次性实例配置 `work_dir=D:/Alicia`，新建测试 binding：init 在 `agy_init_timeout`（15s）内完成——确认 AGY 不因以 `D:/Alicia` 为工作区做大规模扫描而超时。
2. 让模型 `run_command` 输出当前目录 → `D:\Alicia`；以相对路径写一个唯一命名的探针文件 → 出现在 `D:/Alicia`，不出现在 generation root。
3. 对**该测试 binding** retire → 一次性实例的 generation root 被删；探针文件仍在 `D:/Alicia`。随后删除探针文件并确认已删。
4. 收尾：停止一次性实例，删除其临时 state / provider root。

---

## 6. 决策记录

- **D1 默认值放哪：** 已定 a（源码可选配置 + `run-runtime` 别名设 `D:/Alicia`；源码零机器常量）[gpt-5.6-sol / Solaire]。
- **D2 ambient `.agents` 共享还是隔离（仅当 Gate 0 结论为“加载”时生效）：**
  - **共享**：接受 Runtime 会话读取 `D:/Alicia/.agents/`（及未来放进去的 skills/rules/agents/hooks）。AGENTS.md 边界明写“process work_dir 的 ambient 定制不属于 generation-private 控制面”。代码零增量。代价：`D:/Alicia/.agents/hooks.json` 若出现会在 Runtime 会话中执行。
  - **隔离**：保持控制面 generation-private。候选手段只有官方已提供的一个：agent.md frontmatter 设 `inheritCustomizations: false`（F13），需先实测它在 1.3.x headless 下确实切断 workspace 来源且不影响 `exocore-memory` MCP 与 `AGENT_TOOLS`；它改变 agent 声明，因此连带 `AGENT_TOOLSET_HISTORY` 同类的声明升级路径与 `SECURITY_POLICY_REVISION` bump，属独立子计划，不在本计划内展开。
  - 审核建议：**隔离** [gpt-5.6-sol / Solaire]——官方 changelog 表明 hooks/agents/rules/skills 可能进入执行面，共享会改变现有 generation-private 控制边界，不是单纯 cwd 对齐。若 Alicia 选共享，须作为她明确批准的边界变更写入 AGENTS.md。
  - 计划本身不在无证据时新增 skills/rules/hooks 守卫。
  - **裁决：共享** [Alicia]（Gate 0 后作出，理由与后果见文末“D2 裁决”）。
- **D3 work_dir 适用范围** [Alicia]：该 ExoCore-Runtime 实例拉起的**所有** AGY session 统一使用 `D:/Alicia` 作为 process work_dir，不做 per-preset / per-binding 区分（与 §3 的进程级单一配置一致，§7 已剔除 per-generation/per-request cwd）。C 盘 generation-private 状态照旧随 retire 清理；`D:/Alicia` 内普通文件由 Alicia 管理，retire 不触碰。本条只是范围确认，**不构成生产代码施工授权**。

---

## 7. 消融剃刀（已剔除）

| 剔除项 | 理由 |
|---|---|
| bump `LAUNCH_ENVIRONMENT_REVISION` / 存量 options 迁移 | §4.3：重启即全量换进程，无复用窗口 |
| 校验 `work_dir` 不得位于 provider data root 内 | 单用户本机配置为 `D:/Alicia` vs `C:` 数据根，不存在该配置 |
| 在 work_dir 下另放 `AGENTS.md` 镜像 / 把 ControlStore 迁到 `work_dir` | Runtime canonical 控制规则仍由私有 agent.md / ControlStore 管理；不得向用户 work_dir 写 Runtime 镜像或覆盖用户 ambient 文件 |
| 放宽 `generated_artifacts` 到 `work_dir` | 普通文件不属于 Runtime；图片实测在 brain 内（F6） |
| 新增 `generated_artifacts` 外部路径用例 | 已有回归，捕获链零改动 |
| 真机 `generate_image` 冒烟 | 捕获链未改，F6 证据 + 确定性回归已足够 |
| “全量测试与改动前同数通过” | 新增用例后计数必然变化，无意义；改为“无失败” |
| 普通文件的登记 / 导出 / 清理 / 配额 | 非目标；普通文件归 Alicia |
| `work_dir` 不可用时退回 staging workspace | 伪成功回退，禁止 |
| per-generation / per-request cwd、wire 字段 | 无需求；进程级配置足够 |
| 同时保留对 staging workspace 的 `.agents` 检查 | AGY 不再以它为 cwd，检查无对象 |
| 无证据的 skills/rules/hooks 大范围守卫 | 由 Gate 0 + D2 决定，不预造 |

---

## 8. 提交与记录

- 一次提交（代码 + 测试 + Runtime AGENTS.md/README），`refs XinyiS7/ExoCore#N`；按惯例提交即 push。
- 真机冒烟证据写入本 Plan 末尾“施工记录”节；施工完成后本文件移入 `Plan/Archived/`。
- 本机 `~/.bashrc` 别名改动单独告知 Alicia，不入仓。

---

## 施工记录

### Gate 0 结论：**加载**（ambient `.agents/skills` 进入模型上下文）[claude-opus-5-5 / 砚]

条件：官方 `agy.exe` 1.3.3；未改生产代码，未启动 Runtime 实例。探针用 Runtime 自身代码构造隔离 profile：`AgyProcessSupervisor._isolated_environment`（HOME/APPDATA/XDG/TEMP 全重定向）、`AntigravityAdapter._expected_settings()`（生产 allow/deny）、`render_agent_markdown` 生成的生产形状 agent.md（frontmatter 仅 `name`/`description`/`tools`）；以 `--agent <该 agent>` 运行。工作目录为两份一次性目录 `D:/Alicia/tmp/gate0-<tag>`（含 `.agents/skills/<marker>/SKILL.md` 与 `.agents/rules/gate0.md`）及无 `.agents` 的基线目录；验后均已删除（`probe_dirs_removed=true`），scratch profile 亦已删除。

| 探针 | 有 `.agents` | 基线 |
|---|---|---|
| `-p /skills --output-format json`（零回合：`num_turns=0`、usage 全零） | marker skill 列出，`path` 指向 cwd 下 `.agents/skills/…` | 无 marker |
| 一次模型回合（生产 `--model gemini-3.1-pro-high --effort high --dangerously-skip-permissions`，`-p` 文本提问“逐字列出上下文中的 skills 与 rules”） | 回复为 marker skill 名 | 回复“无 skills”，只列内置规则 |

- **skills：确认加载**，且对生产形状 agent 可见、`model_invocable`。
- **rules：未观察到**，但不能据此判定“不加载”——探针 rule 文件未带任何 frontmatter/触发声明，AGY 的 workspace rule 格式本次未核实。
- **hooks / agents / MCP / plugins：按计划未探测**（避免执行面）；MCP/plugins 仍由现有守卫在 spawn 前拒绝。
- 消耗：1 次模型回合 ×2（有/无 `.agents` 各一）。

**后果：** 按授权分段，D2 交 Alicia 裁决前不改生产代码。

### D2 裁决：**共享（接受 ambient 加载）**[Alicia]

Alicia 明确批准的边界变更：`D:/Alicia` 是纯本地文件夹、不作为仓库使用；全局 skills 不放在 `D:/Alicia/.agents/`，该目录将由 Alicia 自行删除；“cwd 下 `AGENTS.md` / `.agents` 可能被 AGY 读到”已知悉并接受。据此：
- 不实施 `inheritCustomizations: false` 隔离子计划，不新增 skills/rules/hooks 守卫；MCP/plugins 现有守卫照旧跟随 work_dir。
- §3 的 Runtime `AGENTS.md` 边界条②写明：process work_dir 的 ambient 定制（skills/rules/agents/hooks/清单）不属于 generation-private 控制面，系 Alicia 批准的边界变更。
- 第一段授权（Gate 0）结束；进入 §3 施工仍需 Alicia 批准本计划并开 issue，且 Gate 1 为施工后硬门禁。

### §3 施工 + 验证（refs XinyiS7/ExoCore#46）[claude-opus-5-5 / 砚]

施工授权：Alicia 2026-10-10 明确“可以开工”。

**确定性测试**
- 新增 `tests/integration/test_antigravity_adapter.py`：`test_default_process_work_dir_is_the_staging_workspace`（version/models/quota/spawn 四类进程 cwd 均为 staging workspace）、`test_external_work_dir_is_process_cwd_and_survives_retire`（四类进程 cwd 均为外部 work_dir；profile/mailbox/control/workspace 仍在 generation root；work_dir 内只有用户预置文件；retire 后 generation root 消失、用户文件内容不变）、`test_external_work_dir_mcp_sources_are_rejected_before_spawn`（work_dir 下 `.agents/mcp_config.json` 与 `.agents/plugins` → `agy_uncontrolled_mcp_source_forbidden`，spawn 证据数不变）。
- 新增 `tests/unit/test_config.py`：`test_agy_work_dir_is_read_from_env_and_kept_out_of_repr`、`test_agy_work_dir_must_be_an_existing_absolute_directory`（relative / missing / file 三例 `ValueError`）。
- fixture：`fake_agy.py` 预检证据补 `cwd`；`hard_crash_agy_owner.py` 随字段改名。
- 变异验证：把 `_process_work_dir` 临时改为恒返回 staging workspace，新增外部 work_dir 用例 6 个子测试全部 FAIL；已恢复。
- 全量 `python.exe -m unittest discover -s tests`：349 项通过，1 项既有 host-symlink 跳过；`git diff --check` 干净。

**真机（官方 agy 1.3.3；进程内驱动生产 `RuntimeService` + `AntigravityAdapter`，临时 state DB 与 provider root 位于系统 Temp 的一次性目录；一次性 binding；未触及现用 Runtime 实例与任何现用 binding）**

| 项 | 结果 |
|---|---|
| Gate 1 A 段：work_dir 未配置，新 binding 首回合（告知暗号） | `done`；conversation_id `ed62f05c-…` |
| Gate 1 B 段：重启并配置 work_dir=`D:/Alicia`，同 binding 续聊（问暗号） | `done`；**同一 conversation_id**；回复为正确暗号 → **Gate 1 PASS** |
| 5.2-1 init 耗时（三次 spawn 至 init 就绪） | 2.31 s / 2.77 s / 2.56 s（上限 15 s） |
| 5.2-2 cwd 与相对路径写入 | 模型回报 cwd `D:\Alicia`；探针文件出现在 `D:/Alicia`，generation root 内无 |
| 5.2-3 retire 该 smoke binding | `changed=true`；generation root 消失；`D:/Alicia` 探针文件仍在 |
| 收尾 | 探针文件已删；Gate 1 binding 已 retire；临时 state/provider root 已删；`D:/Alicia` 无其他新增项 |

**施工偏差：**
- `GenerationLayout.workspace` 改名为 `work_dir`，没有新增并列字段（依 §3“无其他读者则删除”）；`_preflight` / `_probe_quota_snapshot` 的形参也随之改名。
- 不再单独对 staging workspace 跑 `.agents` 检查（§7 已剔除）。work_dir 未配置时，被检查的目录仍是 staging workspace，行为与原先一致。
