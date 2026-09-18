# Subscription Runtime AGY — CP4 Stop / Hard-Cancel Construction Plan

> **状态：FROZEN · PLAN PASS — Solaire REVISE-4 independent-review closure**  
> **日期：** 2026-09-18  
> **前置：** CP1 PASS / CP2 PASS / CP3 CLOSED  
> **范围：** Stop signal → ExoCore single-owner settlement → Runtime cancellation arbitration → AGY hard process cleanup → durable terminal truth。  
> **纪律：** 本文件仅冻结施工契约；当前不施工、不 commit、不重启 PID 13920、不进入 CP5/MCP。

---

## 0. CP4 目标与非目标

### 0.1 目标

CP4 只解决四件事：

1. **Stop 响应性**：消除 ExoCore 同步 Runtime stream 阻塞期间无法及时观察 `stop_event` 的死区。
2. **Single-owner**：Watcher 只负责打断本地读取；ExoCore `RuntimeTurnRunner` 是唯一调用 `RuntimeBindingService.cancel_turn()`、决定本地 terminal 投影的主体。
3. **Physical-honest cancel**：Runtime 只有在 provider 有正向物理证据时才能 durable `cancelled`；自然 terminal、kill failure、ownership unknown 必须各自落真实状态。
4. **Hard cancel**：AGY 活动进程树取消走 `force=True`，并通过真实 1.2.6 Windows 探针证明 active-turn Stop 的端到端 process-tree cleanup SLA `< 2.0s`、无孤儿进程。

### 0.2 非目标

- 不修改 CP2 tools / permission policy / URL deny。
- 不修改 CP3 Project Rules / generation identity / canonical backing。
- 不接 MCP，不进入 CP5。
- 不改 stopped turn 的 coverage 规则，不为 stopped turn 创建 canonical assistant Message。
- 不承诺 stopped 后 REUSE 同一 AGY session；按现有 continuity law，下一轮预期 `REBASE_CANONICAL_GAP`。
- 不以 CP4 顺手消灭所有“send-attempt 但 Runtime 尚未 durable request”的协议不确定性；该极小窗口必须诚实落 `indeterminate`，不得伪装成 stopped。

---

## 1. Verified Current-State Inventory

### 1.1 ExoCore HTTP / Stop signal

- `agents/views.py::ChatStreamStopView.post()`：Stop 只设置当前 run 的 `threading.Event`。
- 该入口当前不写 `RuntimeTurn` / `RuntimeBinding` / `Message`，也不调用 Runtime `/cancel`。这一点继续保留。

### 1.2 ExoCore runner

`agents/runtime_turn.py::RuntimeTurnRunner.run_prepared_happy_path()` 当前关键顺序：

```text
client_factory()
  -> ensure_binding()
  -> mark_turn_sent()
  -> should_stop()
  -> client.stream_turn()
  -> for event in events:
       should_stop()
```

已核实缺陷：

- Stop 在 `ensure_binding()` 前已经置位，仍会先构造 client / 做 control-plane 网络。
- Stop 在 ensure 期间到达，ensure 返回后仍会先 `mark_turn_sent()`。
- `for event in events` 阻塞在 httpx socket read 时无法轮询 `should_stop()`。
- `RuntimeEventStream.close()` 已具备跨线程关闭 bound response 的机制；它适合作为 watcher 的**解阻塞手段**，但不拥有 cancel/DB 权限。

### 1.3 ExoCore durable cancel seam

`bridge/subscription_runtime/bindings.py::RuntimeBindingService.cancel_turn()` 已具备大部分领域状态机：

- `PREPARED`：纯本地变为 `STATUS_STOPPED / cancelled_before_send / provider_input_effect=not_sent`。
- definitive terminal：幂等返回，禁止覆盖。
- `SENT / INDETERMINATE`：调用 Runtime cancel，随后 replay journal 对齐权威 terminal。
- cancel transport indeterminate：不得伪造 stopped。

CP4 只需允许 `client=None` 的**精确 PREPARED 分支**；若 turn 已非 PREPARED 且 client 缺失，必须 fail closed。

### 1.4 ExoCore send-boundary 事实

`mark_turn_sent()` 是 ExoCore 的保守 send-attempt boundary；`SubscriptionRuntimeClient.stream_turn()` 返回的是 lazy `RuntimeEventStream`，真正 POST 在 iterator 消费时才开始。

因此存在极小但真实的窗口：

```text
ExoCore local = SENT
Runtime request row = 可能尚未注册
```

若 Stop 恰在这里发生，Runtime `/cancel` 可能返回“request not prepared / not found”。这是**真实 send-attempt uncertainty**，不能硬改成 stopped。CP4 要尽量把 stop checks 放在 `mark_turn_sent()` 前，但一旦跨越该 boundary，仍沿用 may-have / indeterminate 法律。

### 1.5 Runtime service cancel

`ExoCore-Runtime/src/exocore_runtime/service.py::RuntimeService.cancel()` 当前顺序：

```text
journal.terminal(status="cancelled")
  -> await provider.cancel(...)
```

这是 CP4 必修 defect：provider kill 若失败，durable journal 已不可逆写成 false `cancelled`。

同时，`RuntimeService.stream_turn()` 在 owner HTTP stream 被取消时已经会调用 `self.cancel(...)`。CP4 后它不是第二个 terminal owner，而只是**同一个 Runtime request arbiter 的另一个 caller**；所有 caller 必须汇入同一个 settlement task。

### 1.6 Runtime provider stream terminalization

`service.py::_stream_provider_events()` 当前会在以下路径写 terminal：

- provider terminal `done` → completed；
- provider terminal `error` → failed / indeterminate；
- provider adapter exception；
- malformed / unknown provider event；
- unexpected EOF。

CP4 仲裁必须覆盖**所有**这些 terminalization 出口，不能只包住 `done/error` 分支。

### 1.7 AGY process supervisor

`providers/antigravity/process.py` 当前：

