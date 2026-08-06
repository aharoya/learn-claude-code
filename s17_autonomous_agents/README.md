# s17: Autonomous Agents — 自己看板，自己认领

[中文](README.md) · [English](README.en.md) · [日本語](README.ja.md)

s01 → ... → s15 → s16 → `s17` → [s18](../s18_worktree_isolation/) → s19 → s20

> *"自己看板，自己认领"* — 空闲时轮询，有活就干。
>
> **Harness 层**: 自治 — 队友自组织，不依赖 Lead 分配。

---

## 问题

s16 的队友能通信、能握手关机。但每个队友等 Lead 分配任务——如果任务看板上有 10 个未认领任务，Lead 得手动 assign 10 次。这不能扩展。队友应该自己看任务看板，发现没人做的任务就认领，做完再找下一个。

---

## 解决方案

![Autonomous Agents Overview](images/autonomous-agents-overview.svg)

沿用 S16 的教学版 MessageBus 和协议工具。本章新增：**idle_poll**（空闲时每 5 秒轮询一次）、**scan_unclaimed_tasks**（扫描看板上可认领的任务）、**自动认领**（找到任务就 claim，不用 Lead 操心）。

队友生命周期从两阶段变成三阶段：

| 阶段 | 行为 | 退出条件 |
|------|------|---------|
| WORK | inbox → LLM → 工具循环 | `stop_reason != tool_use` |
| IDLE | 每 5s 轮询 inbox + 任务板 | 60s 超时 |
| SHUTDOWN | 发 summary，退出 | — |

---

## 工作原理

### idle_poll: 空闲轮询

队友完成当前任务后不退出，进入 IDLE 阶段——每 5 秒检查一次有没有新工作：

```python
IDLE_POLL_INTERVAL = 5   # seconds
IDLE_TIMEOUT = 60         # seconds

def idle_poll(name, messages, role) -> str:
    """Return 'work', 'shutdown', or 'timeout'."""
    for _ in range(IDLE_TIMEOUT // IDLE_POLL_INTERVAL):
        time.sleep(IDLE_POLL_INTERVAL)

        # ① 检查收件箱（优先）
        inbox = BUS.read_inbox(name)
        if inbox:
            # shutdown_request 立即处理
            for msg in inbox:
                if msg.get("type") == "shutdown_request":
                    # ... 回复 shutdown_response
                    return "shutdown"
            # 普通消息注入上下文，回到 WORK
            messages.append(...)
            return "work"

        # ② 扫描任务看板
        unclaimed = scan_unclaimed_tasks()
        if unclaimed:
            task = unclaimed[0]
            result = claim_task(task["id"], name)
            if "Claimed" in result:
                messages.append(...)
                return "work"
    return "timeout"
```

inbox 优先（可能包含 shutdown_request 等协议消息），任务板其次。IDLE 阶段收到 shutdown_request 会直接回复并退出，不等到下一轮 WORK。

### scan_unclaimed_tasks: 扫描任务看板

找 pending 状态、无 owner、所有依赖已完成（`can_start`）的任务：

```python
def scan_unclaimed_tasks() -> list[dict]:
    unclaimed = []
    for f in sorted(TASKS_DIR.glob("task_*.json")):
        task = json.loads(f.read_text())
        if (task.get("status") == "pending"
                and not task.get("owner")
                and can_start(task["id"])):
            unclaimed.append(task)
    return unclaimed
```

三个条件：必须是 pending、没有 owner、所有 blockedBy 依赖已完成。`can_start` 检查依赖任务的状态——有依赖不代表不能做，只有被未完成的任务阻塞才不能做。教学版按文件名排序取第一个；CC 用文件锁防止多个队友同时认领同一个任务。

### claim_task: owner 检查

自动认领时检查 claim 结果，不把失败当成功：

```python
def claim_task(task_id: str, owner: str = "agent") -> str:
    task = load_task(task_id)
    if task.status != "pending":
        return f"Task {task_id} is {task.status}, cannot claim"
    if task.owner:
        return f"Task {task_id} already owned by {task.owner}"
    if not can_start(task_id):
        return f"Blocked by: {deps}"
    task.owner = owner
    task.status = "in_progress"
    save_task(task)
    return f"Claimed {task.id} ({task.subject})"
```

