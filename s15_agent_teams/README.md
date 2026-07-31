# s15: Agent Teams — 一个搞不定，组队来

[中文](README.md) · [English](README.en.md) · [日本語](README.ja.md)

s01 → ... → s13 → s14 → `s15` → [s16](../s16_team_protocols/) → s17 → s18 → s19 → s20
> *"一个搞不定, 组队来"* — 文件收件箱 + 队友线程。
>
> **Harness 层**: 团队 — 多 Agent 协作, 消息总线。

---

## 问题

"重构整个后端"涉及认证模块、数据库层、API 路由、测试。一个 Agent 在修 API 路由时，认证模块的细节已经不在上下文里了。上下文窗口就那么大，单个 Agent 的注意力覆盖不了所有模块。

s06 的子 Agent 是临时工，叫来干一件事就走了。但有些任务需要能通信、能协作的队友。

---

## 解决方案

![Agent Teams Overview](images/agent-teams-overview.svg)

教学代码沿用 S14 的能力（prompt 组装、任务系统、后台执行、cron 调度）。为了聚焦团队机制，省略了完整错误恢复、记忆和技能系统。新增三样：**MessageBus**（文件收件箱）、**spawn_teammate_thread**（启动队友线程）、**inbox 注入**（Lead 接收队友消息并注入 history）。

子 Agent vs 队友：

| | s06 子 Agent | s15 队友 |
|---|---|---|
| 生命周期 | 一次性，用完销毁 | 多轮（教学版限 10 轮，真实 CC 用 idle loop） |
| 通信 | 只回传结论 | 异步收件箱，随时通信 |
| 上下文 | 完全隔离 | 通过消息共享信息 |
| 数量 | 一个主 Agent + 偶尔子 Agent | 一个 Lead + 多个队友 |

---

## 工作原理

![Team Topology](images/team-topology.svg)

### MessageBus: 文件收件箱

每个 Agent（包括 Lead 和队友）有一个 `.jsonl` 邮箱。发消息 = 往对方的文件里 append 一行 JSON。读消息 = 读文件 + 删除（消费式）：

```python
class MessageBus:
    def send(self, from_agent: str, to_agent: str,
             content: str, msg_type: str = "message"):
        msg = {"from": from_agent, "to": to_agent,
               "content": content, "type": msg_type,
               "ts": time.time()}
        inbox = MAILBOX_DIR / f"{to_agent}.jsonl"
        with open(inbox, "a") as f:
            f.write(json.dumps(msg) + "\n")

    def read_inbox(self, agent: str) -> list[dict]:
        inbox = MAILBOX_DIR / f"{agent}.jsonl"
        if not inbox.exists():
            return []
        msgs = [json.loads(line) for line in inbox.read_text().splitlines()]
        inbox.unlink()  # 消费式：读完删除
        return msgs
```

为什么用文件而不是内存队列？教学版选文件是因为直观、跨线程可观察。真实 CC 也用文件收件箱（`~/.claude/teams/{team}/inboxes/`），但加了 `proper-lockfile` 防并发写冲突。教学版的 `read_inbox` 有 read + unlink 竞态，多线程同时读可能丢消息，对教学场景可以接受。

### spawn_teammate_thread: 启动队友

Lead 调用 `spawn_teammate` 工具启动一个队友。队友跑在自己的 daemon 线程里，有自己的 system prompt、自己的 messages、自己的简化工具集：

```python
def spawn_teammate_thread(name: str, role: str, prompt: str) -> str:
    system = f"You are '{name}', a {role}. Use tools to complete tasks."

    def run():
        messages = [{"role": "user", "content": prompt}]
        sub_tools = [bash, read_file, write_file, send_message]
        for _ in range(10):           # 最多 10 轮
            inbox = BUS.read_inbox(name)
            if inbox:
                messages.append({"role": "user",
                    "content": f"<inbox>{json.dumps(inbox)}</inbox>"})
            response = client.messages.create(
                model=MODEL, system=system, messages=messages[-20:],
                tools=sub_tools, max_tokens=8000)
            # ... 执行工具、处理结果
        # 完成后发 summary 给 Lead
        BUS.send(name, "lead", summary, "result")

    threading.Thread(target=run, daemon=True).start()
```