- `AgyProcessSupervisor.cancel()` 先锁外检查 `current_request_id`，再 `close_binding(force=False)`。
- `_finish_cancelled_stream_cleanup()` 也使用 `force=False` 并可等待 graceful close timeout。
- `_dispose_session(force=True)` / `_kill_tree()` 已有 Windows `taskkill /PID ... /T /F` + Job Object cleanup 基础。
- 正常 `result` 路径在 `_assert_quiet_after_result()` 成功后，会清空 `session.current_request_id`，然后才把规范化 `ProviderEvent` 向上 yield。

由此存在自然 terminal 微窗：provider 已严格完成，但 Runtime journal 尚未 terminal；late cancel 不能因为 `current_request_id is None` 就写 cancelled。

### 1.8 Stopped turn continuity

现有 ExoCore 法律不变：

- stopped turn 保留 canonical user Message；
- 不 `finalize_completed_turn()`；
- 不创建 canonical assistant Message；
- 不推进 `committed_coverage_anchor_message_pk`；
- 下一轮 `_unclaimed_gap_messages()` 看见该 user Message，预期 `REBASE_CANONICAL_GAP`。

### 1.9 Solaire independent review closure — R11–R14

REVISE-3 对最新工作树逐行反证后又确认四个必须在施工前冻结的 seam：

- **R11 / certification boundary**：`process.py` 在 strict AGY result 后先清 `current_request_id`，但 `adapter.py` 随后才做 mailbox receipt validation。process result 只能是 candidate；natural terminal proof 的主权必须上移到 adapter certification。
- **R12 / shutdown admission race**：`_shutting_down=True` 与 active cancellation-task snapshot 若非原子，一个 cancel 可在“已检查 admission、尚未登记 task”时穿过 shutdown snapshot。
- **R13 / whole-request arbitration + start fence**：`service.py::_run_owned_turn()` 在 `_stream_provider_events()` 之前已有多处直接 terminal path；同时 `process.py` 当前 `current_request_id` claim 不在 binding lock 下。arbiter 必须覆盖 whole request，provider start 与 cancel 必须共享 exact-request fence，禁止 terminal 后 ghost send。
- **R14 / attempted-send cancel classification**：ExoCore 本地已 `SENT`、Runtime request 尚未 registered 时，当前 cancel 会得到 409 `identity_conflict`，而 client 默认把它归为 definitive failure。CP4 必须做 **cancel-operation-specific** conservative indeterminate classification，不能让 durable local turn 留在 unresolved `SENT`。

这四项均属于 CP4 已声明目标的竞态闭合，不扩张 CP2/CP3/CP5。

---

## 2. Frozen Laws

### Law 1 — ExoCore Single Owner

Watcher / `/stop/` handler：

- 可：设置 signal、记录自身 `interrupted`、调用 `event_stream.close()` 解阻塞。
- 不可：调用 `client.cancel()`、写 ORM、决定 terminal。

`RuntimeTurnRunner`：

- 是 ExoCore 唯一调用 `RuntimeBindingService.cancel_turn()` 的 owner；
- 只依据 Runtime durable outcome 投影 `completed / stopped / failed/indeterminate`。

### Law 2 — Runtime Terminal Immutability

Runtime request 一旦 durable terminal：

```text
completed | failed | cancelled | indeterminate
```

后到 cancel 只能读取，不能覆盖。

映射：

```text
Runtime cancelled <=> ExoCore STATUS_STOPPED
Runtime indeterminate != ExoCore stopped
```

### Law 3 — Cancelled Requires Positive Physical Proof

Runtime 仅在 provider cancel receipt 为：

- `CANCELLED_PRESTART`：exact prepared request 已被 fencing，provider stdin 尚未写入，且其 generation process 已完成 exact force-dispose；
- `CANCELLED_ACTIVE`：exact active request 的 process tree 已完成 force-dispose；或
- `CANCELLED_ABANDONED`：同一 exact request 已由 abandoned-owner cleanup 完成 force-dispose。

时允许写 `cancelled`。

不得把这些情况当 cancelled：

- provider/session 不存在但原因未知；
- request 已非 current 但无 provider-certified terminal proof；
- taskkill / cleanup 报错；
- transport/control outcome 不确定。

### Law 4 — Natural Terminal Beats Late Cancel, But Only After Provider Certification

AGY process 层的 `result_seen + _assert_quiet_after_result()` 只证明 **process-level result candidate**，还不等于 provider terminal truth。`AntigravityAdapter` 随后仍必须完成 exact mailbox receipt validation、相同的 fatal-cleanup/error mapping，成功后才能形成 provider-certified `NATURAL_TERMINAL_READY` + normalized terminal `ProviderEvent`。

因此 supervisor 的 process candidate **不得直接**映射成 Runtime 的 `NATURAL_TERMINAL_READY`。若 cancel 已先取得 `CANCELLING` arbiter，Runtime-owned settlement task 可以直接救援落盘的，只能是 adapter 已认证的 final terminal；若 mailbox validation 失败，则救援的是与正常 stream path 相同的 normalized failed/indeterminate terminal，而不是 `completed`。

provider-certified receipt 在 Runtime durable terminal 成功前不得提前销毁；owner stream 消失也不能使这份 terminal proof 丢失。

### Law 5 — No I/O Under Arbiter Lock

Request arbiter lock 只允许：

- state compare / transition；
- task registration；
- snapshot references。

严禁在锁内 await：provider I/O、process kill、journal/network wait。

### Law 6 — Exact Request Process Ownership + Start Fence

AGY 的 request ownership acquisition、显式 cancel 与 abandoned cleanup 必须共享同一个 per-binding serialization boundary。至少保证：

```text
start:  exact prepared request -> claim current_request_id -> stdin send eligibility
cancel: request identity check -> process dispose -> session pop -> kill receipt
```

其中 exact request identity check、`current_request_id` claim/clear、process dispose、session pop、kill receipt 都不得依赖锁外快照。

禁止两类 TOCTOU：

- 锁外判断 request A，然后锁内误关已经变成 request B 的 session；
- cancel 已经 fenced request A，而旧 owner 随后仍 claim A 并写 stdin，制造 terminal 之后的 ghost execution。

### Law 7 — Runtime-owned Settlement Survives Caller Disconnect

一旦 `OPEN -> CANCELLING`，创建 Runtime-owned `asyncio.Task`。

