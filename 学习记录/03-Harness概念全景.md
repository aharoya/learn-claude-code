# Harness 概念全景 —— 从 s01 到 s20 的所有概念一篇理清

## 引言：为什么要有这么多概念？

这个项目教你造的是**一个让 LLM 能干活的完整系统**，而不是一段调 API 的脚本。就像一辆车不只是发动机——你需要方向盘、刹车、油箱、仪表盘、安全带。每个概念解决一个特定问题，它们互相配合，不互相替代。

本文档以一个统一的**工厂流水线**类比贯穿所有概念。

---

## 核心基石：Function Calling

### 是什么

**Function Calling 是 LLM API 的内置能力**，不是这个项目发明的。当你把 `tools` 参数传给 API：

```python
response = client.messages.create(
    tools=TOOLS,       # ← 告诉模型"你有这些函数可以调"
    messages=[...],    # ← 告诉模型"这是当前问题和上下文"
)
```

LLM 的回复有两种可能：

| 返回值 | 含义 | 对应代码 |
|--------|------|---------|
| `stop_reason = "tool_use"` | 模型决定调用某个工具 | 执行工具 → 结果回写 → 继续循环 |
| `stop_reason = "end_turn"` | 模型觉得任务完成了 | 打印文本 → 退出 agent_loop |

### 它和 Tools 的关系

```
Function Calling = LLM API 的"打电话"能力
Tool = 电话那头接听的人（具体干活的函数）

没有 Function Calling → Tools 只是一堆 JSON Schema，LLM 不会调它们
没有 Tools → Function Calling 无函数可调，LLM 只能输出普通文本
```

### 它和所有其他概念的关系

**Function Calling 是整个 Agent 循环的驱动引擎。** 模型在循环中反复调 API：

```
LLM 收到用户请求
  → API 通过 Function Calling 让 LLM 决定调哪个工具
  → 你的代码执行该工具
  → 结果通过 tool_result 回传给 LLM
  → LLM 看结果决定下一步（再调工具 or 输出最终答案）
```

所有 s02-s20 的概念都是在这条链路的不同位置插入的"扩展点"。

---

## 工厂类比总图

```
┌──────────────────────────────────────────────────────────────────┐
│                        工厂大楼（Harness）                        │
│                                                                  │
│  ┌────────────────────────────────────────────────────────┐     │
│  │  厂长办公室（LLM）                                       │     │
│  │   - 收到订单（用户输入）                                  │     │
│  │   - 查看手册（System Prompt + Skill + Memory）            │     │
│  │   - 下达指令（Function Calling → Tool / Subagent）       │     │
│  │   - 检查进度（TodoWrite）                                │     │
│  └────────────────────────┬───────────────────────────────┘     │
│                           │                                       │
│  ┌────────────────────────┴───────────────────────────────┐     │
│  │  车间走廊（Hook 监控）                                   │     │
│  │   进入车间前 → 安全检查（Permission Hook）                 │     │
│  │   进入车间时 → 记录日志（Log Hook）                        │     │
│  │   出车间时   → 检查产出（PostToolUse Hook）                │     │
│  └────────────────────────────────────────────────────────┘     │
│                           │                                       │
│  ┌───────────┬────────────┼────────────┬──────────────┐       │
│  ▼           ▼            ▼            ▼              ▼       │
│ ┌──────┐ ┌──────┐ ┌──────────┐ ┌─────────┐ ┌──────────┐    │
│ │ Tool │ │ MCP  │ │Subagent  │ │Background│ │ Cron     │    │
│ │ 内建  │ │ 外接  │ │ 分厂厂长 │ │ 异步任务 │ │ 定时任务  │    │
│ └──────┘ └──────┘ └──────────┘ └─────────┘ └──────────┘    │
│                                                              │
│  ┌──────────────────────────────────────────────────────┐   │
│  │  档案室（知识系统）                                     │   │
│  │  Skill: 操作手册（按需取阅）                            │   │
│  │  Memory: 过往工作记录（跨项目参考）                      │   │
│  │  System Prompt: 贴在墙上的厂规                          │   │
│  └──────────────────────────────────────────────────────┘   │
│                                                              │
│  ┌──────────────────────────────────────────────────────┐   │
│  │  调度中心（多 Agent 协作）                              │   │
│  │  Task: 订单看板（谁做了什么，谁依赖谁）                  │   │
│  │  MessageBus: 对讲机                                    │   │
│  │  Protocol: 交接班流程（申请/审批/关闭）                  │   │
│  │  Autonomous: 自动巡检机器人                            │   │
│  └──────────────────────────────────────────────────────┘   │
└──────────────────────────────────────────────────────────────────┘
```