关键设计：
- **队友有简化工具集**：bash、read、write、send_message。教学版省略了任务和 cron，聚焦通信机制。真实 CC 的队友也有 TaskCreate、TaskUpdate 等工具，任务系统是团队共享的
- **教学版限 10 轮**：防止队友无限循环。真实 CC 用 idle loop：跑完一轮后发 `idle_notification`，等 inbox 消息，收到后继续，直到 `shutdown_request` 才退出
- **完成后自动汇报**：`BUS.send(name, "lead", summary)` 把最终结果发到 Lead 的收件箱

### Lead 的 inbox 注入

Lead 在每轮主循环结束后检查收件箱。队友发来的消息注入到 history 里，让 LLM 能看到并做出反应：

```python
# 主循环结束后
inbox = BUS.read_inbox("lead")
if inbox:
    inbox_text = "\n".join(
        f"From {m['from']}: {m['content'][:200]}" for m in inbox)
    history.append({"role": "user",
                    "content": f"[Inbox]\n{inbox_text}"})
```

教学版在用户输入循环外注入。CC 更精细，Lead 的 `useInboxPoller` 每 1 秒检查一次，有消息就提交为新的 turn，不需要等用户输入。

### 权限冒泡

教学版省略了权限冒泡。真实 CC 的流程（`permissionSync.ts`、`useSwarmPermissionPoller.ts`）：

1. 队友遇到需要审批的操作 → 发 `permission_request` 到 Lead 收件箱
2. Lead 的 `useInboxPoller` 检测到请求 → 路由到审批队列
3. 用户审批后 → Lead 发 `permission_response` 回队友
4. 队友的 `useSwarmPermissionPoller`（每 500ms 轮询）收到回复 → 继续或拒绝

### 合起来跑

```
1. Lead: "搭建后端：一个人搞不定，组队吧"
2. Lead → spawn_teammate("alice", "backend dev", "创建数据库 schema")
3. Lead → spawn_teammate("bob", "frontend dev", "写 API 客户端")
4. alice 线程启动 → 自己的 LLM 调用 → bash "python manage.py migrate"
5. bob 线程启动 → 自己的 LLM 调用 → write_file("client.ts", ...)
6. alice 完成 → BUS.send("alice", "lead", "Schema done: users, orders tables")
7. bob 完成 → BUS.send("bob", "lead", "Client written with types")
8. Lead 下次循环 → inbox 注入 history → LLM 看到 alice 和 bob 的结果
```

两个队友并行工作。

---

## 相对 s14 的变更

