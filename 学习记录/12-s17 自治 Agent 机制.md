# s17 自治 Agent：自己看板，自己认领

> 本章把"队友等 Lead 分配任务"升级为"队友自己找活干"。核心是三个新函数的接力：`scan_unclaimed_tasks`（扫描）→ `claim_task`（认领）→ `idle_poll`（轮询），以及队友生命周期的三阶段化：**WORK → IDLE → SHUTDOWN**。读完你会明白：s16 的 idle loop 早已埋下伏笔，s17 只是让队友"多看了一眼任务看板"。

---

## 1. 要解决的问题

s16 的队友能通信、能握手关机。但**任务分配还是 Lead 手动**——看板上有 10 个未认领任务，Lead 就得 assign 10 次。这不能扩展。

s17 的思路转变：**队友不该等分配，应该自己看板、自己认领**。Lead 只负责创建任务 + 启动队友，之后的事交给队友自组织。

> 一句话：把"中央分配"改成"市场认领"。

---

## 2. 自治闭环：三个新函数接力

s17 新增的所有机制可以浓缩成一条链：

```
scan_unclaimed_tasks → claim_task → idle_poll(下一轮)
       │                  │              │
   找可认领的任务      认领并置 in_progress   回 WORK 干活 / 继续等
```

### 2.1 scan_unclaimed_tasks：扫描看板

找出"没人做、又能做"的任务，三个条件缺一不可：

```python
def scan_unclaimed_tasks() -> list[dict]:
    unclaimed = []
    for f in sorted(TASKS_DIR.glob("task_*.json")):
        task = json.loads(f.read_text())
        if (task.get("status") == "pending"      # ① 还没开始
                and not task.get("owner")         # ② 没人认领
                and can_start(task["id"])):       # ③ 依赖全部完成
            unclaimed.append(task)
    return unclaimed
```

**关键语义**：`can_start` 判断的是"blockedBy 中没有未完成的任务"，**不是 "blockedBy 为空"**。有依赖 ≠ 不能做，只有被未完成的依赖阻塞才不能做。

### 2.2 claim_task：认领（三道校验）

```python
def claim_task(task_id: str, owner: str = "agent") -> str:
    task = load_task(task_id)
    if task.status != "pending":   # ① 必须还没开始
        return f"Task {task_id} is {task.status}, cannot claim"
    if task.owner:                 # ② 必须没人认领
        return f"Task {task_id} already owned by {task.owner}"
    if not can_start(task_id):     # ③ 依赖必须完成
        return "Cannot start — ..."
    task.owner = owner             # 通过 → 写入 owner
    task.status = "in_progress"    #        状态置为 in_progress
    save_task(task)
    return f"Claimed {task.id} ({task.subject})"
```

`idle_poll` 认领时**检查返回值**（`if "Claimed" in result`），失败不注入上下文——这是 s17 的一个细节：不把失败当成功。

### 2.3 idle_poll：空闲轮询

```python
IDLE_POLL_INTERVAL = 5   # 每 5 秒轮询一次
IDLE_TIMEOUT = 60         # 60 秒无活 → 超时退出

def idle_poll(name, messages, role) -> str:
    for _ in range(IDLE_TIMEOUT // IDLE_POLL_INTERVAL):   # 12 次
        time.sleep(IDLE_POLL_INTERVAL)
        inbox = BUS.read_inbox(name)       # ① inbox 优先！
        if inbox:
            # shutdown_request → 回复并退出
            for msg in inbox:
                if msg.get("type") == "shutdown_request":
                    BUS.send(name, "lead", "Shutting down gracefully.",
                             "shutdown_response", {...})
                    return "shutdown"
            # 普通消息 → 注入上下文 → 回 WORK
            messages.append(...)
            return "work"
        unclaimed = scan_unclaimed_tasks()   # ② 其次扫描任务板
        if unclaimed:
            task = unclaimed[0]
            result = claim_task(task["id"], name)
            if "Claimed" in result:
                messages.append(...)
                return "work"
    return "timeout"    # ③ 12 次空转 → 超时
```

**为什么 inbox 优先？** shutdown_request 等协议消息必须立即处理，不能被普通消息挤到下一轮。s16 的教训：协议响应不能丢。

---

## 3. 生命周期状态机：WORK → IDLE → SHUTDOWN

s16 队友做完任务就退出。s17 加了 IDLE 阶段，外层 `while True` 让 WORK 和 IDLE 交替，直到超时或收到关机：

```
WORK ──(模型结束本轮)──→ IDLE ──(60s 无活/收到 shutdown)──→ SHUTDOWN
  ↑                        │
  └──(收到消息/认领成功)────┘
```