---

## 概念分类详解

我把 20 章的所有概念按"解决的问题"分成六个大类。

---

### 第一类：行动能力（Action）—— 让模型能动手

#### 1. Tool（工具）— s01/s02

**本质**：模型可以直接调用的函数。

```python
# 定义（给 LLM 看的 JSON Schema）
{"name": "bash", "description": "Run a shell command.",
 "input_schema": {"properties": {"command": {"type": "string"}}}}

# 实现（实际干活）
def run_bash(command):
    return subprocess.run(command, shell=True, ...)
```

**解决的问题**：模型只能输出文本，不能操作文件系统、不能运行命令。

**复杂度演进**：
- s01：1 个工具（bash），硬编码调用
- s02：5 个工具（bash/read/write/edit/glob），查表分发
- s20：26 个工具

**关键设计：TOOL_HANDLERS 策略模式**

```python
# 不再 if block.name == "bash": ... elif block.name == "read_file": ...
# 而是：
handler = TOOL_HANDLERS.get(block.name)
output = handler(**block.input)
```

新增工具只需两步：写函数 + 注册到 `TOOL_HANDLERS`，循环代码零改动。

---

#### 2. Subagent（子 Agent）— s06

**本质**：一个拥有**全新对话上下文**的独立 Agent 循环。

```python
def spawn_subagent(description):
    messages = [{"role": "user", "content": description}]  # 全新上下文
    for _ in range(30):
        response = client.messages.create(
            model=MODEL, messages=messages, tools=SUB_TOOLS, ...)
        # ... 工具执行循环 ...
    return extract_text(messages[-1])  # 只返回摘要
```

**解决的问题**：复杂任务在父 Agent 的脏上下文中难以专注思考。子 Agent 获得干净上下文，干完活只返回摘要，不污染父上下文。

**和 Tool 的关系**：`task` 本身也是一个 Tool（LLM 通过 Function Calling 调用它），但它的执行内容是一个全新的完整循环。

| | Tool | Subagent |
|---|---|---|
| **上下文** | 使用父 Agent 的 messages | 全新的 messages |
| **返回** | 完整工具输出 | 仅摘要 |
| **可递归** | 无限制 | 禁止递归（SUB_TOOLS 不含 task） |
| **适用场景** | 单步操作 | 复杂的多步骤子任务 |

---

#### 3. Background Task（后台任务）— s13

**本质**：在独立线程中异步执行的 Tool。

```python
def start_background_task(block):
    threading.Thread(target=worker, daemon=True).start()
    return f"[Background task {bg_id} started]"
```

**解决的问题**：`npm install`、`pytest` 等慢操作会阻塞 Agent 循环。后台任务让慢操作在后台跑，Agent 可以继续做别的事。

**和 Tool 的关系**：后台任务不是一种新工具，而是一种**执行策略**——同一个 bash 工具，默认同步执行，发现是慢操作则转为后台执行。

---

#### 4. Cron Scheduler（定时器）— s14

**本质**：在独立线程中按时间表自动注入消息到 Agent 循环的调度器。

```python
def cron_scheduler_loop():
    while True:
        time.sleep(1)
        now = datetime.now()
        for job in scheduled_jobs:
            if cron_matches(job.cron, now):
                # 将 job.prompt 注入到 agent_loop
                cron_queue.append(job)
```

