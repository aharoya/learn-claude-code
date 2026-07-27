# Harness 概念全景 —— 从 s01 到 s20 的所有概念一篇理清

> 本文档以 [Claude Code 官方文档](https://code.claude.com/docs/en/features-overview) 的扩展能力分类体系为参考框架，结合本项目 s01-s20 的实现逐一说明。

---

## 第一部分：官方分类体系

### 官方 8 大特征

Claude Code 官方文档把扩展能力分成 8 个特征，按加载时机和用途排列：

| 特征 | 加载时机 | 做什么 |
|------|---------|--------|
| **CLAUDE.md** | 每次会话自动加载 | 持久化项目约定 |
| **Skills** | 按需加载（描述常驻，内容用时加载） | 知识和可触发的工作流 |
| **MCP** | 会话启动时连接 | 连接外部服务，提供工具 |
| **Hooks** | 事件触发时执行 | 生命周期自动化 |
| **Subagents** | 按需创建 | 隔离上下文执行子任务 |
| **Agent teams** | 按需创建 | 多会话协作 |
| **Code intelligence** | 按需 | 语言服务器导航 |
| **Plugins** | 安装后启用 | 打包分发上述所有组件 |

来源：[Extend Claude Code](https://code.claude.com/docs/en/features-overview)

### 一条分界线：LLM 知道 vs LLM 不知道

```
LLM 能感知的（参与决策）        LLM 感知不到的（Harness 内部保障）
─────────────────────────     ────────────────────────────────
Tool（工具）                    Agent Loop（循环）
Skill（通过 Tool 加载的知识）    Hook（含权限检查）
System Prompt（系统说明）        Context Compaction
Plugin/MCP 工具（外接工具）      Error Recovery
                                后台线程（Cron/Background）
```

LLM 能感知的才影响它的决策，感知不到的是 Harness 在背后做保障。

---

## 第二部分：核心基石

### Function Calling

**这是 LLM API 的内置能力**，不是 Harness 发明的。当你把 `tools` 参数传给 API：

```python
response = client.messages.create(
    tools=TOOLS,       # ← 告诉模型"你有这些函数可以调"
    messages=[...],    # ← 告诉模型"这是当前问题和上下文"
)
```

LLM 的回复有两种可能：

| 返回值 | 含义 |
|--------|------|
| `stop_reason = "tool_use"` | 模型决定调用某个工具 |
| `stop_reason = "end_turn"` | 模型觉得任务完成了 |

```
Function Calling = LLM API 的"打电话"能力
Tool = 电话那头接听的人（具体干活的函数）

没有 Function Calling → Tools 只是一堆 JSON Schema
没有 Tools → Function Calling 无函数可调
```

所有 s02-s20 的概念都是在这条链路的不同位置插入的"扩展点"。

---

## 第三部分：概念详解（按官方分类）

### 一、行动能力

#### 1. Tool（工具）— s01/s02

**官方定位：** 内建工具（Built-in tools），包括文件操作、搜索、执行、网络访问。

**本质：** 模型通过 Function Calling 主动调用的函数。**这是 LLM 和外部世界的唯一操作接口。**

```python
# 定义（给 LLM 看的 JSON Schema）
{"name": "bash", "description": "Run a shell command.",
 "input_schema": {"properties": {"command": {"type": "string"}}}}

# 实现（实际干活）
def run_bash(command):
    return subprocess.run(command, shell=True, ...)
```

**解决的问题：** 模型只能输出文本，不能操作文件系统、不能运行命令。

**复杂度演进：**
- s01：1 个工具（bash），硬编码调用
- s02：5 个工具（bash/read/write/edit/glob），查表分发
- s20：26 个工具

**关键设计——TOOL_HANDLERS 策略模式：**

```python
handler = TOOL_HANDLERS.get(block.name)
output = handler(**block.input)
```

新增工具只需两步：写函数 + 注册到 `TOOL_HANDLERS`，循环代码零改动。

---

#### 2. Subagent（子 Agent）— s06

**官方定位：** 隔离上下文执行子任务，只返回摘要。

**本质：** 一个拥有**全新对话上下文**的独立 Agent 循环。

```python
def spawn_subagent(description):
    messages = [{"role": "user", "content": description}]  # 全新上下文
    for _ in range(30):
        response = client.messages.create(
            model=MODEL, messages=messages, tools=SUB_TOOLS, ...)
        # ... 工具执行循环 ...
    return extract_text(messages[-1])  # 只返回摘要
```

**解决的问题：** 复杂任务在父 Agent 的脏上下文中难以专注思考。子 Agent 获得干净上下文，干完活只返回摘要，不污染父上下文。

**和 Tool 的关系：** `task` 本身也是一个 Tool（LLM 通过 Function Calling 调用它），但它的执行内容是一个全新的完整循环。

| | Tool | Subagent |
|---|---|---|
| **上下文** | 使用父 Agent 的 messages | 全新的 messages |
| **返回** | 完整工具输出 | 仅摘要 |
| **可递归** | 无限制 | 禁止递归（SUB_TOOLS 不含 task） |
| **官方对比（Agent teams）** | — | 适合只需结果的任务 |

**官方对比（Subagent vs Agent team）：**

| 方面 | Subagent | Agent Team |
|------|----------|------------|
| **上下文** | 独立窗口，结果回主对话 | 完全独立 |
| **通信** | 只和主 Agent 通信 | 队友之间直接通信 |
| **协调** | 主 Agent 管理所有工作 | 共享任务看板，自治协调 |
| **最适合** | 只需要结果的任务 | 需要讨论和协作的复杂工作 |

---

#### 3. Background Task（后台任务）— s13

**本质：** 在独立线程中异步执行的 Tool。

```python
def start_background_task(block):
    threading.Thread(target=worker, daemon=True).start()
    return f"[Background task {bg_id} started]"
```

**解决的问题：** `npm install`、`pytest` 等慢操作会阻塞 Agent 循环。后台任务让慢操作在后台跑，Agent 可以继续做别的事。

**和 Tool 的关系：** 后台任务不是一种新工具，而是一种**执行策略**——同一个 bash 工具，默认同步执行，发现是慢操作则转为后台执行。

---

#### 4. Cron Scheduler（定时器）— s14

**本质：** 在独立线程中按时间表自动注入消息到 Agent 循环的调度器。

```python
def cron_scheduler_loop():
    while True:
        time.sleep(1)
        now = datetime.now()
        for job in scheduled_jobs:
            if cron_matches(job.cron, now):
                cron_queue.append(job)
```

**解决的问题：** Agent 应该能按时间表主动工作，而不只是被动响应用户。

**和 Tool 的关系：** Cron 定时器本身**不是**一个 Tool——它是一个独立运行的守护线程。但它的**管理接口**是三个 Tool（`schedule_cron`、`list_crons`、`cancel_cron`），通过 Function Calling 让 LLM 也能管理定时任务。

> 官方没有直接的 Cron 概念。可通过 Hook + MCP 组合实现类似能力。

---

### 二、知识能力

#### 5. Skill（技能）— s07

**官方定义：** 可复用的知识和可触发的工作流。分为 Reference skill（参考型）和 Action skill（`/<name>` 触发的工作流型）。

**本质：** 按需加载的领域知识文档。

```python
# 第一级：在 SYSTEM 里注入技能目录
"Skills catalog:\n- code-review: Review code changes\n- api-docs: API doc style guide"

# 第二级：模型调用 load_skill("code-review") → 完整 SKILL.md 文档
def load_skill(name):
    return SKILL_REGISTRY[name]["content"]
```

**解决的问题：** 所有知识塞进 SYSTEM 提示词会超 token 限制。Skill 让模型按需取阅——"目录"常驻 SYSTEM，"全文"用时才加载。

**和 Tool 的区别：**

| | Tool | Skill |
|---|---|---|
| **模型调用后** | 执行操作（改文件、跑命令） | 获得一段知识文本 |
| **副作用** | 有（修改文件系统） | 无（只是文本） |
| **本质** | 函数 | 文档 |
| **扩展方式** | 写 Python 代码 | 写 Markdown 放到 skills/ 目录 |

`load_skill` 本身也是一个 Tool，所以 Skill 加载也走 Function Calling，但它的"返回值是知识"而不是"执行结果"。

**官方对比（MCP vs Skill）：**

| 方面 | MCP | Skill |
|------|-----|-------|
| **是什么** | 连接外部服务的协议 | 知识、工作流、参考材料 |
| **提供什么** | 工具和数据访问 | 知识、工作流、参考材料 |
| **例子** | Slack 集成、数据库查询 | 代码审查清单、API 风格指南 |

它们配合使用：MCP 提供连接，Skill 教 Claude 怎么用好那个连接。

**本项目差异：** Claude Code 将 CLAUDE.md 和 Skill 分开——CLAUDE.md 是自动加载的项目约定，Skill 是按需加载的知识。本教程没有单独实现 CLAUDE.md 机制。

---

#### 6. Memory（记忆）— s09

**本质：** 在文件系统中持久化的、跨会话可检索的过往经验。

**解决的问题：** 每次新对话模型都是"失忆"的。记忆让模型在**不同会话之间**记住用户的偏好、项目约定、已解决的问题。

**和 Skill 的区别：**

| | Skill | Memory |
|---|---|---|
| **来源** | 预定义的领域知识（开发者编写） | 运行中积累的经验（自动生成） |
| **生命周期** | 版本控制，随项目更新 | 随使用积累，可能被压缩/合并 |
| **作用域** | 对所有人有效 | 对这个项目/用户有效 |
| **加载方式** | 模型按需调用 `load_skill` | 系统在每轮对话自动注入相关记忆 |

> 官方实现方式：通过 CLAUDE.md + MCP memory server。

---

#### 7. System Prompt（系统提示词）— s10

**本质：** 运行时动态组装的模型行为说明书。

```python
def assemble_system_prompt(context):
    sections = [PROMPT_SECTIONS["identity"]]
    if context.get("memories"):
        sections.append(f"Relevant memories:\n{context['memories']}")
    return "\n\n".join(sections)
```

**解决的问题：** 硬编码 SYSTEM 字符串无法随运行时状态变化。组装器根据当前上下文动态拼接。

**和所有概念的关系：** System Prompt 是所有可见概念的**汇聚点**——Tool 清单、Skill 目录、Memory 内容都在这里。

> **官方对应：** CLAUDE.md 是持久化项目约定的主要方式，System Prompt 组装在 Harness 层自动完成。本项目 s10 展示了这个组装过程的教学简化版。

---

### 三、控制能力

#### 8. Hook（钩子）— s04

**官方定义：** 在生命周期事件上自动触发的处理程序。支持 5 种类型——`command`、`http`、`mcp_tool`、`prompt`、`agent`。共 27 种事件。

**本质：** 在 Agent 循环的关键节点自动触发的回调函数。

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

**解决的问题：** 如果在循环体里硬编码安全检查、日志记录、输出检查，每新增一种需求就要改一次 agent_loop。Hook 把这些旁路逻辑"挂"出去。

**和 Tool 的区别：**

| | Tool | Hook |
|---|---|---|
| **谁触发** | 模型主动（Function Calling） | 系统自动（事件触发） |
| **LLM 知道吗** | 是，模型主动选择 | 否，完全无感知 |
| **返回值去哪** | 必回传给 LLM（tool_result） | 可选——可拒绝、可记录、可注入额外上下文 |

**官方强调：** Guardrails（护栏逻辑）必须放在 Hook 里。CLAUDE.md 或 Skill 里的指令只是请求，Hook 才是强制执行。

**四种事件（本项目的简化版）：**

| Hook 事件 | 触发时机 | 典型用途 |
|-----------|---------|---------|
| `UserPromptSubmit` | 用户输入后 | 注入上下文、记录日志 |
| `PreToolUse` | 工具执行前 | 权限检查、日志记录 |
| `PostToolUse` | 工具执行后 | 大输出警告、后处理 |
| `Stop` | agent_loop 退出前 | 统计汇总 |

---

#### 9. Permission（权限）— s03

**本质：** 在 Tool 执行前的安全检查。

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

**和 Hook 的关系：** Permission 是 Hook 的一个实例。官方文档没有把 Permission 作为独立概念——它放在 PreToolUse Hook 里。

```
s03 方式：check_permission(block) 硬编码在 agent_loop 里
s04 方式：register_hook("PreToolUse", permission_hook) 挂载为 Hook

Hook 是"机制"（怎么挂载），Permission 是"策略"（挂载什么逻辑）
```

---

#### 10. TodoWrite（待办清单）— s05

**本质：** 一个特殊的 Tool，让 LLM 用来声明和更新计划。

```python
def run_todo_write(todos):
    CURRENT_TODOS = todos
```

**解决的问题：** 模型收到复杂请求后直接开干，缺乏"先计划再执行"的步骤。加上 nag 计数器（连续 3 轮未更新 todo 自动提醒），确保模型不会忘记跟进计划。

---

#### 11. Error Recovery（错误恢复）— s11

**本质：** LLM 调用失败时的分级重试和降级策略。

```python
def with_retry(fn, state):
    for attempt in range(10):
        try:
            return fn()
        except 429:     # 限流 → 等几秒重试
            time.sleep(retry_delay(attempt))
        except 529:     # 过载 → 等 + 可能切换降级模型
            ...
```

**解决的问题：** Agent 是无人值守的长期进程。API 限流、网络抖动、输出截断是常态，不是异常。429/529 用指数退避重试，max_tokens 升级上限，prompt_too_long 压缩上下文。

---

#### 12. Context Compaction（上下文压缩）— s08

**本质：** 在上下文超限之前主动压缩消息历史的策略集。官方称为 context window 管理。

```python
def prepare_context(messages):
    messages = tool_result_budget(messages)   # 大输出持久化
    messages = snip_compact(messages)         # 裁剪中间消息
    messages = micro_compact(messages)        # 压缩老旧 tool_result
    if estimate_size(messages) > LIMIT:
        messages = compact_history(messages)  # LLM 摘要压缩
    return messages
```

**解决的问题：** 工具调用一多，messages 越来越大，很快会触及 token 上限。四层压缩管道从轻到重逐步收紧。

---

### 四、协作能力

#### 13. Task System（任务系统）— s12

**本质：** 文件持久化的 DAG（有向无环图）任务看板。

```python
@dataclass
class Task:
    id: str
    subject: str
    status: str              # pending / in_progress / completed
    owner: str | None
    blockedBy: list[str]     # 依赖列表
```

**解决的问题：** 多 Agent 共享任务列表，任务之间有依赖关系（B 依赖 A 完成才能开始）。

> **官方对应：** Agent Teams 环境中内置 TaskCreate/TaskCompleted 等 Hook 事件。

---

#### 14. MessageBus（消息总线）— s15

**本质：** 基于 JSONL 文件的多 Agent 通信系统。

```python
class MessageBus:
    def send(self, from_agent, to_agent, content, msg_type="message"):
        # 追加到收件人邮箱文件
```

**解决的问题：** 多 Agent 之间需要通信。Mailbox 文件解耦——发送者只管写，接收者空闲时再读。

---

#### 15. Team Protocol（团队协议）— s16

**本质：** Agent 之间正式交互的握手流程。

```python
class ProtocolState:
    request_id: str
    type: str          # shutdown / plan_approval
    status: str        # pending / approved / rejected
```

**解决的问题：** 协议定义了握手机制（请求→响应→确认），确保 Agent 之间的关键操作（关闭、审批）可靠。

---

#### 16. Autonomous Agent（自治 Agent）— s17

**本质：** 具备 WORK/IDLE 双阶段生命周期的自主 Agent。

```python
while True:
    # WORK 阶段：执行任务（最多 10 轮）
    for _ in range(10):
        LLM → 工具 → 循环
    # IDLE 阶段：空闲轮询
    for _ in range(12):
        if 有消息 → 回到 WORK
        if 有无主任务 → 自动认领 → 回到 WORK
        time.sleep(5)
```

**解决的问题：** Agent 不能一直在跑（浪费 API），也不能一直睡（响应迟钝）。

> **官方对应：** Agent Teams。s15-s17 三章合起来相当于 Claude Code 的 Agent Teams（实验性功能）。

---

### 五、隔离能力

#### 17. Worktree Isolation（工作树隔离）— s18

**本质：** 每个任务一个独立的 git worktree，互不干扰。

```python
def create_worktree(name):
    subprocess.run(["git", "worktree", "add", path, "-b", f"wt/{name}"])
```

**解决的问题：** 多个 Agent 并行工作时，修改同一个文件会冲突。每个任务一个独立的工作目录。

> **官方对应：** Hook 事件中有 WorktreeCreate/WorktreeRemove。

---

### 六、扩展能力

#### 18. MCP（Model Context Protocol）— s19

**官方定义：** 连接外部服务的开放标准协议。MCP Server 提供工具和数据访问。

**本质：** 通过标准协议从外部服务器动态发现并注册工具的机制。

```python
def assemble_tool_pool():
    tools = list(BUILTIN_TOOLS)
    for server_name, client in mcp_clients.items():
        for tool in client.tools:
            prefixed = f"mcp__{server_name}__{tool.name}"
            tools.append(prefixed)
    return tools
```

**解决的问题：** 内建工具集无法覆盖所有场景。MCP 让 Agent 动态接入第三方服务。

**和 Tool 的区别：**

| | 内建 Tool | MCP 工具 |
|---|---|---|
| **定义位置** | `TOOLS = [...]` 硬编码 | 外部服务器动态发现 |
| **生命周期** | 进程启动就存在 | `connect_mcp()` 后才有 |
| **谁开发** | 本仓库开发者 | 第三方服务提供者 |
| **命名** | `bash`, `read_file` | `mcp__docs__search` |

**Plugin 和 MCP 的关系：**

```
Plugin（概念：可插拔扩展）
  ├── 实现方式 A：ChatGPT Plugins（OpenAI 自己的协议）
  ├── 实现方式 B：MCP（开放标准协议）← 本项目用的
  └── 实现方式 C：自定义协议
```

类比 USB：Plugin 是"能插上去用"这个想法，MCP 是 USB 协议（规定了怎么插、数据怎么传）。

> **官方定义：** Plugin 是打包分发层，把 Skills、Hooks、MCP Servers、Subagents 打包成一个可安装单元。Plugin skills 有命名空间隔离（`/<plugin-name>:<skill-name>`）。本项目没有实现 Plugin 层。

---

## 第四部分：概念关系总表

| 概念 | 章节 | 官方分类 | 谁触发 | LLM 知道吗 | 上下文成本 |
|------|------|---------|--------|-----------|-----------|
| **Function Calling** | API 内置 | — | API | 是 | — |
| **Agent Loop** | s01 | — | 代码 | 否 | — |
| **Tool** | s02 | Built-in | 模型 (FC) | 是 | — |
| **Permission** | s03 | Hook 实例 | Hook | 否 | — |
| **Hook** | s04 | Extension | 系统自动 | 否 | 零（除非有输出） |
| **TodoWrite** | s05 | Tool | 模型 (FC) | 是 | — |
| **Subagent** | s06 | Extension | 模型 (Tool) | 间接 | 独立上下文 |
| **Skill** | s07 | Extension | 模型 (Tool) | 是 | 描述常驻，内容用时加载 |
| **Compaction** | s08 | 内置管理 | 代码 | 否 | — |
| **Memory** | s09 | Extension | 系统自动 | 否（注入 SYSTEM） | — |
| **System Prompt** | s10 | — | 代码 | 否（被读取） | 每次请求 |
| **Error Recovery** | s11 | — | 代码 | 否 | — |
| **Task System** | s12 | Agent Teams | 模型 (Tool) | 是 | — |
| **Background Task** | s13 | 内置 | 代码 | 有时 | — |
| **Cron** | s14 | — | 守护线程 | 否 | — |
| **MessageBus** | s15 | Agent Teams | Agent | 取决于用法 | — |
| **Protocol** | s16 | Agent Teams | Agent | 取决于用法 | — |
| **Autonomous** | s17 | Agent Teams | 循环逻辑 | 否 | — |
| **Worktree** | s18 | Hook 事件 | Agent (Tool) | 是 | — |
| **MCP** | s19 | External | 模型 (FC) | 是 | 低（tool search 按需） |

---

## 第五部分：一条请求的完整路径

```
用户输入："帮我重构 main.py，并在新分支上提交"
  │
  ├── 1. UserPromptSubmit Hook → 记录日志 ───── [Hook]
  │
  ├── 2. agent_loop 开始
  │     ├── 检查 cron_queue → 注入定时消息 ───── [Cron]
  │     ├── 检查 background_results → 通知 ──── [Background]
  │     ├── rounds_since_todo >= 3？→ nag ───── [TodoWrite]
  │     └── prepare_context：
  │           ├── tool_result_budget ──────────── [Compaction-L3]
  │           ├── snip_compact ────────────────── [Compaction-L1]
  │           ├── micro_compact ───────────────── [Compaction-L2]
  │           └── 超限？→ compact_history ────── [Compaction-L4]
  │
  ├── 3. assemble_system_prompt
  │     ├── identity + tools catalog + workspace ─ [System Prompt]
  │     ├── memory injection ──────────────────── [Memory]
  │     ├── skills catalog ────────────────────── [Skill]
  │     └── MCP server list ──────────────────── [MCP]
  │
  ├── 4. LLM 调用（包裹在错误恢复中）
  │     ├── with_retry(LLM, state) ────────────── [Error Recovery]
  │     └── 429? → 指数退避 / 529? → 模型降级
  │
  ├── 5. LLM 返回 tool_use
  │     ├── Hook: PreToolUse → permission_hook ── [Hook/Permission]
  │     ├── Hook: PreToolUse → log_hook ───────── [Hook]
  │     │
  │     ├── 模型调 bash ──────────────────────── [Tool]
  │     │     └── 慢操作？→ 后台线程执行 ───── [Background]
  │     │
  │     ├── 模型调 task ──────────────────────── [Subagent]
  │     │     └── 子 Agent 独立循环 → 返回摘要
  │     │
  │     ├── 模型调 load_skill ────────────────── [Skill]
  │     │
  │     ├── 模型调 connect_mcp ───────────────── [MCP]
  │     │
  │     └── 模型调 mcp__docs__search ────────── [MCP Tool]
  │
  ├── 6. Hook: PostToolUse → large_output_hook ── [Hook]
  │
  └── 7. stop_reason != "tool_use" → Stop Hook ── [Hook]
       → agent_loop 返回 → 打印结果
```

---

## 第六部分：常见困惑解答

### 1. 为什么 Tool 和 Skill 都是 Tool？

`load_skill` 本身是一个 Tool（模型通过 Function Calling 调用它）。但用途不同：

- **Tool（狭义）**：模型调用来改文件、跑命令——有副作用
- **Skill**：模型调用来获取知识——无副作用

技术上同一套 Function Calling 通道，设计意图不同。

### 2. Hook 和 Permission 什么关系？

**Permission 是 Hook 的一个实例。** Hook 是**机制**（怎么挂载），Permission 是**策略**（挂载什么逻辑）。官方没有把 Permission 当作独立概念。

### 3. Plugin 和 MCP 什么关系？

**Plugin 是概念**（可插拔扩展），**MCP 是协议**（怎么实现可插拔）。Plugin 可以打包 Skills、Hooks、MCP Servers 等。本项目用 MCP 实现插件能力。

### 4. Cron 是 Tool 吗？

**Cron 本身不是 Tool**——它是一个独立运行的守护线程。但它的**管理接口**是三个 Tool。

### 5. Subagent 和 Autonomous Agent 有什么区别？

| | Subagent (s06) | Autonomous Agent (s17) |
|---|---|---|
| **启动方式** | 父 Agent 调用 task 工具 | 系统启动时自动创建 |
| **生命周期** | 完成子任务就结束 | WORK/IDLE 双阶段，持久运行 |
| **通信** | 不通信，只返回摘要 | 通过 MessageBus 收发消息 |

### 6. 本项目 vs Claude Code 概念映射

| Claude Code 官方 | 本项目 | 差异 |
|-----------------|-------|------|
| Built-in Tools | s01/s02 | 一致 |
| CLAUDE.md | s07（部分） | 官方分开，本项目合并到 Skill |
| Skills | s07 | 只有 Reference 型，无 Action skill |
| Hooks | s04 | 一致，官方 27 种事件，本项目 4 种 |
| MCP | s19 | 一致 |
| Subagents | s06 | 一致 |
| Agent Teams | s15/s16/s17 | 对应 |
| Plugins | 无 | 打包分发层，本项目没实现 |
| Code Intelligence | 无 | LSP 集成，本项目没覆盖 |

---

## 参考来源

- [Extend Claude Code — Official Docs](https://code.claude.com/docs/en/features-overview)
- [Hooks Reference](https://code.claude.com/docs/en/hooks)
- [Plugins Reference](https://code.claude.com/docs/en/plugins-reference)
- [Glossary — Agentic Harness](https://code.claude.com/docs/en/glossary)
