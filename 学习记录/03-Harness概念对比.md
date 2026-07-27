# 大模型视角下的 Harness 概念对比

## 一个关键分界线：LLM 知道 vs LLM 不知道

```
LLM 看得到（参与交互）            LLM 看不到（Harness 内部机制）
──────────────────────────────  ──────────────────────────────
Tool（工具）                      Agent Loop（循环）
Skill（知识）                      Hook（钩子，含 Permission）
Plugin（插件，通过 MCP 协议）     Error Recovery（错误恢复）
System Prompt（系统提示词）        Context Compaction（上下文压缩）
                                  Background Task（后台任务）
                                  Cron Scheduler（定时器）
```

LLM 能感知的，才真正影响模型的**决策**。LLM 感知不到的，只是 Harness 在背后做的**保障工作**。

---

## LLM 能感知的

### 1. Tool — 模型的手

LLM 通过 Function Calling 主动调用的函数。**这是 LLM 和外部世界的唯一操作接口。**

```python
# LLM 看到的（API 参数中的 tools）
{"name": "bash", "description": "Run shell command", "input_schema": {...}}

# LLM 的行为：看到工具描述 → 决定是否调用 → 传参 → 等待结果
```

**关键**：对 LLM 来说，Tool 就是一个选择题选项。API 给出一堆候选，LLM 选一个调。

### 2. Skill — 模型的知识库

LLM 可以主动加载的领域知识。**这是 LLM 可以"阅读"但不"执行"的东西。**

```
LLM 收到任务 "给这个 API 写文档"
  → 看到系统提示里有技能目录 "code-review, api-docs"
  → 觉得 api-docs 有用
  → 调用 load_skill("api-docs")
  → 获得一份文档风格指南（tool_result）
  → 阅读后据此写文档
```

`load_skill` 本身也是一个 Tool，但它的用途不是执行操作，而是获取知识。**从 LLM 视角看：**

| | Tool | Skill |
|---|---|---|
| **调用后得到** | 执行结果（文件内容、命令输出） | 一段知识文本 |
| **副作用** | 有（改文件、跑命令） | 无 |
| **本质** | 函数 | 文档 |

### 3. Plugin（插件）— 模型的外接工具

**Plugin 对 LLM 来说就是 Tool，没有区别。** 区别只在 Harness 层面——Plugin 不是硬编码在代码里的，而是允许第三方扩展。

这个项目用的是 **MCP（Model Context Protocol）** 协议来实现插件机制。MCP 是一套标准协议，定义了 Agent 如何发现和调用外部服务器上的工具：

```python
# 普通 Tool：硬编码在代码里
TOOLS = [{"name": "bash", ...}, {"name": "read_file", ...}]

# MCP Plugin：运行时通过 MCP 协议从外部服务器动态发现
connect_mcp("docs")     # → MCP 服务器返回 2 个工具定义
connect_mcp("deploy")   # → MCP 服务器返回 2 个工具定义
# Harness 把它们合并到 TOOLS 列表：
TOOLS += [{"name": "mcp__docs__search", ...},
          {"name": "mcp__deploy__status", ...}]
```

从 LLM 的视角，`mcp__docs__search` 和 `bash` 没有任何区别——都是一个名字、一些参数。**Plugin 是"Tool 的可插拔扩展机制"，MCP 是实现它的其中一种协议。**

| | Tool | Plugin（通过 MCP） |
|---|---|---|
| **定义位置** | 代码硬编码 | 外部服务器，运行时发现 |
| **谁开发** | 本项目的开发者 | 第三方服务提供者 |
| **接入方式** | 写代码 + 注册 TOOLS | 运行 MCP 服务器 + connect_mcp |

### 4. System Prompt — 模型的出厂设置

模型每轮对话都能看到的"背景说明"，告诉它：你是谁、你在哪、你能做什么。

```python
# Harness 动态组装
"You are a coding agent at /home/project.
Available tools: bash, read_file, write_file...
Current time: 2026-07-27T10:30:00
Relevant memories: user prefers FastAPI..."
```

System Prompt 是**所有 LLM 可见概念的汇聚点**——Tool 清单、Skill 目录、Memory 内容都在这里。

---

## LLM 感知不到的

### 5. Hook — 模型背后的"安检员"

**这是最容易和 Tool 搞混的概念**，区别很关键：