**解决的问题**：Agent 应该能按时间表主动工作，而不只是被动响应用户。比如每天 9 点检查 CI 状态。

**和 Tool 的关系**：Cron 定时器本身**不是**一个 Tool——它是一个独立运行的守护线程。但它的**管理接口**是三个 Tool（`schedule_cron`、`list_crons`、`cancel_cron`），通过 Function Calling 让 LLM 也能管理定时任务。

---

### 第二类：知识能力（Knowledge）—— 让模型知道更多

#### 5. Skill（技能）— s07

**本质**：按需加载的领域知识文档。

```python
# 第一级：在 SYSTEM 里注入技能目录
"Skills catalog:\n- code-review: Review code changes\n- api-docs: API doc style guide"

# 第二级：模型调用 load_skill("code-review") → 完整 SKILL.md 文档注入
def load_skill(name):
    return SKILL_REGISTRY[name]["content"]
```

**解决的问题**：所有知识塞进 SYSTEM 提示词会超 token 限制。Skill 让模型按需取阅——"目录"常驻 SYSTEM，"全文"用时才加载。

**和 Tool 的区别**：

| | Tool | Skill |
|---|---|---|
| **模型调用后** | 执行操作（改文件、跑命令） | 获得一段知识文本 |
| **副作用** | 有（修改文件系统） | 无（只是文本） |
| **本质** | 函数 | 文档 |
| **扩展方式** | 写 Python 代码 | 写 Markdown 放到 skills/ 目录 |

`load_skill` 本身也是一个 Tool，所以 Skill 加载也走 Function Calling，但它的"返回值是知识"而不是"执行结果"。

---

#### 6. Memory（记忆）— s09

**本质**：在文件系统中持久化的、跨会话可检索的过往经验。

```python
# 写入
with open(MEMORY_DIR / f"{slug}.md", "w") as f:
    f.write("name: api-pattern\n---\n用户偏好使用FastAPI...")

# 读取（跨会话）
def select_relevant_memories(query):
    # LLM 判断哪些记忆相关 + 关键词 fallback
    return relevant_memories
```

**解决的问题**：每次新对话模型都是"失忆"的。记忆让模型在**不同会话之间**记住用户的偏好、项目约定、已解决的问题。

**和 Skill 的区别**：

| | Skill | Memory |
|---|---|---|
| **来源** | 预定义的领域知识（开发者编写） | 运行中积累的经验（自动生成） |
| **生命周期** | 版本控制，随项目更新 | 随使用积累，可能被压缩/合并 |
| **作用域** | 对所有人有效 | 对这个项目/用户有效 |
| **加载方式** | 模型按需调用 `load_skill` | 系统在每轮对话自动注入相关记忆 |

---

#### 7. System Prompt（系统提示词）— s10

**本质**：运行时动态组装的模型行为说明书。

```python
PROMPT_SECTIONS = {
    "identity": "You are a coding agent. Act, don't explain.",
    "tools": "Available tools: bash, read_file, write_file...",
    "workspace": f"Working directory: {WORKDIR}",
    "memory": "Relevant memories injected below.",
}

def assemble_system_prompt(context):
    sections = [PROMPT_SECTIONS["identity"]]
    if context.get("memories"):
        sections.append(f"Relevant memories:\n{context['memories']}")
    return "\n\n".join(sections)
```

**解决的问题**：硬编码 SYSTEM 字符串无法随运行时状态变化。System Prompt 组装器根据当前上下文（有哪些工具连上了、哪些记忆被命中）动态拼接。

**和所有概念的关系**：System Prompt 是所有知识类概念的**汇聚点**：

```
assemble_system_prompt(context):
  identity              ← 模型身份
  tools catalog         ← Tool 清单
  workspace             ← 工作目录
  
  if 有记忆:             ← Memory
    inject memories
  
  if 有 Skill 目录:      ← Skill
    inject skill_catalog
  
  if 有 MCP 连接:        ← MCP Plugin
    inject mcp_tools
```

