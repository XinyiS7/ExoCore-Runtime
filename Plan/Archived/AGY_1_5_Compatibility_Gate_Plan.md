# AGY 1.5.x 兼容门禁放宽

> Issue: XinyiS7/ExoCore#104（P1 · area:runtime · bug）
> 状态：实现与验收通过
> 范围：仅 ExoCore-Runtime；不改 ExoCore / Desktop、wire 契约或 Runtime capabilities。

## 1. 目标

官方 AGY 已升级到 1.5.0，而 Runtime 的 preflight 兼容信封仍为 `>=1.1.20,<1.5`。当前 Runtime 一旦重启，会在 `models`、`/quota`、进程 spawn 和用户 stdin 之前返回 `agy_version_unsupported`。

本次把兼容信封最小放宽为 `>=1.1.20,<1.6`，恢复现有 provider 路径；1.6.0 起继续 fail-closed。[gpt / Solaire]

## 2. 冻结范围与不变量

### 2.1 必做

1. 允许 1.5.0 及后续 1.5.x 版本戳通过 preflight 版本门禁。
2. 把下一小版本拒绝样本移到 1.6.0，继续验证排他上界。
3. 同步源码 docstring、README 和 AGENTS 中的兼容信封口径。
4. 保持越界拒绝发生在 `models`、`/quota`、AGY turn spawn 和用户 stdin 之前。

### 2.2 不做

- 不接入 1.5.0 新增的 `agy acp`、`--remote-agent` 或其它新功能。
- 不改 argv、NDJSON event/step 解析、result 投影、timeout 策略、权限策略、模型解析策略。
- 不改 `RUNTIME_CAPABILITIES` 或 ExoCore ↔ Runtime wire。
- 不创建真实会话，不发送真实模型回合，不生成新的真机 capture fixture。
- 不把 1.5.0 changelog 中仅在超时或新子命令路径发生的行为扩张为本次适配工作；若真实使用暴露解析差异，另开 issue 诊断。[gpt / Solaire]

## 3. 关键文件

| 文件 | 变更 |
|---|---|
| `src/exocore_runtime/providers/antigravity/process.py` | 信封与条件从 `<1.5` 改为 `<1.6`；注释记录 1.5.x 按 Alicia 当前官方升级放行 |
| `src/exocore_runtime/providers/antigravity/ndjson.py` | 模块兼容信封同步为 `<1.6` |
| `tests/fixtures/fake_agy.py` | 增加 1.5.0、1.5.9 可接受版本场景；`bad_version` 改为 1.6.0 |
| `tests/integration/test_antigravity_adapter.py` | 验证 1.5.0 / 1.5.9 放行，以及 1.6.0 在发送前拒绝 |
| `README.md`、`AGENTS.md` | 同步兼容信封、证据口径和下一小版本 fail-closed 描述 |

无 schema、数据库或 migration 变更。

## 4. 施工顺序

1. 更新 fake AGY 版本场景和门禁验证目标。
2. 修改生产版本上界。
3. 同步文档口径。
4. 由测试执行方运行聚焦回归与 Runtime 全量回归；主模型不直接运行机械测试流水线。
5. 检查 diff，确认没有 wire、解析器或权限策略的范围外改动。

## 5. 验收标准

- **G1**：版本戳 1.5.0 能完成既有 fake provider turn，不返回 `agy_version_unsupported`。
- **G2**：版本戳 1.5.9 同样通过，证明上界是排他小版本而非单点版本。
- **G3**：版本戳 1.6.0 返回 `agy_version_unsupported`。
- **G4**：G3 路径没有进入模型 turn；既有“发送前失败”不变量保持。
- **G5**：所有兼容信封文档均为 `>=1.1.20,<1.6`，不存在仍代表生产口径的 `<1.5` 残留。
- **G6**：Runtime 相关自动化回归通过，且没有新增真实调用、fixture capture 或跨仓改动。

## 6. Ablation / Razor Check

- 仅修改版本信封、对应测试版本戳和文档是恢复服务所需的最小集合。
- 1.5.0 的新子命令不在 Runtime 调用链上，接入它们不能帮助本次恢复，删除。
- `finished_reason`、print-timeout 新表现尚未在正常现有回合路径形成已证实故障；本次不 speculative hardening、不改 parser。真实故障出现时按独立根因处理。
- 不做付费真机回合；Alicia 的日常真实会话是此次最合适的功能确认面，与 1.4.x 放宽惯例一致。[gpt / Solaire]

## 7. 验收结果

- 聚焦门禁回归：3 项通过（1.5.0、1.5.9 放行；1.6.0 发送前拒绝）。
- Runtime 全量：379 项执行，378 通过，1 跳过，0 失败。
- 首轮全量曾在未改动的 cancel race 用例出现一次竞态失败；该用例隔离连续复跑 3 次均通过，随后全量复跑通过。未为此修改范围外代码。
- `git diff --check` 通过。

结论：G1–G6 全部满足。[gpt / Solaire]