HTTP caller 只 `shield()` 等待；caller 的 `CancelledError` 不得取消 settlement task。

### Law 8 — Graceful Shutdown Never Cancels Settlement Tasks

`RuntimeService.shutdown()` 不得对 active cancellation task 调 `task.cancel()`。

“关闭新 cancel admission”与“snapshot 已登记 cancellation tasks”必须在同一个短生命周期锁/原子 registration seam 内完成；否则一个 cancel 可以在检查 `_shutting_down` 后、登记 task 前跨过 shutdown snapshot。

Graceful shutdown 必须先原子关闭 admission 并 snapshot 已存在的 settlement tasks，再 shielded-await 它们，之后 terminalize 真正剩余 open requests，最后 provider shutdown。锁内只做 flag/state/task registration，不 await provider/network/process I/O。

### Law 9 — Partial Output Is Not Canonical Assistant

Stop 前已发送到 UI 的 content/thinking delta 可留在 SSE/buffer；stopped turn 不因此创建 assistant Message，也不推进 coverage。

### Law 10 — SLA Scope

`< 2.0s` SLA 针对**已经进入 Runtime active execution / provider process tree 的 hard-cancel 路径**。

不把 `ensure_generation` control-plane 延迟或真实 send-attempt uncertainty 伪装成 hard-cancel SLA failure。

---

## 3. Frozen Design

## 3.1 ExoCore — Pre-send Gates + StreamStopWatcher

### 3.1.1 Pre-send gates

`RuntimeTurnRunner.run_prepared_happy_path()` 改为：

```text
[A] PRE-CLIENT / PRE-ENSURE
    if should_stop():
        cancel_turn(prepared, client=None)
        => STOPPED / cancelled_before_send / not_sent
        => zero client construction, zero Runtime network

client = client_factory()
ensure_binding(...)

[B] POST-ENSURE / PRE-STREAM-OBJECT
    if should_stop():
        local PREPARED cancel
        => not_sent
        note: ensure control-plane network already happened; TurnRequest did not

event_stream = client.stream_turn(...)   # lazy TurnRequest stream

[C] FINAL PRE-MARK-SENT
    if should_stop():
        close unstarted event_stream
        local PREPARED cancel
        => not_sent

mark_turn_sent(...)

[D] SENT / STREAM PHASE
    start _StreamStopWatcher
    consume stream
    watcher interruption => main runner calls cancel_turn(...)
```

跨越 `mark_turn_sent()` 后不再存在“为了 Stop 好看而回滚 PREPARED”的逻辑；此后统一按 `may_have_reached_provider`。

### 3.1.2 `_StreamStopWatcher`

新增最小内部 helper，职责只有：

- daemon/background thread 观察 `should_stop()`；poll interval 可设 ≤100ms；
- Stop 时先设置自身 `interrupted=True`，再调用 `event_stream.close()`；
- `close()` 抛出的本地 cleanup exception 只记录 safe fixed metadata，不决定 terminal；
- `disarm()` 必须可确定性结束 watcher，避免 thread leak。

Runner exception priority：

```text
if watcher.interrupted or should_stop():
    -> _cancel_result() / RuntimeBindingService.cancel_turn()
elif stream transport indeterminate:
    -> existing mark_transport_indeterminate()
```

即：主动 Stop 造成的本地 stream close 不得先被误判成普通 transport failure；但若随后 Runtime cancel 本身不可确定，仍必须诚实落 indeterminate。

### 3.1.3 PREPARED local cancel seam

`RuntimeBindingService.cancel_turn(prepared, client=None)`：

- 仅当 locked turn 仍 `PREPARED` 时允许 `client=None`；
- 该分支零网络，写：
  - `STATUS_STOPPED`
  - `terminal_code=cancelled_before_send`
  - `provider_input_effect=not_sent`
- 若 turn 已 `SENT/INDETERMINATE` 且 client 为 None，fail closed。

### 3.1.4 Attempted-send micro-window

如果 Stop 在本地 `mark_turn_sent()` 之后、Runtime request 尚未 durable registration 之前发生：

- Runner 仍调用远端 cancel；
- 当前 Runtime 对“request 尚未 prepared/registered”会走 cancel control error（现行为 409 `identity_conflict`）；该 **cancel-operation-specific** 结果在本地 `SENT` 语义下必须保守分类为 indeterminate，不能沿用普通 definitive-request-failure 使 turn 留在 `SENT`；
- 可实现为 cancel 专属 safe error code，或 client effect matrix 的 cancel-specific override；**禁止**全局放宽其它 operation 的 identity-conflict 语义；
- 无论采用哪一种，最终必须进入既有 `cancel_transport_indeterminate` / may-have / recovery law，**不得**改写为 stopped。

这是“send attempt 已跨界但远端 effect 尚不可证明”的真实不确定性，不属于 watcher 误判。

---

## 3.2 Runtime — Provider-neutral Cancel Contract

`providers/base.py`（或等价 internal provider module）新增内部 contract；**不改 HTTP wire schema**：

```python
class ProviderCancelOutcome(str, Enum):
    CANCELLED_PRESTART = "cancelled_prestart"
    CANCELLED_ACTIVE = "cancelled_active"
    CANCELLED_ABANDONED = "cancelled_abandoned"
    NATURAL_TERMINAL_READY = "natural_terminal_ready"
    OWNERSHIP_UNKNOWN = "ownership_unknown"

@dataclass(frozen=True)
class ProviderCancelReceipt:
    outcome: ProviderCancelOutcome
    natural_terminal: ProviderEvent | None = None
```

`RuntimeProviderAdapter.cancel(...) -> ProviderCancelReceipt`。

要求：

- `RuntimeService` 只认识 provider-neutral receipt；不得 import AGY supervisor 私有类型。
- `DeterministicFakeAdapter` 同步实现，用于 service race tests。
- `NATURAL_TERMINAL_READY` 仅允许携带 **adapter-certified** normalized terminal `ProviderEvent`，不得把 supervisor process candidate 直接上送，也不得携带 raw AGY payload/stdout/tool body。
- provider contract 增加窄的、**本地同步且幂等**的 `reclaim_request(binding_id, request_id)`（或等价 provider-neutral ack），只用于 Runtime durable terminal 之后移除 request proof 的 registry ownership；不得执行外部 I/O，也不得用“provider 已 return/yield”代替 durable ack。
- receipt 不复制 binding/request identity；调用参数本身就是 identity。