---

### 第三类：控制能力（Control）—— 让系统安全可靠

#### 8. Permission Gate（权限门）— s03

**本质**：在 Tool 执行前的三道安检。

```python
def check_permission(block):
    # Gate 1：黑名单（自动拒绝）
    if any(p in command for p in DENY_LIST):
        return False
    # Gate 2：规则匹配 → Gate 3 用户确认
    if check_rules(block.name, block.input):
        return ask_user() == "allow"
    return True
```

**解决的问题**：模型可能会执行危险命令（rm -rf /、sudo rm），需要多层防护。

---

#### 9. Hook（钩子）— s04

**本质**：在 Agent 循环的关键节点自动触发的回调函数。

```python
HOOKS = {"UserPromptSubmit": [], "PreToolUse": [],
         "PostToolUse": [], "Stop": []}

def trigger_hooks(event, *args):
    for callback in HOOKS[event]:
        result = callback(*args)
        if result is not None:
            return result  # 非 None 返回值 = "拦截"
    return None
```

**解决的问题**：如果在循环体里硬编码安全检查、日志记录、输出检查，每新增一种需求就要改一次 agent_loop。Hook 把这些旁路逻辑"挂"出去：

```python
# 注册（一次）
register_hook("PreToolUse", permission_hook)
register_hook("PreToolUse", log_hook)

# agent_loop 里（不再改动）
blocked = trigger_hooks("PreToolUse", block)
```

**和 Tool 的区别**：Tool 是**模型主动调用的**（通过 Function Calling），Hook 是**系统自动触发的**（模型不知道它的存在）。同一个 tool_use 过来，先过 Hook（安全检查），再过 Tool Handler（执行）。

四种事件：

| Hook 事件 | 触发时机 | 典型用途 |
|-----------|---------|---------|
| `UserPromptSubmit` | 用户输入后 | 注入上下文、记录日志 |
| `PreToolUse` | 工具执行前 | 权限检查、日志记录 |
| `PostToolUse` | 工具执行后 | 大输出警告、后处理 |
| `Stop` | agent_loop 退出前 | 统计汇总 |

---

#### 10. TodoWrite（待办清单）— s05

**本质**：一个特殊的 Tool，让 LLM 用来声明和更新计划。

```python
# LLM 调用 todo_write(todos=...)
def run_todo_write(todos):
    CURRENT_TODOS = todos
    # 打印任务列表
```

**解决的问题**：模型收到复杂请求后直接开干，缺乏"先计划再执行"的步骤。TodoWrite 给模型一个工具来声明意图、分解步骤。加上 nag 计数器（连续 3 轮未更新 todo 自动提醒），确保模型不会忘记跟进计划。

---

#### 11. Error Recovery（错误恢复）— s11

**本质**：LLM 调用失败时的分级重试和降级策略。

```python
def with_retry(fn, state):
    for attempt in range(10):
        try:
            return fn()
        except 429:     # 限流 → 等几秒重试
            time.sleep(retry_delay(attempt))
        except 529:     # 过载 → 等 + 可能切换降级模型
            if state.consecutive_529 >= 3:
                state.current_model = FALLBACK_MODEL
            time.sleep(retry_delay(attempt))

# agent_loop 外层处理非瞬时错误
try:
    response = with_retry(lambda: LLM(messages, ...), state)
except max_tokens:     # 输出截断 → 扩大 token 上限
    escalate_max_tokens()
    continue
except prompt_too_long:  # 输入太大 → 压缩上下文
    reactive_compact(messages)
    continue
```

**解决的问题**：Agent 是无人值守的长期进程。API 限流、网络抖动、输出截断是常态，不是异常。

---

#### 12. Context Compaction（上下文压缩）— s08

**本质**：在上下文超限之前主动压缩消息历史的策略集。