```
                Hook 拦截点
                    │
LLM 决定调 bash("rm -rf /")
    │               │
    │    ┌──────────┴──────────┐
    │    │ PreToolUse Hook     │ → 权限检查 → "Permission denied"
    │    │（LLM 不知道这步存在） │
    │    └─────────────────────┘
    ▼
执行 run_bash("rm -rf /")  ← 被 Hook 拦截，根本没执行
    │
    ▼
LLM 收到 tool_result = "Permission denied."
    │  （它以为 bash 返回了这个，不知道是 Hook 替它挡了）
```

| | Tool | Hook |
|---|---|---|
| **谁触发** | 模型主动调（Function Calling） | 系统自动触发 |
| **LLM 知道吗** | 是，模型主动选择 | 否，完全无感知 |
| **返回值去向** | 返回给 LLM 做 tool_result | 可返回给 LLM（拒绝原因），也可仅记录日志 |
| **本质** | 模型问"我要做这个"→ 系统帮它做 | 系统说"我先检查一下"→ 再做 |

**Permission 就是 Hook 的一个实例**——安全检查逻辑作为 PreToolUse Hook 注册，和日志记录、输出检查平级。不需要把它当作单独概念。

### 6. Agent Loop — 模型的"操作间"

```python
while True:
    response = LLM(messages, tools)  # 模型在这"思考"
    if stop_reason != "tool_use":    # 模型说"我想好了"
        return
    # 帮模型执行它想做的事
    for block in response.content:
        if block.type == "tool_use":
            result = 执行工具(block)
            messages.append({"type": "tool_result", ...})
```

LLM 每次调用 API 只做一件事：看消息 → 决定。**它不知道循环的存在**，它以为每次 API 调用都是独立的。是 Harness 把它的"决定"收集起来、执行、回传，再问它下一步。

### 7. Error Recovery — 模型的"替身"

当 API 调用失败时（429 限流、529 过载、超长截断），LLM 不知道：

```python
try:
    response = LLM(messages)     # 模型尝试回答
except 429:                       # 模型不知道这发生了
    time.sleep(5)                 # 等 5 秒
    response = LLM(messages)     # 重新问同一个问题
except max_tokens:
    max_tokens = 64000           # 扩大输出限制
    response = LLM(messages)     # 重新问
```

模型只是正常收到一个请求、正常返回一个响应。它不知道之前失败了 3 次、等了 15 秒、还切换了模型。

---

## 四个核心概念的一句话总结

```
Tool = 模型主动调用的"手"
Hook = 系统自动触发的"安检"
Skill = 模型按需阅读的"知识手册"
Plugin = 可插拔的外接能力（MCP 是实现协议）
```

Tool 和 Plugin：**对模型来说是一回事**，区别在于 Harness 侧是硬编码还是通过 MCP 协议动态发现。

Tool 和 Skill：**都走 Function Calling**，区别在于 Tool 要执行操作、Skill 只是阅读。

Tool 和 Hook：**方向相反**——模型主动调 Tool，Hook 被动拦截 Tool。

Skill 和 System Prompt：**Skill 是"按需取阅"**，System Prompt 是"提前给到"。

---

## 从 LLM 视角看一条完整请求

```
用户："帮我重构 main.py"

① System Prompt 注入（模型看到：你是谁、有什么工具、有哪些记忆）
② LLM 收到消息 + SYSTEM
③ LLM 返回 tool_use: todo_write(...)
    → 后台执行（LLM 不知道细节）
    → tool_result 回传
④ LLM 看到结果，返回 tool_use: read_file("main.py")
    → Hook 检查路径是否安全（LLM 不知道）
    → 执行 read_file
    → Hook 检查输出是否太大（LLM 不知道）
    → tool_result 回传
⑤ LLM 看到代码，返回 tool_use: task("重写 main.py 并测试")
    → spawn_subagent()
      → 子 Agent 独立循环（LLM 不知道有子 Agent 存在）
      → 返回摘要
    → tool_result 回传
⑥ LLM 看到摘要，返回 tool_use: todo_write(更新进度)
    → tool_result 回传
⑦ LLM 认为完成 → end_turn

LLM 看到的是：7 次 API 调用，7 组 tool_use ↔ tool_result
LLM 看不到的是：Hook 拦截检查、子 Agent 内部 15 轮工具循环、Error Recovery 里的重试
```