HTTP `CancelResult / CancelOutcome` 保持现有 v2 成功响应结构，不新增字段；如为 attempted-send micro-window 引入 cancel 专属 safe error code，只允许 metadata-only code 变化，不暴露 provider/process 内容。

---

## 3.3 Runtime — Request Arbiter + Cancellation Settlement Task

### 3.3.1 Arbiter

```python
class ArbiterState(str, Enum):
    OPEN = "open"
    NATURAL_TERMINAL_PENDING = "natural_terminal_pending"
    CANCELLING = "cancelling"
    TERMINAL = "terminal"

class RequestArbiter:
    state
    lock
    done_event
    cancellation_task | None
```

`_get_or_create_arbiter(key)` 必须是无 await 的同步 registry 操作，保证 stream/cancel 对同 request 获取同一对象。owned stream 在 `store.claim_request()` 确认本实例为 owner 后、进入 `_run_owned_turn()` 的任何 resolution/provider await 或 terminal path 之前就必须取得该 arbiter；若 cancel 在 claim 后先到，则 cancel 创建同一个 arbiter，owner 随后只复用。

### 3.3.2 `RuntimeService.cancel()`

1. 先读 durable request：已 terminal → 原样 `changed=False` 返回，**不再重复 provider.cancel**。
2. cancel task registration 必须经过 Runtime lifecycle/admission seam：shutdown 要么看到并 snapshot 该 task，要么在 task 尚未获准创建前原子关闭 admission，不存在中间态。
3. 获取 arbiter：
   - `NATURAL_TERMINAL_PENDING`：不调用 provider；等待 `done_event`，读 durable terminal，`changed=False`。
   - `CANCELLING`：等待已有 Runtime-owned cancellation task；并发 caller 返回 `changed=False`。
   - `OPEN`：原子置 `CANCELLING`，创建一个 Runtime-owned settlement task；该 caller 是 first canceller。
4. 所有 caller 用 `asyncio.shield()` 等待；caller disconnect 不取消 settlement。
5. 返回前必须断言 store 已 terminal；禁止 `CancelResult.status=prepared/sent`。

### 3.3.3 Runtime-owned settlement task

锁外调用 provider cancel：

```text
CANCELLED_PRESTART / CANCELLED_ACTIVE / CANCELLED_ABANDONED
    -> journal cancelled
    -> first canceller changed=True

NATURAL_TERMINAL_READY
    -> 使用与正常 provider terminal 相同的 bounded mapping helper
       将 receipt.natural_terminal durable 为 completed / failed / indeterminate
    -> cancel 没有改变成 cancelled；CancelResult changed=False

OWNERSHIP_UNKNOWN
    -> journal indeterminate / cancel_ownership_unknown
    -> first canceller changed=True

provider cancel raises
    -> journal indeterminate / cancel_cleanup_failed
    -> first canceller changed=True
```

只有 durable terminal 已确认后：

```text
arbiter.state = TERMINAL
arbiter.done_event.set()
```

随后可从 registry 回收 arbiter。已持有 arbiter 引用的 waiter 仍可正常读取 `done_event`；新 caller 走 durable terminal fast path。provider proof 的 `reclaim_request()` 在此之后执行；即使 reclaim 失败也不得重新关闭 `done_event`、改写 terminal 或让 cancel caller 永久等待。

### 3.3.4 `changed` 语义冻结

- `changed=True`：本次 first cancel settlement 将请求收敛成 `cancelled` 或 cancel-induced `indeterminate`。
- `changed=False`：terminal 原已存在、并发重复 cancel、或 late cancel 发现/救援的是自然 terminal。
- ExoCore 决不能用 `changed` 判断 stopped；只看 `status` + journal terminal truth。

---

## 3.4 Runtime — Whole-Request Arbiter / Unified Terminalization Gate

Request arbiter 的覆盖范围是**整个 claimed Runtime request**，不是只包 `_stream_provider_events()`。现行 `_run_owned_turn()` 在 provider stream 之前也会直接 terminalize：unsupported resolution、bootstrap conflict、`prepare_turn()` provider error/exception、`send_boundary_conflict`；这些出口与 cancel 同样必须只有一个 terminal winner。

### 3.4.1 所有 terminal path 统一 claim

在**任何** `journal.terminal()` 前统一经过 request arbiter：

```text
if state == OPEN:
    state = NATURAL_TERMINAL_PENDING
    release arbiter lock
    # no await here
    persist chosen terminal synchronously
    state = TERMINAL; done_event.set()

if state == CANCELLING:
    natural owner MUST NOT persist terminal
    await done_event
    replay winner journal frames > yielded_sequence
    return

if state == TERMINAL:
    replay winner and return
```

`OPEN -> NATURAL_TERMINAL_PENDING` 之后到 durable `journal.terminal()` 之间**禁止任何 await**。这样 natural owner 一旦先 claim，就不会留下一个可被 task cancellation 永久悬空的 pending 状态。journal/store 写失败则按既有 fatal service failure 处理，不得假装另一个 terminal 已成功。

该 gate 必须覆盖：

- resolution unsupported / bootstrap conflict；
- provider prepare error / exception；
- send-boundary conflict；
- pending provider `done` / `error`；
- malformed / unknown event；
- provider adapter error / provider exception；
- unexpected EOF；
- cancel-induced `cancelled / indeterminate / rescued natural terminal`。

自然 owner durable terminal 成功后按以下顺序释放逻辑 waiter，再回收 provider proof：

```text
state = TERMINAL
set done_event
reclaim arbiter registry entry

# durable truth + waiters 已安全释放之后
provider.reclaim_request(binding, request)
```

`reclaim_request()` 冻结为本地同步、幂等 registry release：发生在 durable terminal 已存在、`done_event` 已释放之后且不在 arbiter lock 内；不得引入外部 I/O。异常只代表实现 defect，不能反向覆盖已 durable terminal，也不能阻塞已经释放的 waiter；shutdown/provider cleanup 仍清 residual registry。