```python
def prepare_context(messages):
    messages = tool_result_budget(messages)   # L3: 大输出持久化
    messages = snip_compact(messages)         # L1: 裁剪中间消息
    messages = micro_compact(messages)        # L2: 压缩老旧 tool_result
    if estimate_size(messages) > LIMIT:
        messages = compact_history(messages)  # L4: LLM 摘要压缩
    return messages
```

**解决的问题**：工具调用一多，messages 越来越大，很快会触及 token 上限。四层压缩管道从轻到重逐步收紧，尽量在不动核心内容的前提下减少 token 消耗。

---

### 第四类：协作能力（Coordination）—— 让多 Agent 一起工作

#### 13. Task System（任务系统）— s12

**本质**：文件持久化的 DAG（有向无环图）任务看板。

```python
@dataclass
class Task:
    id: str
    subject: str
    status: str              # pending / in_progress / completed
    owner: str | None
    blockedBy: list[str]     # 依赖列表
```

**解决的问题**：多 Agent 共享一个任务列表，任务之间有依赖关系（B 依赖 A 完成才能开始）。这是一个"去中心化"的协调方式——每个 Agent 自己查看任务看板，认领可开始的任务。

---

#### 14. MessageBus（消息总线）— s15

**本质**：基于 JSONL 文件的多 Agent 通信系统。

```python
class MessageBus:
    def send(self, from_agent, to_agent, content, msg_type="message"):
        msg = {"from": from_agent, "to": to_agent,
               "content": content, "type": msg_type, "ts": time.time()}
        # 追加到收件人邮箱文件
        with open(f".mailboxes/{to_agent}.jsonl", "a") as f:
            f.write(json.dumps(msg) + "\n")

    def read_inbox(self, agent):
        # 读取并清空收件箱
        msgs = 读取文件内容
        文件清空
        return msgs
```

**解决的问题**：多 Agent 之间需要通信。Mailbox 文件是解耦的——发送者只管写，接收者空闲时再读。

---

#### 15. Team Protocol（团队协议）— s16

**本质**：Agent 之间正式交互的握手流程。

```python
class ProtocolState:
    request_id: str
    type: str          # shutdown / plan_approval
    sender: str
    status: str        # pending / approved / rejected

def match_response(response_type, request_id, approve):
    # 通过 request_id 匹配请求和响应
    # 防止一个响应误确认了另一个请求
    state = pending_requests.get(request_id)
    if state.type == "shutdown" and response_type != "shutdown_response":
        return  # 类型不匹配，忽略
    state.status = "approved" if approve else "rejected"
```

**解决的问题**：Agent 之间的"闲聊"和"正式交互"应该分开。协议定义了握手机制（请求→响应→确认），确保 Agent 之间的关键操作（关闭、审批）可靠。

---

#### 16. Autonomous Agent（自治 Agent）— s17

**本质**：具备 WORK/IDLE 双阶段生命周期的自主 Agent。

```python
while True:
    # WORK 阶段：执行任务（最多 10 轮）
    for _ in range(10):
        LLM → 工具 → 循环
    # IDLE 阶段：空闲轮询
    for _ in range(12):
        if 有消息 → 回到 WORK
        if 有无主任务 → 自动认领 → 回到 WORK
        if 收到关闭指令 → 退出
        time.sleep(5)
```

**解决的问题**：Agent 不能一直在跑（浪费 API），也不能一直睡（响应迟钝）。双阶段让 Agent 在繁忙和空闲之间自动切换。

---

#### 17. Worktree Isolation（工作树隔离）— s18

**本质**：每个任务一个独立的 git worktree，互不干扰。

```python
def create_worktree(name):
    subprocess.run(["git", "worktree", "add", path, "-b", f"wt/{name}"])
    # 每个 worktree 有独立的文件系统副本

def remove_worktree(name):
    # 删除前检查是否有未提交的改动
    files, commits = _count_worktree_changes(path)
    if files > 0 or commits > 0:
        return "有改动，需要确认才能删除"
```