教学版没有文件锁，并发认领可能出现竞争。但至少 `task.owner` 检查避免了最明显的"后写覆盖"问题。CC 用 `proper-lockfile` 保护任务文件，`claimTask` 在文件锁内完成读-改-写（`utils/tasks.ts:541-612`）。

### 队友生命周期: WORK → IDLE → SHUTDOWN

s16 的队友做完任务就退出。s17 加了 IDLE 阶段，队友在外层循环中反复 WORK → IDLE：

```python
# Outer loop: WORK → IDLE cycle
while True:
    # WORK phase: 内层循环（最多 10 轮 LLM 调用）
    for _ in range(10):
        # 检查 inbox、处理协议消息、调 LLM、执行工具
        ...
        if response.stop_reason != "tool_use":
            break  # WORK 阶段结束

    # IDLE phase
    idle_result = idle_poll(name, messages, role)
    if idle_result == "shutdown":
        break
    if idle_result == "timeout":
        break  # 60s 超时 → SHUTDOWN

# SHUTDOWN: 发 summary 给 Lead
BUS.send(name, "lead", summary, "result")
```

关键设计：
- **外层 while True**：WORK 和 IDLE 交替进行，直到超时或收到关机请求
- **内层 for 10**：WORK 阶段最多 10 轮 LLM 调用（防止无限循环）
- **IDLE 超时 60 秒**：12 次轮询 × 5 秒 = 60 秒。超时后发送 summary 并退出
- **shutdown_request 两阶段都能响应**：WORK 阶段通过 `handle_inbox_message` 分发；IDLE 阶段 `idle_poll` 直接检查并回复

### 身份重注入

autoCompact（s08）之后，队友的 messages 列表可能被压缩成一段摘要。每次进入新的 WORK 阶段时检查：

```python
if len(messages) <= 3:
    messages.insert(0, {"role": "user",
        "content": f"<identity>You are '{name}', role: {role}. "
                   f"Continue your work.</identity>"})
```

消息过短说明发生了压缩，此时重新注入身份信息。真实 CC 中 context compaction 会保留 system prompt，教学版的简化实现需要手动处理。

### consume_lead_inbox: 统一 inbox 消费

`check_inbox` 工具和主循环末尾都调用同一个 `consume_lead_inbox()` 函数：先路由协议 response 更新状态，再把所有消息注入 Lead 的对话历史。队友发来的 summary/result 不会只打印在终端，Lead 的 LLM 能看到并协调下一步。

### 合起来跑

```
1. Lead: "搭建后端——任务太多，让队友自己认领"
2. Lead → create_task("创建数据库 schema")
3. Lead → create_task("写 API 路由")
4. Lead → create_task("写单元测试")
5. Lead → spawn_teammate("alice", "backend", "你是后端开发者")
6. Lead → spawn_teammate("bob", "backend", "你是后端开发者")

7. alice 线程启动 → WORK: 没有初始 inbox → 空转 → IDLE
8. bob 线程启动 → WORK: 没有初始 inbox → 空转 → IDLE

9. alice IDLE 第 1 次轮询 → scan_unclaimed → 发现"创建数据库 schema"
10. alice → claim_task → "创建数据库 schema" → 回到 WORK
11. bob IDLE 第 1 次轮询 → scan_unclaimed → 发现"写 API 路由"
12. bob → claim_task → "写 API 路由" → 回到 WORK

13. alice WORK: write_file("schema.sql", ...) → complete_task → WORK 结束
14. alice IDLE → scan → "写单元测试" → claim → WORK
15. alice WORK: write_file("test_api.py", ...) → complete_task → WORK 结束
16. alice IDLE → 60s 无新任务 → SHUTDOWN

17. bob 类似流程 → 做完 → SHUTDOWN
18. Lead consume_lead_inbox → 看到 alice 和 bob 的 summary
```

两个队友并行认领、并行工作。Lead 只需要创建任务和启动队友，不需要手动分配。

---

## 相对 s16 的变更