### 3.4.2 Cancel 后禁止 owner 继续产生新的 provider effect

`provider.prepare_turn()` 是 await 点。owner 从任何 provider await 返回后、尤其是 prepare 返回后与真正 provider stream/start 前，都必须重新观察 arbiter：

- `CANCELLING` → 不再 mark/send/start；等待 settlement winner 并 replay；
- `TERMINAL` → 不再产生 provider effect，直接 replay；
- `OPEN` → 才允许继续。

这仍不足以单独关闭“最后一次 check → provider stdin write”的微窗，所以 AGY adapter/supervisor 还必须提供 §3.5 的 exact start fence。Runtime gate 与 provider fence 两层都要成立。

### 为什么 cancel 先赢时不再“让渡回自然流”

AGY 可能已经产生 process result candidate，但 owner HTTP stream 恰好同时被 Stop/断连取消。如果 cancellation task 只把状态改回 `NATURAL_TERMINAL_PENDING` 然后等待自然流，那个自然流可能已经不存在，形成永久死锁。

所以：

- natural owner **先**占位 → natural owner 无 await 地完成 durable terminal；
- cancel **先**占位 → Runtime-owned settlement task 必须自己完成最终 durable truth；若 provider 给出 **adapter-certified** `NATURAL_TERMINAL_READY`，settlement task 直接救援该 terminal。

没有双向互等，也没有 terminal 之后的 ghost send。

---

## 3.5 AGY — Exact Request Start Fence + Certified Receipts

### 3.5.1 两级 natural evidence：process candidate != provider terminal

`AgyProcessSupervisor.stream_turn()` 在：

```text
normalizer.result_seen
  -> _assert_quiet_after_result() PASS
```

之后只能形成 request-scoped **process result candidate**。它可以让 supervisor 清楚“这个 exact request 已经过 strict AGY result boundary”，但不能直接成为 provider-neutral `NATURAL_TERMINAL_READY`，因为 `AntigravityAdapter.stream_turn()` 还必须验证 `EphemeralMailbox.validate_receipt(request_id, payload_hash)`。

adapter 是 provider terminal certification owner，而且 normal stream 与 late cancel 必须共享**同一个 request-scoped certification state**（可由当前 `_prepared` tuple 收窄演化为内部 state object）：

```text
process result candidate
  -> acquire exact adapter request seam
  -> if state.certified_terminal already exists: reuse it
  -> else validate exact prepared identity
       -> mailbox.validate_receipt(...)
       -> apply the same mailbox cleanup / fatal-generation cleanup / error mapping
       -> store one immutable state.certified_terminal
  -> publish/reuse adapter-certified normalized terminal receipt
  -> only now may Runtime observe NATURAL_TERMINAL_READY
```

因此 certification 是 single-flight / idempotent：stream 与 cancel 谁先完成，另一方都复用同一 immutable result，不重复 mailbox validation/cleanup。`reclaim_request()` 只移除 registry entry；已经持有该 request-state 本地引用的 in-flight stream 仍可读取 immutable certified result 完成退出。

若 mailbox receipt missing/mismatch/invalid，正常 owner path 与 late-cancel rescue path 必须得到**同一个** normalized failed/indeterminate terminal，并执行相同 fatal generation cleanup；禁止 process-level `done` 抢先变成 `completed`。

### 3.5.2 Exact prestart / active ownership fence

`prepare_turn()` 已通过 adapter artifact lock 建立 exact `_prepared` request。CP4 必须让“prepared → current_request_id claim → stdin eligibility”与 cancel 使用同一锁序，冻结为：

```text
adapter artifact lock -> supervisor per-binding lock
```

owner start：

```text
lock exact prepared A
  -> supervisor lock exact session
  -> confirm A not fenced / no conflicting current request
  -> claim current_request_id = A
  -> grant stdin-send eligibility for A
unlock
```

cancel：

- exact `_prepared` A 存在但 A 尚未 active：在相同锁序下 fence A、force-dispose 该 generation process、移除 exact prepared state，成功后返回 `CANCELLED_PRESTART`；
- exact current A active：在 supervisor binding lock 内 force-dispose exact session，返回 `CANCELLED_ACTIVE`；
- exact abandoned receipt 已证明 kill：返回 `CANCELLED_ABANDONED`；
- process candidate 存在：交给 adapter certification，成功/失败都返回对应 certified `NATURAL_TERMINAL_READY`；
- 以上均无 proof：`OWNERSHIP_UNKNOWN`。

禁止旧形状：

```text
lock 外看 current_request_id == A
等待 lock
close_binding() 实际关闭已经变成 B 的 session
```

也禁止 cancel 已 fence A 后，旧 owner 再从 `_prepared` 快照启动 A。

### 3.5.3 Abandoned owner-stream receipt

当 Runtime HTTP owner stream 被取消：

- `_finish_cancelled_stream_cleanup()` 走 `force=True`；
- 只有 exact request dispose 成功，才能记录 `CANCELLED_ABANDONED` proof；
- cleanup 失败记录 exact failure/unknown proof，不得留下 success marker；
- 若 request 已到 process candidate，必须走 adapter certification，不得伪造 abandoned kill；
- request mismatch 绝不能消费另一个 request 的 proof。

显式 `/cancel` 后到时读取 exact abandoned receipt，而不是把“session 不存在”当成功。

### 3.5.4 Receipt lifecycle / durable ack

provider request proof 可用 request-scoped map 或每 binding 单槽，但必须满足：

- exact `(binding_id, request_id)` 匹配，且下一 request 不得覆盖未 ack 的上一 request proof；
- process candidate 与 adapter-certified terminal receipt 是不同层级，前者不得越级被 Runtime 消费；
- normal stream 与 cancel 共享同一个 request-state / certification result，重复调用必须复用 immutable proof 而不是重复 mailbox cleanup；
- `CANCELLED_*` / certified natural proof 在 Runtime durable terminal 成功前不得因 owner stream return、adapter yield 或 cancel caller disconnect 被清除；
- Runtime natural owner 或 cancellation settlement 在 durable terminal 后调用 provider-neutral、本地同步幂等的 `reclaim_request()` ack，再回收 registry ownership；已有 in-flight local reference 可继续只读完成；
- retire/shutdown 只能在 settlement 收口后清 residual proof；Runtime crash 则允许 in-memory proof 丢失，重启继续用既有 `sent -> indeterminate_after_restart` 法律，不引入 durable cancel-intent schema；
- 无 raw stdout/tool content，acceptance 证明 steady-state 无持续增长。