**解决的问题**：多个 Agent 并行工作时，修改同一个文件会冲突。每个任务一个独立的工作目录（git worktree），Agent 在自己的目录里随便改，不影响主分支和其他任务。

---

### 第五类：扩展能力（Extension）—— 让系统能接入外部世界

#### 18. MCP Plugin（MCP 集成）— s19

**本质**：通过标准协议从外部服务器动态发现并注册工具的机制。

```python
def connect_mcp(name):
    client = MCPClient(name)
    # 连接外部服务器，发现它的工具列表
    # 工具自动注册到 Agent 的工具池

def assemble_tool_pool():
    tools = list(BUILTIN_TOOLS)
    for server_name, client in mcp_clients.items():
        for tool in client.tools:
            prefixed = f"mcp__{server_name}__{tool.name}"
            tools.append(prefixed)
    return tools
```

**解决的问题**：内建工具集无法覆盖所有场景。MCP 让 Agent 可以动态接入第三方服务（文档搜索、部署平台、数据库），每个服务通过 MCP 协议暴露自己的工具集。

**和 Tool 的区别**：

| | Builtin Tool | MCP Plugin |
|---|---|---|
| **定义位置** | `TOOLS = [...]` 硬编码 | 外部服务器动态发现 |
| **生命周期** | 进程启动就存在 | `connect_mcp()` 后才有 |
| **谁开发** | 本仓库开发者 | 第三方服务提供者 |
| **命名** | `bash`, `read_file` | `mcp__docs__search`, `mcp__deploy__status` |

---

## 概念关系总表

| 类别 | 概念 | 章节 | 核心问题 | 谁触发 | 模型知道吗 |
|------|------|------|---------|--------|-----------|
| **核心** | Function Calling | API 内置 | 模型如何选函数 | API | 是，核心机制 |
| **核心** | Agent Loop | s01 | 如何循环调 LLM | 代码 | 否 |
| **行动** | Tool | s02 | 模型如何操作外部世界 | 模型 (FC) | 是 |
| **行动** | Subagent | s06 | 复杂任务如何隔离处理 | 模型 (Tool) | 是 |
| **行动** | Background Task | s13 | 慢操作如何不阻塞循环 | 代码 (启发式) | 有时 |
| **行动** | Cron | s14 | Agent 如何定时主动工作 | 守护线程 | 否（由系统注入） |
| **知识** | Skill | s07 | 领域知识如何按需注入 | 模型 (Tool) | 是 |
| **知识** | Memory | s09 | 经验如何跨会话持久化 | 系统自动 | 否（注入到 SYSTEM） |
| **知识** | System Prompt | s10 | SYSTEM 如何动态组装 | 代码 | 否（被读取） |
| **控制** | Permission | s03 | 危险操作如何拦截 | Hook | 否 |
| **控制** | Hook | s04 | 旁路逻辑如何不侵入循环 | 代码自动 | 否 |
| **控制** | TodoWrite | s05 | 模型如何计划再行动 | 模型 (Tool) | 是 |
| **控制** | Error Recovery | s11 | API 失败如何自动恢复 | 代码 (try/except) | 否 |
| **控制** | Context Compaction | s08 | 上下文太大如何压缩 | 代码 (阈值触发) | 否 |
| **协作** | Task System | s12 | 多 Agent 如何共享任务 | 模型 (Tool) | 是 |
| **协作** | MessageBus | s15 | Agent 之间如何通信 | Agent (代码) | 取决于用法 |
| **协作** | Protocol | s16 | 正式握手如何可靠 | Agent + 代码 | 取决于用法 |
| **协作** | Autonomous | s17 | Agent 如何自动空闲切换 | 循环逻辑 | 否 |
| **隔离** | Worktree | s18 | 并行任务文件冲突如何解决 | Agent (Tool) | 是 |
| **扩展** | MCP Plugin | s19 | 第三方能力如何标准接入 | 代码 + 外部服务 | 最终是 Tool |