| 组件 | 之前 (s16) | 之后 (s17) |
|------|-----------|-----------|
| 任务分配 | Lead 手动 assign | 队友自动认领（can_start 检查依赖） |
| 队友状态 | WORK → IDLE（每 1s 轮询 inbox）→ WORK / SHUTDOWN | WORK → IDLE（每 5s 轮询 inbox + 任务板，60s 超时）→ WORK / SHUTDOWN |
| claim_task | 无 owner 检查 | 拒绝已有 owner 的任务 |
| IDLE 阶段关机 | 收到 shutdown_request 后退出 | 直接 dispatch shutdown 并退出 |
| Lead inbox | consume_lead_inbox 路由协议响应并注入上下文 | 沿用 consume_lead_inbox 机制 |
| 新函数 | 已有 consume_lead_inbox | idle_poll, scan_unclaimed_tasks（沿用 consume_lead_inbox） |
| 身份保持 | 仅 system prompt | 压缩后自动重注入 |
| Lead 工具 | 14 | 14（不变） |
| 队友工具 | 5 | 8（+ list_tasks, claim_task, complete_task） |
| 队友退出条件 | WORK 完进入 IDLE，等待 shutdown_request 后退出（无超时） | 60s 无新任务或收到 shutdown_request 后退出 |

---

## 本节核心回顾（复习用）

### 自治闭环：三个新函数串成一条链

s17 的所有新机制可以浓缩成一个闭环，三个函数接力：

```
scan_unclaimed_tasks → claim_task → idle_poll(下一轮)
       │                  │              │
   找可认领的任务      认领并置 in_progress   回 WORK 干活 / 继续等
```

- **scan_unclaimed_tasks（扫描）**：`pending + 无 owner + 依赖全完成` 三条件过滤，找出"没人做、又能做"的任务
- **claim_task（认领）**：三道校验（状态/owner/依赖）后才写入 owner + `in_progress`——教学版靠 owner 检查防"后写覆盖"，但没有文件锁，仍有 TOCTOU 窗口
- **idle_poll（轮询）**：空闲时每 5s 唤醒，inbox 优先（shutdown 不能被饿死），其次扫描看板认领

### 生命周期状态机：WORK → IDLE → SHUTDOWN

```
WORK ──(模型结束本轮)──→ IDLE ──(60s 无活/收到 shutdown)──→ SHUTDOWN
  ↑                        │
  └──(收到消息/认领成功)────┘
```

两个要点：
1. **两处 inbox 读取点**：WORK 阶段走 `handle_inbox_message`（协议分发 + 返回是否退出）；IDLE 阶段 `idle_poll` 直接检查 shutdown_request 并立即响应——所以"正在干活"和"闲着没事"两个状态下，关机请求都不会被漏掉
2. **合作式退出**：线程没有 `terminate()`，靠 `return` 自然结束。WORK 里 `should_shutdown=True` → break 外层循环；IDLE 里返回 `"shutdown"`/`"timeout"` → break——最终都落到循环外的 summary 发送

### 协议：两种、一套机制（s16 继承）

s17 的协议机制和 s16 完全一样，**没有新增协议类型**。两种协议，每种由一对 request/response 消息组成：

| 协议 | 消息类型（一对） | 发起方 | 响应方 |
|------|-----------------|--------|--------|
| **shutdown**（关闭握手） | `shutdown_request` ↔ `shutdown_response` | Lead | 队友 |
| **plan_approval**（计划审批） | `plan_approval_request` ↔ `plan_approval_response` | 队友 | Lead |

两种协议共用同一套关联机制：`request_id` 关联 + `ProtocolState` 状态机（pending → approved/rejected）+ `match_response` 类型校验。

**"协议" ≠ "消息类型"**：协议是一对 request-response 消息，靠 request_id 关联、进入状态机；普通消息（`message` 类型，如 `run_request_plan` 发的那条、队友的 `result` summary）没有 request_id、不进入状态机，只是普通通信，不构成协议。

两条链方向相反：shutdown 是 **Lead 发起**（要关人），plan_approval 是 **队友发起**（要审批）——同一个机制、两个方向。

### 竞态：没有文件锁的认领

教学版 `claim_task` 的 owner 检查是"读时判断"，两个队友可能同时读到 `owner=None` 再同时写入（TOCTOU）。这在实际演示中概率很低（5s 轮询间隔拉开窗口），但要知道：**真正并发安全需要文件锁/原子操作**——CC 用 `proper-lockfile` 在锁内完成读-改-写，这是教学版刻意省略、留给读者思考的点。

### inbox 消费式读取：展示工具为何会清空邮箱

`run_check_inbox` 名义上是"读取并展示收件箱"，但调用它之后 Lead 的邮箱会被清空——这看起来矛盾。逐层拆开看，结论分两层：**清空本身没错，错的是 `run_check_inbox` 复用了消费式读取函数。**