若为做到这一点需要引入大范围 provider state machine / SQLite schema，触发 HOLD，不自行扩张。

---

## 3.6 Runtime Graceful Shutdown

现有 `shutdown()` 顺序要适配 Runtime-owned settlement task，并关闭 admission/snapshot 竞态：

```text
1. acquire lifecycle/admission lock
   -> self._shutting_down = True
   -> atomically stop new turn + new OPEN->CANCELLING registrations
   -> snapshot every cancellation_task already admitted
   -> release lock

2. await asyncio.shield(each snapshotted task) to completion
   -> 不得 task.cancel()
   -> caller disconnect 与 shutdown 都不能取消 settlement owner

3. settlement tasks 全部完成后
   -> terminalize_open_requests_for_shutdown() only for truly remaining-open rows

4. provider.shutdown()

5. clear/reclaim arbiter + provider receipt residuals
```

一个 cancel 要么在 lifecycle lock 内先完成 task registration 并必然进入 snapshot，要么在 shutdown 关闭 admission 后不得新建 settlement task；禁止“检查通过但尚未登记”的夹层状态。已处于 `CANCELLING` 的 duplicate caller 可 join 既有 task，但不能创建第二个。

为什么**不设置“2 秒后 task.cancel”**：

- cancellation task 自身就是 terminal truth owner；主动 cancel 它会重新制造 R6 的半结算状态。
- AGY physical cancel 已有内部 bounded process timeout，且 CP4 acceptance 要证明 active hard-cancel `<2s`；若 graceful shutdown 中 settlement 无法结束，这是实现/环境 failure，应让 shutdown fail/HOLD，而不是用第二个 cancellation 覆盖真理。

若进程不是 graceful shutdown 而是直接 crash，继续沿用既有 restart law：durable `sent` without terminal → conservative `indeterminate_after_restart`。

---

## 3.7 ExoCore Continuity / Partial Output

Stopped turn：

- canonical user Message 保留；
- UI 可已经看见 partial content/reasoning；
- `RuntimeHappyPathResult` 可携带 partial 用于本次流结束呈现；
- 不创建 canonical assistant Message；
- 不推进 committed coverage；
- service 发送 `stopped {partial: true}`；
- 下一轮 settlement 预期 `REBASE_CANONICAL_GAP`。

CP4 不处理 F4 UI partial persistence pending 项。

---

## 4. Race / Error Matrix

| 场景 | 权威行为 | Durable outcome |
|---|---|---|
| S1 Stop 在 runner/client 前已置位 | PREPARED local cancel，零 client / Runtime network | ExoCore stopped / not_sent |
| S2 Stop 在 ensure 期间到达 | ensure 可自然返回；pre-mark gate local cancel；TurnRequest 未发 | stopped / not_sent |
| S3 Stop 在 lazy stream 创建后、mark_sent 前 | close unstarted stream + PREPARED local cancel | stopped / not_sent |
| S4 Stop 在 mark_sent 后但 Runtime request 尚未注册 | 尝试 remote cancel；cancel-specific request-missing / identity-conflict 归 conservative indeterminate，不得留 unresolved SENT | indeterminate/recovery if unresolved |
| S5 Active AGY stream / long tool | watcher close → Runtime arbiter cancel → exact force kill | cancelled ↔ stopped |
| S6 Natural terminal 已 durable 后 Stop | terminal fast path；零 provider re-kill | original completed/failed/indeterminate |
| S7 AGY strict result candidate 已产生，但 mailbox 尚未认证，late cancel | supervisor candidate 不越级；adapter 完成 exact mailbox certification；success rescue done，validation failure rescue same failed/indeterminate | certified natural terminal，not guessed completed |
| S8 Natural owner 已占 `NATURAL_TERMINAL_PENDING` 后 cancel | cancel 只 wait done_event；owner claim→journal 间无 await | natural terminal |
| S9 Provider kill OS/adapter failure | 禁止 cancelled | indeterminate / cancel_cleanup_failed |
| S10 Request 非 current 且无正向 proof | 禁止猜 completed/cancelled | indeterminate / cancel_ownership_unknown |
| S11 HTTP cancel caller 中途断连 | shielded Runtime-owned task 继续 | eventual terminal |
| S12 多个并发 `/cancel` | 一个 settlement task；joiners 不重复 kill | one terminal; duplicate changed=False |
| S13 Runtime owner stream 被客户端断开 | provider cleanup force=True；exact abandoned receipt；`stream_turn` 内部 cancel 也汇入同一 arbiter | one terminal |
| S14 shutdown 与 active cancel registration 竞态 | lifecycle lock 原子 close admission + snapshot；已 admitted task 必被等待 | one terminal, then provider shutdown |
| S15 stopped 后下一 user turn | stopped user message 为 canonical gap | REBASE_CANONICAL_GAP |
| S16 cancel 在 provider `prepare_turn()` await 期间赢 | arbiter 先 CANCELLING；prepare 返回后 owner 不再 mark/send；adapter exact prestart fence force-dispose | cancelled via CANCELLED_PRESTART or honest indeterminate |
| S17 owner 与 cancel 竞争“最后 check → stdin write” | artifact→binding lock start fence 决定先手；cancel 先赢则旧 owner 永不写 stdin，owner 先赢则 cancel exact-kill active process | one terminal, no ghost execution |
| S18 pre-stream resolution/bootstrap/prepare/send-boundary terminal 与 cancel 竞争 | whole-request arbiter 决定唯一 terminal owner | exactly one terminal |
| S19 provider-certified receipt 已形成、owner stream 随后消失 | proof 保留到 Runtime durable ack；cancel task 可救援 | certified terminal, no proof loss |

---

## 5. Minimal Implementation Scope