---

## 一条请求贯穿所有概念的完整路径

以下是 s20 综合系统中，一条用户请求的完整处理链路：

```
用户输入："帮我重构 main.py，并在新分支上提交"
  │
  ├── 1. UserPromptSubmit Hook → 记录日志 ──── [Hook]
  │
  ├── 2. agent_loop 开始
  │     ├── 检查 cron_queue → 有触发？→ 注入消息 ────── [Cron]
  │     ├── 检查 background_results → 完成？→ 注入通知 ── [Background]
  │     ├── rounds_since_todo >= 3？→ nag 提醒 ──────── [TodoWrite]
  │     └── prepare_context：
  │           ├── tool_result_budget → 大输出持久化 ── [Compaction-L3]
  │           ├── snip_compact → 裁剪中间消息 ──────── [Compaction-L1]
  │           ├── micro_compact → 压缩旧结果 ───────── [Compaction-L2]
  │           └── 超限？→ compact_history 摘要 ─────── [Compaction-L4]
  │
  ├── 3. assemble_system_prompt
  │     ├── identity ──────────────────────────────── [System Prompt]
  │     ├── tools catalog ─────────────────────────── [Tool]
  │     ├── worksapce ─────────────────────────────── [System Prompt]
  │     ├── memory injection ──────────────────────── [Memory]
  │     ├── skills catalog ────────────────────────── [Skill]
  │     └── MCP server list ──────────────────────── [MCP]
  │
  ├── 4. LLM 调用（包裹在错误恢复中）
  │     ├── with_retry(LLM, state) ────────────────── [Error Recovery]
  │     └── 429? → 指数退避 / 529? → 模型降级
  │
  ├── 5. LLM 返回 tool_use
  │     ├── Hook: PreToolUse → permission_hook ──── [Permission/Hook]
  │     ├── Hook: PreToolUse → log_hook ──────────── [Hook]
  │     │
  │     ├── 情况 A：模型调 bash ──────────────────── [Tool]
  │     │     ├── 慢操作？→ 后台线程执行 ────────── [Background]
  │     │     └── 通知结果
  │     │
  │     ├── 情况 B：模型调 task ──────────────────── [Subagent]
  │     │     └── 子 Agent 独立循环（全新上下文）
  │     │           ├── 同上 PreToolUse Hook
  │     │           └── 执行工具 → 返回摘要
  │     │
  │     ├── 情况 C：模型调 load_skill ────────────── [Skill]
  │     │     └── 返回 SKILL.md 全文
  │     │
  │     ├── 情况 D：模型调 connect_mcp ──────────── [MCP]
  │     │     └── 发现新工具 → 下一轮自动可用
  │     │
  │     ├── 情况 E：模型调 create_task ───────────── [Task]
  │     │     └── 写入 .tasks/task_xxx.json
  │     │
  │     └── 情况 F：mcp__docs__search ──────────── [MCP Plugin]
  │           └── 调用外部 MCP 服务器
  │
  ├── 6. Hook: PostToolUse → large_output_hook ────── [Hook]
  │
  ├── 7. 结果回写 → 回到步骤 2（继续循环）
  │
  └── 8. stop_reason != "tool_use" → Stop Hook ────── [Hook]
       → agent_loop 返回 → 打印结果
```

---

## 一张图串联所有概念