```python
while True:
    # 身份重注入（s17）
    if len(messages) <= 3:          # 消息过短说明被压缩过
        messages.insert(0, {"role": "user",
            "content": f"<identity>You are '{name}', role: {role}. ...</identity>"})

    # WORK 阶段：内层循环（最多 10 轮 LLM 调用）
    should_shutdown = False
    for _ in range(10):
        inbox = BUS.read_inbox(name)          # ① 读 inbox + 分发协议
        for msg in inbox:
            if handle_inbox_message(name, msg, messages):  # True=收到关机
                should_shutdown = True; break
        ...
        response = client.messages.create(...)  # ③ 调 LLM（I/O，释放 GIL）
        if response.stop_reason != "tool_use":
            break                              # 模型结束本轮
        # ④ 执行工具 ...
    if should_shutdown:
        break

    # IDLE 阶段（s17 新增）
    idle_result = idle_poll(name, messages, role)
    if idle_result in ("shutdown", "timeout"):
        break

# SHUTDOWN：发 summary 给 Lead，退出
summary = "Done."
...
BUS.send(name, "lead", summary, "result")
active_teammates.pop(name, None)
```

### 3.1 两处 inbox 读取点（易漏点）

| 位置 | 函数 | 处理方式 | 返回值 |
|------|------|---------|--------|
| WORK 阶段 | `handle_inbox_message` | 协议分发：shutdown → 回复+True；plan 审批 → 注入+False | True=退出 / False=继续 |
| IDLE 阶段 | `idle_poll` | 直接查 shutdown_request，立即回复并返回 | "shutdown"/"work"/"timeout" |

所以**"正在干活"和"闲着没事"两个状态下，关机请求都不会被漏掉**。

### 3.2 合作式退出（承接学习记录 10）

线程没有 `terminate()`，靠 `return` 自然结束：

- WORK 里 `should_shutdown=True` → break 外层循环
- IDLE 里返回 `"shutdown"`/`"timeout"` → break

最终都落到循环外的 summary 发送。整个过程是**标志位 + 循环条件 + 函数返回**的合作式取消。

### 3.3 身份重注入（s17 细节）

`autoCompact`（s08）之后，队友的 messages 可能被压缩成一段摘要。每次进入 WORK 前检查 `len(messages) <= 3`——消息过短说明发生了压缩，重新注入 `<identity>`。真实 CC 里 context compaction 会保留 system prompt，教学版简化实现需要手动处理。

---

## 4. 竞态：没有文件锁的认领

教学版 `claim_task` 的 owner 检查是**读时判断**，两个队友可能同时读到 `owner=None` 再同时写入（**TOCTOU**）。

```
线程 A: 读到 owner=None
线程 B: 读到 owner=None        ← 两个都认为"没人认领"
线程 A: 写入 owner=A
线程 B: 写入 owner=B           ← 后写覆盖，A 白干了
```

在实际演示中概率很低（5s 轮询间隔拉开窗口），但要知道：**真正并发安全需要文件锁/原子操作**。CC 用 `proper-lockfile` 在锁内完成读-改-写（`claimTask`），还用 task-list 级锁把 busy check 和 claim 做成原子操作（`claimTaskWithBusyCheck`），避免 TOCTOU。

教学版刻意省略这个——是留给读者思考的点。

---

## 5. 与 s16 的关系：继承与新增

| 机制 | s16 | s17 |
|------|-----|-----|
| 协议层（request_id + 状态机） | ✅ shutdown + plan_approval | 沿用 |
| 任务分配 | Lead 手动 | 队友自动认领 |
| 队友工具 | 5 个 | 8 个（+ list_tasks/claim_task/complete_task） |
| 队友生命周期 | WORK → IDLE（1s 轮询）→ WORK/SHUTDOWN | WORK → IDLE（5s 轮询 + 任务板，60s 超时）→ WORK/SHUTDOWN |
| IDLE 关机 | 收到 shutdown_request 后退出 | 直接 dispatch shutdown 并退出 |

s17 没有新增协议类型，**协议机制完全继承 s16**。核心增量在"队友如何自己发现活"。

---

## 6. 复习清单

- [ ] 自治闭环三函数接力：扫描 → 认领 → 轮询
- [ ] `can_start` 的依赖语义：不是 blockedBy 为空，而是没有未完成依赖
- [ ] `idle_poll` 的优先级：inbox（shutdown 不能饿死）> 任务板
- [ ] WORK→IDLE→SHUTDOWN 状态机 + 两处 inbox 读取点
- [ ] 合作式退出：无 terminate，靠标志位 + 循环条件 + return
- [ ] 身份重注入触发条件：`len(messages) <= 3`
- [ ] TOCTOU 竞态：owner 检查不原子，CC 用文件锁

---

## 7. 关联阅读

- `学习记录/10-s16 协议处理链路.md` — 协议机制（s17 完全继承）
- `学习记录/11-GIL 详解.md` — 为什么队友线程能并发（I/O 释放 GIL）
- `s17_autonomous_agents/demo_code.py` — 本文件数字编号注释版
- 下一步 `s18_worktree_isolation/` — 每个任务独立工作目录，解决"两人改同一文件"

---

**文档生成时间：** 2026-08-05