### 5.1 ExoCore

预计触碰：

- `agents/runtime_turn.py`
  - pre-send gates；
  - `_StreamStopWatcher`；
  - Stop-interrupted exception priority。
- `bridge/subscription_runtime/bindings.py`
  - `cancel_turn(..., client: RuntimeClientProtocol | None)`；
  - 仅 PREPARED 允许 `None`；
  - post-mark cancel control uncertainty 必须投影为 local INDETERMINATE / may-have，而不是留下 SENT。
- `bridge/subscription_runtime/client.py` **仅在需要 operation-specific effect classification 时**：
  - cancel 的 request-not-registered / 409 identity-conflict 可保守 override 为 indeterminate；
  - 不得改变其它 operation 的 identity-conflict 语义。
- 对应 tests。

**原则上不需要改成功 wire contract / DB schema / migration。** 允许新增一个 metadata-only cancel safe error code 作为比 effect override 更窄的实现，但不得扩张响应 body 结构。

### 5.2 ExoCore-Runtime

预计触碰：

- `src/exocore_runtime/providers/base.py`
  - provider-neutral `ProviderCancelOutcome / ProviderCancelReceipt`；
  - `RuntimeProviderAdapter.cancel` 返回 contract；
  - provider-neutral `reclaim_request()` durable-ack seam。
- `src/exocore_runtime/providers/fake.py`
  - deterministic cancel receipts / prestart/start-fence/race fixtures。
- `src/exocore_runtime/providers/antigravity/adapter.py`
  - adapter 是 natural terminal certification owner；process candidate 经过 exact mailbox validation 后才映射 provider-neutral receipt；
  - prepared/start/cancel 使用统一 artifact→binding lock order；
  - existing ephemeral cleanup / fatal-generation cleanup 继续 fail closed；
  - certified receipt 保留至 Runtime reclaim ack。
- `src/exocore_runtime/providers/antigravity/process.py`
  - `force=True`；
  - `current_request_id` claim/clear 与 exact-request close under binding lock；
  - process candidate / abandoned proof lifecycle，禁止直接冒充 provider terminal。
- `src/exocore_runtime/service.py`
  - whole-request arbiter；
  - Runtime-owned cancellation settlement task；
  - all-terminal-path gate + provider-await progression checks；
  - lifecycle/admission lock + graceful shutdown ordering；
  - durable terminal 后 provider receipt reclaim。
- 对应 unit / integration / acceptance tests。

### 5.3 明确禁止

- 不改 state-store schema；若实现发现必须加 durable `cancelling` 状态/迁移，立即 HOLD 回审。
- 不改 HTTP CancelResult schema。
- 不借机重构 provider lifecycle unrelated code。
- 不触碰 CP2/CP3/CP5。

---

## 6. Deterministic Test Matrix

所有 race tests 使用 `threading.Event` / `asyncio.Event` / barrier/future；禁止靠随机 `sleep()` 猜时序。

### 6.1 ExoCore

必须至少覆盖：

1. `pre_ensure_stop`：client factory 0 次；PREPARED → stopped/not_sent。
2. `stop_during_ensure`：ensure 返回后在 mark_sent 前 local stop；TurnRequest 0 次。
3. `final_pre_mark_stop`：lazy stream 已建但未消费；close + local stop。
4. `blocked_stream_stop`：watcher 打断 blocking iterator；只有 runner 调 `cancel_turn()`。
5. watcher close 导致 `RuntimeClientError` 时，Stop 分支优先于普通 transport classifier。
6. `cancel` 返回 completed/failed 时，late Stop 不覆盖。
7. post-mark / remote-request-missing 模拟：cancel 409 identity-conflict（或新的 request-not-prepared safe code）必须走 cancel-specific indeterminate classifier；不得伪 stopped，也不得把 durable turn 留在 SENT。
8. duplicate Stop signal：watcher/cancel owner 不重复。
9. partial deltas 在 result 中保留，但 stopped 不创建 assistant Message、不推进 coverage。
10. stopped turn 下一轮 settlement → `REBASE_CANONICAL_GAP`。
11. watcher `disarm()` 后无存活线程/重复 close。

### 6.2 Runtime core / fake provider

必须至少覆盖：

1. current `cancelled-before-provider-kill` regression RED→GREEN：kill failure 绝不留下 cancelled。
2. `CANCELLED_PRESTART` → cancelled，且 fake provider send count 保持 0。
3. `CANCELLED_ACTIVE` → cancelled。
4. `CANCELLED_ABANDONED` → cancelled。
5. `OWNERSHIP_UNKNOWN` → indeterminate / cancel_ownership_unknown。
6. adapter-certified `NATURAL_TERMINAL_READY(done)` → completed，不 cancelled。
7. adapter-certified `NATURAL_TERMINAL_READY(error)` → 对应 failed/indeterminate terminal，不 cancelled。
8. natural owner 先 claim → late cancel 不调用 provider，等待 natural terminal；claim→journal 区间没有 await。
9. cancel arbiter 先 claim → owner 的 pre-stream error、done/error/exception/EOF 均不得抢写 terminal；等待/replay settlement winner。
10. cancel during `provider.prepare_turn()` → prepare 返回后 owner 不再 mark/send，最终无 ghost provider effect。
11. first cancel caller 被取消/断线 → Runtime-owned task 仍落 terminal。
12. concurrent duplicate cancel → provider cancel 1 次，joiner `changed=False`。
13. shutdown admission barrier race：cancel registration 与 shutdown snapshot 二选一原子排序；不存在漏 snapshot task。
14. shutdown during active cancellation → settlement 先完成；task 从未被 shutdown cancel。
15. provider `reclaim_request()` 只在 durable terminal + waiter release 之后发生；它是本地同步幂等 registry release，重复调用安全且不覆盖 terminal。
16. cancelled / natural-rescued journal restart replay 一致，terminal count=1。
17. arbiter registry 在所有 terminal path（含 pre-stream exits）回收。

### 6.3 AGY integration

必须至少覆盖：