#### 一、清空是"继承"来的，不是 check_inbox 有意为之

`run_check_inbox` 自己没有任何读取逻辑，它调的是统一入口 `consume_lead_inbox`，而底层是消费式读取（读后即删）：

```python
# BUS.read_inbox —— 读后即删
msgs = [json.loads(line) for line in inbox.read_text().splitlines() if line.strip()]
inbox.unlink()          # ← 清空在这里

# consume_lead_inbox —— 路由协议 + 返回，清空是它实现的
msgs = BUS.read_inbox("lead")   # 这一行已经把文件删了
```

所以"展示工具读完后邮箱空了"是**继承来的副作用**，不是 check_inbox 的设计意图。如果它真是纯展示，不该清空。

#### 二、但清空是必要的——防重复消费

如果改成"只读不清空"，问题更大：

1. **同一批消息被注入两次**：check_inbox 以 `tool_result` 展示一次，主循环末尾又读一遍注入 `[Inbox]` 一次 → 模型看到双份，上下文冗余
2. **协议响应被 `match_response` 处理两次** → 这里有个 s17 的退化点，见下面的薄弱点 1
3. **模型反复调 check_inbox 永远看到旧消息**，无法区分新旧

所以"消费式邮箱"（读走 = 已处理）在这个架构里是对的。**真正的问题不是清空，而是 check_inbox 顶着"展示"的名义做了"消费"的事，副作用对调用者不可见。**

#### 三、真正的逻辑薄弱点（有两处）

**薄弱点 1：s17 精简掉了幂等校验（s16→s17 退化）**

对比两个版本：

```python
# s16 的 match_response —— 有第④步幂等校验
if state.status != "pending":
    return  # 已处理过的响应，忽略

# s17 的 match_response —— 只有 unknown + type mismatch 两步
state.status = "approved" if approve else "rejected"   # 直接改
```

当前因为"消费式读取 + 单线程顺序执行"，同一条响应只会被 `match_response` 碰一次，所以幂等校验缺失**暂时无害**。但这是一个埋在 s17 里的隐患：**只要有人把读取改成 peek（只读）或加了第二条消费路径，同一条响应就会被处理两次**——状态被重复覆盖、日志重复打印。s16 修掉的问题，在 s17 又敞开了门。

**薄弱点 2：两条消费路径互斥，导致行为不确定**

Lead 侧有**两个**入口都消费同一批消息：

```
路径A: agent_loop 中模型调用 check_inbox 工具 → 读走 + 清空
路径B: agent_loop 结束后主循环末尾 → consume_lead_inbox → [Inbox] 注入
```

**谁先调，谁拿走；后调的拿到空。** 于是"队友的 summary 以什么形式进入 Lead 历史"取决于模型这一轮**是否恰好调用了 check_inbox**：

- 模型调了 check_inbox → summary 在 `tool_result` 里，主循环末尾拿到空，`[Inbox]` 注入失效
- 模型没调 → summary 在 `[Inbox]` 里，作为明确的 user 消息

数据层面没丢（`tool_result` 也在 history 里），但**行为变得不确定**。还有个轻微后果：队友的 shutdown_response 如果被 check_inbox 读走并路由，状态机更新了，但 Lead 的 LLM 只能从那一轮 `tool_result` 里知道"队友已确认关机"——它**没有**被 `[Inbox]` 单独提示。状态机与 LLM 认知之间出现了脱节窗口。

#### 四、结论

| 观察 | 判断 |
|------|------|
| "展示用却清空" | 表面矛盾，但**清空是对的**（防重复消费），错在 check_inbox 复用了消费式函数却顶着展示的名义 |
| s17 的 match_response 无幂等校验 | **真实隐患**：s16 有、s17 精简掉了，目前无害但一改就爆 |
| 两条消费路径互斥 | 行为不确定，但不是数据丢失，是设计脆弱点 |

**改进方向**（如果要做）：给 `MessageBus` 加一个 `peek_inbox`（只读不删），`run_check_inbox` 用它做纯展示；消费只留给主循环末尾这一条路径。同时把 s17 `match_response` 的幂等校验补回来，避免重复响应覆盖状态。

---

## 试一下

```sh
cd learn-claude-code
python s17_autonomous_agents/demo_code.py
```

试试这个 prompt：

`Create 3 tasks on the board, then spawn alice and bob. Watch them auto-claim and work.`