```
                    ┌─────────────────────────┐
                    │       模型 (LLM)         │
                    │                         │
                    │ 看到 SYSTEM 提示词       │
                    │  → 知道自己是谁          │
                    │  → 知道有哪些工具可用     │
                    │  → 知道相关记忆和技能     │
                    │                         │
                    │ 通过 Function Calling    │
                    │ 决定调哪个工具            │
                    └──────────┬──────────────┘
                               │
                    ┌──────────┴──────────────┐
                    │    agent_loop           │
                    │    while True:          │
                    │                         │
                    │  ① 注入定时/后台消息      │ ← Cron / Background
                    │  ② prepare_context()     │ ← Compaction
                    │  ③ assemble_system()     │ ← System Prompt
                    │  ④ with_retry(LLM())     │ ← Error Recovery
                    │  ⑤ Hook: PreToolUse      │ ← Permission / Hook
                    │  ⑥ TOOL_HANDLERS 分发     │ ← Tool / Subagent
                    │     │                    │    Skill / MCP
                    │     ├─ task ─→ Subagent  │    Task / Worktree
                    │     ├─ load_skill ─→ 知识 │
                    │     ├─ connect_mcp ─→ 插件│
                    │     ├─ create_worktree    │
                    │     └─ 普通工具            │
                    │  ⑦ Hook: PostToolUse     │ ← Hook
                    │  ⑧ 结果回写 → 继续        │
                    └──────────────────────────┘
                              │
           ┌──────────────────┼──────────────────┐
           │                  │                  │
           ▼                  ▼                  ▼
    ┌──────────────┐  ┌──────────────┐  ┌──────────────┐
    │ 内建工具集    │  │ MCP 工具集   │  │ 子 Agent     │
    │ bash         │  │ mcp__docs    │  │ 全新上下文   │
    │ read_file    │  │ mcp__deploy  │  │ 独立循环     │
    │ write_file   │  │ ...          │  │ 只返摘要     │
    │ task         │  │              │  │              │
    │ load_skill   │  │              │  │              │
    └──────────────┘  └──────────────┘  └──────────────┘
```

---

## 常见困惑解答

### 1. 为什么 Tool 和 Skill 都是 Tool？

是的，`load_skill` 本身是一个 Tool（模型通过 Function Calling 调用它）。但它的"返回值是知识"而不是"执行结果"。这个区分在**概念层面**很重要：

- **Tool（狭义）**：模型调用来改文件、跑命令、读数据——有副作用
- **Skill**：模型调用来获取知识——没有副作用

技术上它们走同一套 Function Calling 通道，设计意图不同。

### 2. Hook 和 Permission 什么关系？

**Permission 是 Hook 的一个实例**。在 s03，权限检查直接硬编码在 agent_loop 里。在 s04，它被重构为一个 PreToolUse Hook：

```python
# s03 方式：直接调用
if not check_permission(block):
    ...

# s04 方式：注册为 Hook
register_hook("PreToolUse", permission_hook)
# agent_loop 不再知道"权限"的存在，它只知道"触发 PreToolUse Hook"
```

Hook 是**机制**（怎么挂载扩展逻辑），Permission 是**策略**（挂载的具体逻辑是什么）。

### 3. Cron 是 Tool 吗？

**Cron 本身不是 Tool**——它是一个独立运行的守护线程。但它的**管理接口**是三个 Tool（`schedule_cron`、`list_crons`、`cancel_cron`）。你可以通过 Tool 来管理 Cron，Cron 触发后通过注入消息来"唤醒"Agent。

### 4. Subagent 和 Autonomous Agent 有什么区别？

| | Subagent (s06) | Autonomous Agent (s17) |
|---|---|---|
| **启动方式** | 父 Agent 调用 task 工具 | 系统启动时自动创建 |
| **生命周期** | 完成一个子任务就结束 | WORK/IDLE 双阶段，持久运行 |
| **上下文** | 全新 messages，只看任务描述 | 自己的历史 + 收件箱 |
| **通信** | 不通信，只返回摘要 | 通过 MessageBus 收发消息 |
| **典型场景** | "帮我重构这个函数" | "6 号 Agent，你负责监控 CI 状态" |

### 5. MCP Plugin 和普通 Tool 有什么区别？

MCP Plugin 本质上也是 Tool，但它的工具定义**不是在代码中硬编码的**，而是运行时从外部服务器动态发现的。它是"Tool 的扩展机制"——没有 MCP，新增工具需要改代码；有了 MCP，第三方可以写一个服务器，Agent 连接后自动获得它的工具集。