1. exact prestart request：cancel 先拿 start fence → `CANCELLED_PRESTART`，stdin 0 次，旧 owner 不能随后 claim/write。
2. exact active request `force=True` kill。
3. abandoned stream cleanup `force=True`，无 5s graceful queue。
4. exact abandoned receipt：request A receipt 不能匹配 request B。
5. request-close/start TOCTOU：等待 binding lock 期间 session owner 变化时不得误杀新 request，也不得在 cancel fence 后启动旧 request。
6. process candidate boundary：quiet-after-result 已通过但 mailbox 尚未 validate 时 cancel，supervisor proof **不能**直接变 `NATURAL_TERMINAL_READY`。
7. mailbox certification success：exact receipt 验证后 late cancel 才得到 certified `NATURAL_TERMINAL_READY(done)`。
8. mailbox certification failure（missing/mismatch/invalid）：late cancel 与 normal stream 得到同一 failed/indeterminate terminal + 同一 fatal cleanup，不得 completed。
9. normal stream 与 late cancel 并发 certification：mailbox validation/cleanup 只执行一次，两方复用同一 immutable certified result。
10. certified receipt 在 owner stream return/disconnect 后仍保留，直到 Runtime `reclaim_request()` durable ack；registry removal 后，已有 in-flight local state reference 仍可只读完成；request B 不可覆盖/消费 A。
11. cleanup/kill failure 不写 success receipt。
12. receipt storage steady-state bounded / shutdown clear after settlement。
13. active process tree hard-cancel `<2.0s`，无 child orphan。

---

## 7. Acceptance Gates

### 7.1 ExoCore

```bash
bash .agent/check_real_db_baseline.sh

python.exe manage.py test \
  agents.tests.test_runtime_turn \
  bridge.tests.test_runtime_bindings \
  bridge.tests.test_subscription_runtime_client \
  bridge.tests.test_runtime_coverage -v 2

python.exe manage.py check
python.exe manage.py makemigrations --check --dry-run
```

若 CP4 新测试落在其他具名模块，checkpoint report 必须列出并纳入 focused suite。

### 7.2 ExoCore-Runtime

```bash
python -m unittest discover -s tests/unit -v
python -m unittest discover -s tests/integration -v
python -m unittest discover -s tests/acceptance -v
```

并执行两仓：

```bash
git diff --check
```

### 7.3 CP2 / CP3 regression

必须明确复跑受影响的：

- AGY tool policy / process argv tests；
- workspace persistence / restart tests；
- CP3 project-rules identity/materialization/restart tests。

任何 CP2/CP3 regression → HOLD。

---

## 8. Live Evidence — AGY 1.2.6

当前 `agy --version` = 1.2.6；现有版本 gate 接受 `<1.3`，但此前真实行为证据主要来自 1.2.5。CP4 必须补 1.2.6 live cancellation evidence。

要求：

- 独立 temp generation/profile/workspace；
- 不触碰 PID 13920 / 现有 production sessions；
- 让 AGY `run_command` 启动一个可识别的长运行 child process；
- 在 active tool execution 中触发 cancel；
- 记录 stop/cancel 起点、AGY parent/child PIDs、process-tree disappearance、Runtime terminal；
- 证明 `<2.0s` 且无 orphan；
- journal 最终恰好一个 terminal；
- ExoCore isolated E2E 如执行，则 wire `cancelled` 映射本地 `STATUS_STOPPED`，不出现误报 `recovery_required`。

不要求证明 hard-cancel 后同 AGY conversation REUSE；下一轮 continuity 仍按 canonical-gap REBASE 法律。

---

## 9. HOLD Triggers

施工中任一项成立立即停工：

1. 为正确 cancel 必须新增/迁移 SQLite request 状态（例如 durable `cancelling`）或大改 wire protocol。
2. 无法在 provider-neutral seam 表达 cancel proof，必须让 `RuntimeService` import AGY 私有类型。
3. exact-request start/close 无法在统一 serialization boundary 内证明，存在误杀下一 request 或 terminal 后 ghost send 风险。
4. process-level result candidate 必须绕过 adapter mailbox certification 才能救援 natural terminal。
5. active hard-cancel 1.2.6 实测 ≥2.0s 或出现 orphan。
6. kill/cleanup failure 仍可留下 durable `cancelled`。
7. natural terminal / cancel race 仍存在 non-terminal CancelResult、双 terminal、死锁或 proof 提前丢失。
8. graceful shutdown 的 admission close + task snapshot 无法原子化，或需要通过 `task.cancel()` 才能收尾 cancellation settlement。
9. post-mark remote-request-missing 仍会留下 unresolved `SENT`，或被误投影为 stopped。
10. stopped turn 被迫推进 coverage / 创建 assistant / 绕过 `REBASE_CANONICAL_GAP`。
11. CP2 / CP3 regression。
12. arbiter / watcher / provider receipt 生命周期无法 steady-state bounded cleanup。
13. 真实 DB baseline 偏移。

---

## 10. CP4 Implementation Stop Point

实现方完成后必须停在这里，不得自行进入 CP5，也不得直接把 CP4 宣布关闭：

1. 列出 changed files 与 state-machine delta；
2. 给出 deterministic race tests 结果；
3. 给出 Runtime unit / integration / acceptance 全量数字；
4. 给出 ExoCore focused tests + check + makemigrations；
5. 给出 CP2/CP3 regression 结果；
6. 给出真实 AGY 1.2.6 isolated hard-cancel evidence；
7. 给出两仓 `git diff --check` / `git status --short`；
8. **未 commit，等待独立 CP4 acceptance。**

> **Frozen verdict (REVISE-4 independent review PASS):** CP4 的施工核心不是“把 `force=False` 改成 `True`”，而是建立一条只有一个 durable terminal winner 的取消链：ExoCore watcher 只解阻塞，runner 单点请求 cancel；Runtime whole-request arbiter 与 lifecycle admission seam 选择 natural-vs-cancel/shutdown winner；provider 只有在 exact prestart/active/abandoned kill proof 或 **adapter-certified** natural terminal proof 成立时才能给出终态依据；AGY start/close 共用 exact-request serialization boundary；provider proof 保留到 Runtime durable ack。最终只有经过证明的 truth 才进入 journal，且 terminal 之后不能再产生新的 provider effect。