| 组件 | 之前 (s14) | 之后 (s15) |
|------|-----------|-----------|
| Agent 数量 | 1 | 1 Lead + N 队友线程 |
| 通信 | 无 | MessageBus + .mailboxes/*.jsonl |
| 新类 | — | MessageBus, active_teammates dict |
| 新函数 | — | spawn_teammate_thread, run_send_message, run_check_inbox |
| Lead 工具 | 11 (s14) | + spawn_teammate, send_message, check_inbox (14) |
| 队友工具 | — | bash, read_file, write_file, send_message (4) |
| 权限 | 本地决策 | 教学版省略（真实 CC 有冒泡机制） |

---

## 注意事项

### 1. 文件收件箱的 read + unlink 竞态

```python
def read_inbox(self, agent: str) -> list[dict]:
    inbox = MAILBOX_DIR / f"{agent}.jsonl"
    msgs = [json.loads(line) for line in inbox.read_text().splitlines()]  # ① 读
    inbox.unlink()                                                        # ② 删
    return msgs
```

`read_inbox` 是"读文件 + 删文件"两步操作，不是原子的。如果两个线程同时调用 `read_inbox("lead")`，可能出现：
- 两个线程都读到内容，一个删了文件，另一个 `unlink` 报错（`FileNotFoundError`）
- 或者更糟：线程 A 读完还没删，线程 B 又读了一次 → 同一批消息被消费两次

教学版没有文件锁，`send` 的 append 靠 OS 保证原子性（每次写一行不会互相覆盖），但 read + unlink 的竞态是真实存在的。真实 CC 用 `proper-lockfile` 文件锁解决。教学场景下，只要保证"同一时间只有一个消费者"（实际上主循环的 wake 分支和 LLM 的 check_inbox 工具可能同时触发）就能避免——代码里靠 `if not parts: continue` 兜底。

### 2. peek vs read_inbox：判断用 peek，消费用 read_inbox

```python
# inbox_poller 用 peek —— 只看"有没有"，不消费
if BUS.peek("lead") or has_pending_background():
    events.put(("wake", None))

# 主循环真正处理时才 read_inbox —— 消费消息
inbox = BUS.read_inbox("lead")
```

这是一个重要的区分：
- **peek**：非破坏性，只检查文件是否存在且非空。用于后台线程做"该不该唤醒"的判断
- **read_inbox**：破坏性，读走消息并删除文件。用于真正处理消息时

如果 inbox_poller 用 read_inbox 检查，就会把消息提前消费掉——主循环想读时已经是空文件了，队友的成果就丢了。**判断和消费必须用不同的方法。**

### 3. check_inbox 工具 vs inbox_poller 注入：谁先消费谁拿到

s15 里 Lead 接收队友消息有两条路径：

| 路径 | 触发者 | 消费方式 |
|------|--------|---------|
| check_inbox 工具 | LLM 主动调用 | LLM 读到消息自己处理 |
| inbox_poller 注入 | 后台线程检测到消息 | 主循环构造 [Inbox] 注入 history |

两条路径都调用 `BUS.read_inbox("lead")`。**消息队列语义：谁先消费谁拿到。** 如果 LLM 先用 check_inbox 工具读了消息，inbox_poller 检测到文件已经空了，就不会注入；反过来如果 inbox_poller 先注入，LLM 再调 check_inbox 就返回 "(inbox empty)"。

这不算 bug，是"每条消息只有一个消费者"的设计。但调试时要注意：如果队友消息"莫名消失"，先检查是不是被另一条路径消费了。

### 4. 事件驱动架构：为什么 input() 要放进独立线程

```python
events = queue.Queue()  # 线程安全的 FIFO 队列

def input_reader():
    while True:
        line = input("s15 >> ")   # 阻塞等待用户输入
        events.put(("user", line))
```

之前章节的主循环是 `input() → agent_loop`，用户不输入，Agent 完全空闲。s15 引入了**异步队友**——队友消息随时可能到达。如果把 input() 放主循环，队友消息到达时主循环正阻塞在 input() 上，没人处理。

所以 s15 改成**事件驱动**：input_reader 和 inbox_poller 两个后台线程把事件放进 `queue.Queue`，主循环 `events.get()` 阻塞等待，谁来了处理谁。这样即使没有用户输入，队友消息也能触发新的 turn。

**Python 说明**：`queue.Queue` 是线程安全的——多个线程同时 `put` 不会乱，主循环 `get()` 按 FIFO 顺序取出。事件统一格式化为 `(kind, payload)` 二元组。

### 5. 队友的 send_message handler 用了 lambda 技巧

```python
"send_message": lambda to, content: (BUS.send(name, to, content), "Sent")[1],
```

这个写法对 Python 初学者可能很费解，拆开看：
1. `BUS.send(name, to, content)` 发送消息，但函数本身**不返回值**（返回 None）
2. `(BUS.send(...), "Sent")` 构造一个二元组：`(None, "Sent")`
3. `[1]` 取出元组的第二个元素 → 返回 `"Sent"`

**为什么这么绕？** 因为 `execute_tool` 里的 `handler(**block.input)` 要求 handler 有返回值——返回值会作为 `tool_result` 给 LLM。而 `BUS.send` 返回 None，如果直接映射，LLM 收到一个空的 tool_result，不知道消息到底发没发成功。包装一层后，LLM 明确收到 "Sent"。

更易读的等价写法：
```python
def send_handler(to, content):
    BUS.send(name, to, content)
    return "Sent"
```

### 6. 队友上下文截断：messages[-20:]

```python
response = client.messages.create(
    model=MODEL, system=system, messages=messages[-20:],  # 只保留最近 20 条
    tools=sub_tools, max_tokens=8000)
```

**Python 说明**：`messages[-20:]` 是列表切片——从倒数第 20 个元素取到末尾，丢弃更早的消息。

这意味着队友看到的历史**最多只有最近 20 条消息**。如果队友干了 30 轮，最初的任务描述（第 1 轮）早就被挤出去了——队友可能"忘记"任务是什么。

对比 Lead：Lead 的 `agent_loop` 用完整 `messages`（不截断），靠 s08 的上下文压缩机制控制长度。队友没有压缩机制，用硬截断替代。**如果队友需要长期记住某个信息，必须显式写在 send_message 的消息里或写到文件里。**

### 7. 队友完成摘要的提取：for...else 语法

```python
summary = "Done."
for msg in reversed(messages):                 # 从最后一条往前翻
    if msg["role"] == "assistant" and isinstance(msg["content"], list):
        for b in msg["content"]:
            if getattr(b, "type", None) == "text":
                summary = b.text               # 找到文本就取走
                break
        else:                                  # ★ Python 的 for...else
            continue
        break
```

**Python 的 for...else 语法**：`for` 循环如果"正常跑完"（没遇到 break）就执行 `else` 块。这里是"双重 break"模式：
- 内层 for 找到 text block → `break`（跳回外层，走 `break` 结束整个循环）
- 内层 for 没找到 → `else: continue`（继续外层循环，往前翻更早的 assistant 消息）

**效果**：从最后一条 assistant 消息往回找第一条文本。找到就用它当摘要，找不到就用默认 "Done."。

**问题**：这个"往回找"可能找到几轮前的中间结果，而不是队友的最终结论——如果队友最后一步是工具调用（没有文本），摘要就是上一轮的文本。教学版简化了这个逻辑。

### 8. active_teammates 的竞态：消息可能先于注册移除到达

```python
active_teammates[name] = True          # spawn 时注册
# ...
BUS.send(name, "lead", summary, "result")   # 队友发完最终消息
active_teammates.pop(name, None)            # 然后才移除注册
```

队友"发最终消息"和"从 active_teammates 移除"是两条语句，顺序是发消息 → 移除。所以**队友的最终消息可能已经进了 Lead 收件箱，但 active_teammates 里还有这个队友**。

这就是 inbox_poller 注释里说的"不依赖 active_teammates 判断"的原因——如果 poller 用 active_teammates 判断"还有队友在跑，就不检查收件箱"，可能错过队友刚发来的最终消息。

### 9. 队友不能递归 spawn：工具集硬限制

```python
sub_tools = [bash, read_file, write_file, send_message]  # 没有 spawn_teammate
```

队友的工具集只有 4 个，没有 `spawn_teammate`。这是**硬性禁止递归**——队友无法创建队友，防止无限递归。真实 CC 在 `AgentTool.tsx:273` 明确禁止 "teammates spawning other teammates"。

同样，队友也没有 `check_inbox`（不能看 Lead 的收件箱，上下文隔离）、没有 cron 工具、没有任务工具。队友的工具集刻意最小化，聚焦"干活 + 汇报"两件事。

### 10. daemon 线程：队友可能被强制杀死

```python
threading.Thread(target=run, daemon=True).start()
```

队友跑在 daemon 线程里。主线程退出（用户按 Ctrl+C）时，队友线程被**强制杀死**——即使它正在写文件、正在调 LLM。写到一半的文件可能损坏。

s16 的"关机握手"就是为了解决这个：Lead 发 `shutdown_request`，队友收到后收尾再退出，而不是被强杀。

### 11. daemon 线程阻塞在 stdin：退出时 Fatal Python error

```python
threading.Thread(target=input_reader, daemon=True).start()  # input_reader 阻塞在 input()

# 主循环退出时：
#   break → 解释器 shutdown → 要关闭 stdin → 但 daemon 线程占着 stdin 的锁
#   → Fatal Python error: _enter_buffered_busy
```

这是事件驱动架构（input 放线程）带来的一类隐蔽崩溃：

1. `input_reader` 是 daemon 线程，一直阻塞在 `input()` 等用户输入
2. 主线程 break 退出 → `__main__` 结束 → Python 解释器开始 finalizing
3. shutdown 要关闭 stdin（`BufferedReader`），需要获取它的缓冲锁
4. 但 daemon 线程还占着这把锁（阻塞在 `input()` 上）
5. 主线程拿不到锁 → `Fatal Python error: _enter_buffered_busy: could not acquire lock for <stdin>`

**为什么其他 daemon 线程没问题？** `inbox_poller`、`cron_scheduler_loop` 都阻塞在 `time.sleep()` 上——sleep 是 Python 级、可重入的，shutdown 能顺利接管。**stdin 是 C 级缓冲锁**，正被阻塞线程持有时无法被优雅关闭，于是死锁。

**为什么 Ctrl+C 退出是安全的？** `input_reader` 捕获 `KeyboardInterrupt` 后自己 `return` 了——线程已死，不再持有锁。只有正常输入 q/quit 触发 break 时，线程还活着阻塞，才触发崩溃。

**修复**：退出词由 `input_reader` 直接 `os._exit(0)` 终止进程，不依赖主循环：

```python
# input_reader 线程内：
if line.strip().lower() in ("q", "quit", "exit", ""):
    os._exit(0)   # 直接终止进程，绕开 stdin 死锁
```

为什么必须 `os._exit(0)` 而不是放事件等主循环处理？两个原因：
1. **stdin 死锁**：主循环 break 后解释器 shutdown 要关 stdin，但 daemon 线程还占着缓冲锁 → `_enter_buffered_busy`。`os._exit` 直接杀进程，跳过 shutdown 清理，从根上避免。
2. **主循环可能卡住**：详见第 12 条——主循环可能正卡在一轮很长的 agent_loop 里，quit 事件排不到。由 `input_reader` 独立线程直接 `os._exit` 立即生效，不受主循环状态影响。

> **通用教训**：把阻塞式 IO（`input()`）放进 daemon 线程后，"退出"不再简单。关键认知：**退出信号不应该走共享的事件队列**（那里可能被长任务堵住），而应由读输入的线程直接终止进程。`os._exit` 牺牲了优雅清理，换取"退出永远有效"的确定性——对 CLI 工具是可接受的取舍。真实 CLI 用信号处理（SIGINT/SIGTERM）管理线程退出，也是同样的权衡。

### 12. FIFO 事件队列：quit 会排队，输入多了"卡住"

```python
events = queue.Queue()          # FIFO：先入先出
while True:
    kind, payload = events.get()  # 严格按入队顺序处理
```

`q`/`quit` 如果走普通 `("user", ...)` 事件，会排在队列末尾。快速连续输入多条消息时，每一条都要先跑一轮 agent_loop（调 LLM 可能几秒到几十秒），quit 排在最后面**要等前面全部处理完才轮到**——表现为"输入 quit 没反应"。队友在后台跑还会产生 wake 事件继续插队，进一步推迟。

**修复**：`input_reader` 识别到退出词时**直接 `os._exit(0)`**，不经过事件队列：

```python
if line.strip().lower() in ("q", "quit", "exit", ""):
    os._exit(0)   # 独立线程直接终止进程，不受主循环/队列状态影响
```

> **教训**：FIFO 队列里，"优先级"信息会被入队顺序掩盖。低频但紧急的事件（退出、取消）不该走共享队列——应该用独立通道或直接终止。真实系统（消息队列、任务调度）都有优先级机制，就是这个原因。

### 13. 多线程并发写 stdout：日志可能交错

```python
# 没有锁：三个线程同时 print
# [teammate] a→b: hello...  [cron fire] job_x...  s15 >>  ← 可能字符级交错
```

s15 有 **5 个线程**同时写 stdout：主循环、`input_reader`、`inbox_poller`、cron 调度、队友。直接 `print` 时，一行输出可能被另一线程的半个 print 打断。

教学版**保留直接 `print`**，接受日志可能交错的现实——理由：加锁虽能保证原子性，但 `input()` 的提示符打印不经锁，照样会和其他输出混；且锁不能让提示符与日志分行。真正的解法是专用 TUI/行缓冲库（如 `rich`），教学版不引入。若你的终端日志乱到影响阅读，可自行加锁：

```python
print_lock = threading.Lock()
def log(*args, **kwargs):
    with print_lock:
        print(*args, **kwargs)
```

> **认知**：多线程并发写 stdout 是 CLI 的固有难题。加锁只能保证"单次 print 原子"，无法解决"提示符和日志同屏"——那是 UI 层的问题，需要行级渲染（TUI）而不是锁。

> **教训**：只要存在多线程并发输出，就必须用一个共享锁保护 stdout。这在并发系统的 UI 层（控制台、日志文件）是铁律。真实系统的 logger 也是线程安全的，就是这个道理。

### 与官方 Claude Code 对比

| 方面 | 教学版 s15 | 官方 Claude Code |
|------|-----------|-----------------|
| 通信方式 | MessageBus 类 + .mailboxes/*.jsonl | 直接写收件箱文件 + proper-lockfile |
| 消息类型 | 2 种（message / result） | 15 种结构化类型（idle、permission、shutdown 等） |
| 队友轮次 | 固定 10 轮后结束 | idle loop：空闲等消息，直到 shutdown_request |
| 权限 | 教学版省略 | 双向权限冒泡（permission_request/response） |
| 队友结束 | 发 summary 后自动退出 | 关机握手协议（shutdown_request/approved） |
| 队友工具 | bash/read/write/send（4 个） | 完整工具集 + TaskCreate 等团队共享任务 |
| 递归保护 | 工具集不包含 spawn_teammate | 显式禁止 + 深度限制 |
| 收件箱检查 | 每 1 秒 poller | useInboxPoller 每 1 秒 + 优先级队列 |

---

## 架构设计

从软件设计角度，s15 用了 4 个经典模式（按重要性排序）。这也是整本书里最"正统"的一章——一次用到四种架构设计。

### 1. Actor 模型（最核心）

> **每个 Agent 是一个 Actor：有独立的私有状态 + 自己的邮箱（mailbox），只通过异步消息通信，不共享内存。**

```python
# 每个 Agent = 独立线程 + 独立 messages[] + 独立邮箱文件
# lead.jsonl        ← Lead 的邮箱
# researcher.jsonl  ← 队友的邮箱
class MessageBus:
    def send(self, from_agent, to_agent, content):   # 往对方邮箱发消息
    def read_inbox(self, agent):                     # 读自己的邮箱（消费式）
```

这对应 Erlang/Elixir 的 Actor 模型、Akka 的 Actor、以及真实 CC 的团队实现。

Actor 模型的三个铁律，s15 全遵守了：

| Actor 铁律 | s15 的体现 |
|-----------|-----------|
| Actor 之间不共享内存 | 各自独立 messages[]，只通过文件邮箱通信 |
| 消息是异步的 | `spawn_teammate` 立即返回，队友后台跑 |
| 消息单向传递、不可变 | send 只 append，read 消费式删除 |

**与 s06 子 Agent 对比**：s06 是"函数调用"（同步调用 + 拿返回值），s15 是"消息传递"（异步发消息 + 邮箱收件）。前者是过程式思维，后者是分布式系统思维。

### 2. 生产者-消费者模式（解耦）

s14 已有雏形（cron_scheduler → cron_queue → agent_loop），s15 扩展成完整的解耦结构：

```
生产者                 缓冲                 消费者
──────────────────────────────────────────────────────
队友 (send_message) ─→ 邮箱文件 (.jsonl) ─→ Lead (read_inbox)
后台 worker ─────────→ background_results ─→ Lead (collect)
input_reader ────────→ events queue ──────→ 主循环
inbox_poller ────────→ events queue ──────→ 主循环
```

**核心价值**：生产者和消费者的生命周期完全解耦。队友写消息时 Lead 可能在调 LLM，不关心；Lead 读消息时队友可能已退出，也不关心。中间隔着一个文件/队列，谁先谁后、谁在不在都无所谓。

这就是真实系统（Kafka、RabbitMQ、Redis Streams）都用这个模式的原因——**解耦是扩展性的前提**。想让队友和 Lead 跑在不同进程甚至不同机器，只需把"文件邮箱"换成"网络消息队列"。

### 3. 事件驱动架构（Event Loop）

这是 s15 与之前所有章节最大的结构差异：

```
之前章节：主循环 = input() → agent_loop → 结束    ← 用户输入是唯一驱动力
s15：     主循环 = events.get() → 分发 → agent_loop ← 任何事件都能驱动
```

```python
events = queue.Queue()

def input_reader():  # 事件源 1：用户输入
    ...
def inbox_poller():  # 事件源 2：队友消息/后台任务
    ...

while True:
    kind, payload = events.get()   # 阻塞等事件
    if kind == "user": ...
    elif kind == "wake": ...       # 队友消息到达也触发一轮 agent_loop
```

这是**操作系统/浏览器/Node.js** 同款架构：
- Node.js 的事件循环：所有 IO 事件排队，单线程逐个处理
- 浏览器：click/keyboard/网络响应 都是事件
- s15：用户输入/队友消息/cron 都是事件

**精髓**：Agent 不再只响应"用户说话"，而是响应**任何外部刺激**。这是"从被动服务变成主动协作"的架构转折点——s16、s17 的自治 Agent 都建立在这个事件循环上。

### 4. 中介者模式（Mediator）

如果 Lead 直接持有每个队友的引用，直接调 `alice.send(...)`，通信会变成所有 Agent 互相知道对方的蜘蛛网。s15 用 MessageBus 做**中介者**：

```
# 没有中介者（蜘蛛网）：每个 Agent 维护 N-1 个连接
alice → bob, alice → lead, bob → carol ...

# 有中介者（星型）：每个 Agent 只认识 MessageBus
alice ─┐
bob  ──┤→ MessageBus → 邮箱文件 → 各 Agent 自己读
carol ─┘
```

发送者不需要知道接收者在哪、是线程还是进程——只需要往 Bus 里投递。**Agent 之间零直接依赖**，新增一个 Agent 不需要改任何已有 Agent 的代码。

### 5. 黑板模式（Blackboard / 共享空间）

稍微隐性一些：所有 Agent 通过**文件系统**这个共享空间协作。

```python
# 队友写的文件，Lead 能读到（共享工作目录）
# 队友写 .tasks/*.json，Lead 能读到（共享任务系统）
# 队友写 .mailboxes/*.jsonl，Lead 能读到（共享邮箱）
```

多个 Agent 不直接对话，而是**共同读写同一块"黑板"**（工作目录 + .tasks + .mailboxes）。谁读到什么由自己决定。这种模式特别适合任务型协作——队友做完事把结果写进共享空间，Lead 自己去取，不需要实时协调。

### 模式之间的关系

这些模式是**嵌套**的：

```
事件驱动（主循环）   ← 最外层，决定"什么时候干活"
    ↓ 触发
生产者-消费者（解耦）← 中间层，决定"谁产谁消"
    ↓ 通过
中介者/黑板（通信）  ← 最底层，决定"消息怎么传"
    ↓ 基于
Actor 模型（Agent）  ← 根本假设，每个 Agent 独立自治
```

注意：**这套组合就是分布式系统设计的标准套路**。把"线程"换成"进程"，把"文件邮箱"换成"网络消息队列"，把"队友"换成"微服务"——得到的就是一个微服务架构。

**一句话总结**：s15 用 **Actor 模型**定义 Agent 的形态，用 **生产者-消费者 + 中介者（MessageBus）** 解耦通信，用 **事件驱动** 统一触发机制——四者组合起来就是"多 Agent 协作"的基本架构骨架。后面 s16-s20 全是在这个骨架上加协议、加自治、加隔离。

---

## 试一下

```sh
cd learn-claude-code
python s15_agent_teams/demo_code.py
```

试试这些 prompt：

1. `Spawn alice as a backend developer. Ask her to create a file called schema.sql with a users table.`
2. `Check your inbox for alice's result.`
3. `Spawn bob as a tester. Ask him to check if schema.sql exists and list its contents.`

观察重点：Lead 如何启动队友？`.mailboxes/` 目录下的 JSONL 文件长什么样？队友完成后 Lead 的 inbox 有没有注入到 history？

> **⚠️ 运行后 `.mailboxes/` 或 `.tasks/` 可能是空的——这是正常的，不是 bug。** 两个原因：
>
> **1. 消息被"消费式删除"了。** `read_inbox` 是读 + 删（`inbox.unlink()`），消息一旦被 `check_inbox` 工具或 `inbox_poller` 注入读取，对应的 `.jsonl` 文件就消失了。所以 prompt 2 `Check your inbox` 执行后，`lead.jsonl` 会被读走并删除——你再去目录看自然为空。**目录空恰恰说明消息被正确处理过。**
>
> **2. `.tasks/` 空是预期的。** 这三个 prompt 没有调用任何 task 工具（create_task 等），所以不会生成任务文件。
>
> **想看队友活动的痕迹，别等消息被消费后再看目录，而是观察终端实时输出**：`[teammate] alice spawned` → `[bus] alice → lead: ...` → `[all teammates done]` 这些日志才是流程的真实轨迹。
>
> **另外注意目录位置**：`.tasks`/`.mailboxes` 创建在**你启动 python 的目录**（`WORKDIR = Path.cwd()`），不是 `s15_agent_teams/` 目录本身。用 `cd learn-claude-code && python s15_agent_teams/demo_code.py` 运行，它们就建在项目根目录。

---

## 接下来

队友能干活、能通信。但如果 Lead 想让 Alice 关机，直接杀线程会留下写到一半的文件。需要一个体面的关机协议：Lead 发 shutdown_request，队友收尾后退出。

s16 Team Protocols → 关机握手与消息约定。

<details>
<summary>深入 CC 源码</summary>

> 以下基于 CC 源码 `spawnMultiAgent.ts`、`useInboxPoller.ts`（969 行）、`useSwarmPermissionPoller.ts`（330 行）、`teammateMailbox.ts`、`teamHelpers.ts` 的完整分析。

### 一、没有中央消息总线，是文件系统

教学版用 `MessageBus` 类收发消息。CC 的做法更直接，每个 Agent 直接写其他 Agent 的收件箱文件。

收件箱路径：`~/.claude/teams/{teamName}/inboxes/{agentName}.json`

写入时用 `proper-lockfile` 文件锁保证并发安全（最多重试 10 次）。每个文件是一个 JSON 数组，append 新消息时读→追加→写回。

### 二、15 种消息类型

CC 的团队通信有 15 种结构化消息（`teammateMailbox.ts`）：

| 类型 | 方向 | 用途 |
|------|------|------|
| `plain text` | 双向 | 普通队友间通信 |
| `idle_notification` | 队友→Lead | 队友完成一轮工作，进入空闲 |
| `permission_request` | 队友→Lead | 队友需要操作审批 |
| `permission_response` | Lead→队友 | Lead 审批结果 |
| `plan_approval_request` | 队友→Lead | 队友提交计划待审 |
| `plan_approval_response` | Lead→队友 | Lead 审批计划 |
| `shutdown_request` | Lead→队友 | 请求体面关机 |
| `shutdown_approved` | 队友→Lead | 确认关机 |
| `shutdown_rejected` | 队友→Lead | 拒绝关机（附原因） |
| `task_assignment` | Lead→队友 | 分配任务 |
| `team_permission_update` | Lead→队友 | 广播权限变更 |
| `mode_set_request` | Lead→队友 | 修改队友的权限模式 |
| `sandbox_permission_*` | 双向 | 网络权限请求/回复 |
| `teammate_terminated` | 系统 | 队友被移除通知 |

文本消息被包装在 `<teammate-message>` XML 标签中交付给模型。

### 三、权限冒泡：双向轮询

教学版省略了权限冒泡。CC 的实际流程（`permissionSync.ts`）：

1. **队友**遇到需要审批的操作 → 发 `permission_request` 到 Lead 的收件箱
2. **Lead** 的 `useInboxPoller`（每 1 秒轮询）检测到请求 → 路由到 `ToolUseConfirmQueue`
3. Lead 的 UI 显示审批对话框，带队友名字和颜色
4. 用户审批后 → Lead 发 `permission_response` 回队友的收件箱
5. **队友**的 `useSwarmPermissionPoller`（每 500ms 轮询）收到回复 → 继续或拒绝执行

### 四、队友生命周期

CC 的队友由 `spawnTeammate()`（`spawnMultiAgent.ts`）创建：

1. **Spawn**：创建 tmux 窗格（或进程内），分配颜色，写入 team config
2. **Work**：`useInboxPoller` 每 1 秒检查收件箱 → 有消息就提交为新的 turn
3. **Idle**：Stop hook 触发 → 发 `idle_notification` 给 Lead
4. **Shutdown**：Lead 发 `shutdown_request` → 队友回复 `shutdown_approved` → Lead 清理

### 五、Team Config

团队注册表在 `~/.claude/teams/{teamName}/config.json`（`teamHelpers.ts`）：

```json
{
  "name": "my-team",
  "leadAgentId": "lead@my-team",
  "members": [{
    "agentId": "researcher@my-team",
    "name": "researcher",
    "agentType": "general-purpose",
    "color": "blue",
    "isActive": true
  }]
}
```

队友之间不能嵌套（`AgentTool.tsx:273` 明确禁止 "teammates spawning other teammates"）。

</details>

<!-- translation-sync: zh@v1, en@v1, ja@v1 -->