观察重点：队友是否自动认领了未分配的任务？有 blockedBy 依赖的任务是否在前置完成后被正确认领？空闲超时后是否自动关机？IDLE 阶段收到 shutdown_request 是否立即响应？`.tasks/` 目录下的任务状态如何变化？

---

## 接下来

队友自组织了。但 Alice 和 Bob 都在同一个目录下工作——Alice 改 `config.py`，Bob 也改 `config.py`，互相覆盖。

s18 Worktree Isolation → 每个任务有自己的工作目录，互不干扰。

<details>
<summary>深入 CC 源码</summary>

> 教学说明：本章的 idle_poll + auto-claim 机制是教学设计，用统一的轮询函数演示"空闲后找活干"。CC 的实际实现是多个机制的组合，但目标一致——减少 Lead 的手动分配负担。

### 一、CC 的空闲机制：组合路径，不是单一轮询

教学版用一个 `idle_poll()` 统一处理空闲时的 inbox 检查和任务认领。CC 的实际实现是四个机制的组合：

**idle_notification**：队友完成一轮工作后，`sendIdleNotification()`（`inProcessRunner.ts:569-589`）向 Lead 发送空闲通知。Lead 知道队友可用了，可以分配新任务或请求关机。

**mailbox 轮询**：`waitForNextPromptOrShutdown()`（`inProcessRunner.ts:689-868`）是一个 **500ms 轮询循环**，持续检查三类来源：pending user messages、mailbox 文件消息、task list。shutdown_request 被优先处理（`inProcessRunner.ts:768-804`），不会被普通消息饿死。

**task watcher**：`useTaskListWatcher`（`hooks/useTaskListWatcher.ts:34-189`）用 `fs.watch()` 监听 `.claude/tasks/` 目录变化，1 秒 debounce，当新任务创建或依赖解锁时触发检查。依赖判断（`L197-207`）是"blockedBy 中没有未完成的任务"，不是"blockedBy 为空"。

**主动 claim**：轮询循环内部也会调用 `tryClaimNextTask()`（`inProcessRunner.ts:853-860`）——在等待期间主动从 task list 领取任务。所以"队友不主动轮询任务"不准确，CC 同时有被动通知和主动认领。

### 二、任务认领：文件锁 + 原子操作

`claimTask()`（`utils/tasks.ts:541-612`）用 `proper-lockfile` 的任务文件锁，在锁内完成读-检查-改-写。检查项：owner 是否已存在（`L575-576`）、是否已完成（`L580-581`）、blockedBy 中是否有未完成任务（`L585-594`）。`claimTaskWithBusyCheck()`（`utils/tasks.ts:614-692`）用 task-list 级别锁，把 busy check 和 claim 做成原子操作，避免 TOCTOU。

`findAvailableTask()`（`inProcessRunner.ts:595-604`）的依赖判断也是"所有 blockedBy 已完成"，用 `task.blockedBy.every(id => !unresolvedTaskIds.has(id))` 实现。`tryClaimNextTask()`（`inProcessRunner.ts:624-657`）在认领后把状态更新为 `in_progress`，让 UI 立即反映变化。

### 三、教学版 vs CC 对比

| 维度 | 教学版 (s17) | CC |
|------|-------------|-----|
| 空闲机制 | idle_poll 统一轮询（5s） | idle_notification + 500ms mailbox 轮询 + task watcher |
| 任务发现 | scan_unclaimed_tasks（轮询） | useTaskListWatcher（文件监听）+ tryClaimNextTask（主动轮询） |
| 依赖判断 | can_start（所有 blockedBy 已完成） | findAvailableTask（同样语义） |
| 并发安全 | owner 检查（无文件锁） | proper-lockfile 任务锁 + task-list 锁 |
| shutdown 处理 | IDLE 直接分发，WORK 通过 handle_inbox_message | 500ms 轮询中优先处理 shutdown_request |
| 超时退出 | 60s 无新任务 | 无固定超时，Lead 手动 shutdown |
| 身份保持 | messages 长度检测 | context compaction 保留 system prompt |
| claim 失败处理 | 检查返回值，失败不注入 | 文件锁保证原子性 |

教学版的 `idle_poll()` 把 CC 的四个机制合并成一个轮询函数——简化合理，因为核心语义（空闲时找活干、依赖解锁后可认领、shutdown 优先）是一致的。

</details>

<!-- translation-sync: zh@v2, en@v2, ja@v2 -->
