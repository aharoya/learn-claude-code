# LLM Function Calling 详解：Agent 的工具执行基础

> 本文从本项目（s01-s20）的实际代码出发，回答两个核心问题：① teammate 是怎么通过 function calling 启动和运行的？② 开发 Agent 到底要不要选支持 function calling 的模型？最后补充 Agent 的类型划分和各类型的实现方式，帮你建立"工具调用"在 Agent 架构中的全景认知。

---

## 1. 什么是 Function Calling（Tool Use）

**Function calling**（Anthropic 叫 Tool Use）是 LLM 的一种能力：API 调用时传入一组**工具定义**（`tools` 参数），模型在生成回复时**可以明确声明"我要调用某个工具、传入这些参数"**，而不是只输出纯文本。

工具定义是结构化 JSON Schema：

```python
{"name": "read_file", "description": "Read file contents.",
 "input_schema": {"type": "object",
                  "properties": {"path": {"type": "string"}},
                  "required": ["path"]}}
```

模型返回的响应里，如果 `stop_reason == "tool_use"`，`content` 里就包含 `tool_use` 块：

```python
response = client.messages.create(..., tools=TOOLS, ...)
if response.stop_reason != "tool_use":
    return          # 模型决定结束本轮
for block in response.content:
    if block.type == "tool_use":
        handler(**block.input)   # 解析出工具名 + 参数，分发执行
```

**整个项目的核心循环就建立在这个能力上**（s01-s20 全章节的外挂机制，都是对这个循环的叠加）：

```python
def agent_loop(messages):
    while True:
        response = client.messages.create(model=MODEL, system=SYSTEM, messages=messages, tools=TOOLS)
        messages.append({"role": "assistant", "content": response.content})
        if response.stop_reason != "tool_use":
            return
        results = [{"type": "tool_result", "tool_use_id": block.id, "content": TOOL_HANDLERS[block.name](**block.input)}
                   for block in response.content if block.type == "tool_use"]
        messages.append({"role": "user", "content": results})
```

---

## 2. Q1：Teammate 是通过 function calling 启动的吗？

**是，但要分两层看**——"启动"和"运行"是两个不同的 function calling 循环。

### 第一层：队友线程的"启动"

Lead 的 LLM 通过 function calling 调用 `spawn_teammate` 工具，触发启动：

```python
# ① Lead 的 agent_loop 收到模型返回的 tool_use block
for block in response.content:
    if block.type == "tool_use":
        handler = TOOL_HANDLERS.get(block.name)      # 找到 spawn_teammate
        output = handler(**block.input)              # 执行

# ② 工具注册表把 spawn_teammate 映射到函数
TOOL_HANDLERS = { ... "spawn_teammate": run_spawn_teammate, ... }

# ③ 最终落到 spawn_teammate_thread
def spawn_teammate_thread(name, role, prompt) -> str:
    ...
    threading.Thread(target=run, daemon=True).start()   # ← 启动队友线程
    return f"Teammate '{name}' spawned as {role} (autonomous)"
```

所以**启动**这一步：Lead LLM 决定 → function calling → `spawn_teammate` 工具 → 新线程跑起来。`spawn_teammate` 对 Lead 来说就是一个普通工具，只是它的副作用是"开一个后台线程"。

### 第二层：队友线程的"运行"

线程启动后，`run()` 内部**又是一个完整的 LLM agent 循环**，有自己的工具集 `sub_tools`（8 个），靠自己的 function calling 干活：

```python
def run():
    messages = [{"role": "user", "content": prompt}]
    sub_tools = [bash, read_file, write_file, send_message,
                 submit_plan, list_tasks, claim_task, complete_task]  # 8 个

    while True:
        response = client.messages.create(
            model=MODEL, system=system, messages=messages[-20:],
            tools=sub_tools, max_tokens=8000)          # 队友自己的 LLM 调用
        for block in response.content:
            if block.type == "tool_use":
                handler = sub_handlers.get(block.name)  # 队友自己的工具分发
                output = handler(**block.input)
```

队友认领任务、写文件、发消息，全部通过**它自己的** function calling 完成。

### 关键点：嵌套的 function calling

```
Lead LLM ──function calling──→ spawn_teammate ──启动──→ 队友线程
                                                            │
                                                        run() 内部
                                                            │
                                                    队友 LLM ──function calling──→ sub_tools(8个)
```

- **启动**用的工具集是 Lead 的 `TOOLS`（14 个，含 `spawn_teammate`）
- **运行**用的工具集是队友自己的 `sub_tools`（8 个，不含 `spawn_teammate`——队友不能再开队友）

**一个容易混淆的点**：`spawn_teammate` 返回的字符串是给 **Lead LLM 看**的（"spawned as..."），不是给队友的。队友真正收到的"你是谁、干什么"在 `run()` 里初始化的 `messages = [{"role": "user", "content": prompt}]`，prompt 是 Lead 调用工具时传入的第三个参数。

---

## 3. Q2：开发 Agent 的 LLM 必须支持 function calling 吗？

**分情况看**——对"工具型 Agent"（本项目这种）是必须的，但"Agent"不一定要用工具。

### 3.1 为什么本项目必须支持

整个 harness 的核心循环就是建立在 `stop_reason == "tool_use"` 这个判断上的：

```python
response = client.messages.create(..., tools=TOOLS, ...)  # 传入工具定义
if response.stop_reason != "tool_use":
    return          # 模型决定结束本轮
# 否则：模型明确表示"我要调工具" → 解析 block → 分发
for block in response.content:
    if block.type == "tool_use":
        handler(**block.input)
```

s01-s20 的全部机制（工具分发、权限门、队友协议、自治认领）都是这个循环上的外挂。**如果模型不支持 function calling，这个循环根本转不起来**——s01 的 demo 就无法运行。所以对本项目，选支持 function calling 的模型是硬性前提（Anthropic / DeepSeek / GLM 这些兼容接口都支持）。

### 3.2 但 Agent ≠ 必须用工具

"Agent"只是"能自主做多步决策的 AI 程序"，工具只是它的一种行动手段。比如：

- **纯对话 Agent**（客服、答疑）：只用文本生成，不需要工具
- **纯推理 Agent**（数学、分析）：只要思考链，不需要工具

这类 Agent 随便一个 LLM 都行，跟 function calling 无关。

### 3.3 不支持 function calling 时的替代方案：ReAct 文本协议

工具型 Agent 在 function calling 普及**之前**是这样写的——把工具描述写进 prompt，让模型**用文本格式**表达"我想调工具"：

```text
System: 可用工具: search(query), calc(expr)
        要调用工具时，输出格式:
        Action: search
        Action Input: {"query": "python 历史"}

模型:  我需要查一下 python 的历史
       Action: search
       Action Input: {"query": "python history"}
```

harness 用正则/字符串解析 `Action:` 和 `Action Input:`，执行后把结果追加回对话，模型再继续。这就是 LangChain 早期 AutoGPT 时代的 **ReAct 模式**，**不需要原生 function calling**。

**但现代实践没人这么干了**，代价太大：

| 维度 | 原生 function calling | ReAct 文本协议 |
|------|---------------------|----------------|
| 解析稳定性 | API 返回结构化 `tool_use` 块，不可能错 | 正则解析，模型多写个括号/少个引号就崩 |
| 结束判断 | `stop_reason` 明确区分"调工具/结束" | 靠文本里有没有 `Action:` 猜，容易误判 |
| 参数校验 | 模型按 schema 生成，天然类型正确 | 纯文本，需自己解析 JSON 再校验 |
| 多工具并行 | 一次返回多个 `tool_use` 块 | 一次只能一个 Action |

---

## 4. 补充：Agent 的类型与实现方式

上面提到"Agent 不一定要用工具"，这里系统展开。按"行动方式"划分，Agent 大致有五种类型：

### 4.1 工具型 Agent（本项目类型）

**定义**：通过 function calling 调用外部工具（文件 I/O、Shell、网络、数据库）完成任务。

**核心特征**：
- 工具定义走结构化 Schema（`tools` 参数）
- `stop_reason == "tool_use"` 驱动循环
- 工具执行结果回填到对话（`tool_result`），模型基于结果继续决策

**典型实现**：本项目 s01-s20、Claude Code、OpenAI Code Interpreter 的底座。

**实现方式（Harness 五要素）**：
```
工具（怎么定义）→ 知识（怎么注入）→ 感知（怎么输入）
→ 行动（怎么执行）→ 权限（怎么管控）
```

### 4.2 纯对话 Agent

**定义**：不调用任何工具，只做文本生成。

**核心特征**：单一 `messages` 循环，`stop_reason` 恒为 end_turn，没有工具往返。

**典型实现**：客服机器人、答疑助手、角色扮演。

**代码形态**（最简）：

```python
def chat_agent(messages):
    response = client.messages.create(model=MODEL, messages=messages)
    messages.append({"role": "assistant", "content": response.content})
    return response.content
```

### 4.3 纯推理 Agent（思维链）

**定义**：把复杂问题拆成多步推理，逐步给出结论——但推理在"文本"层完成，不碰工具。

**核心特征**：
- 依赖模型本身的推理能力（CoT / ToT 等提示技巧）
- 可能需要多次 self-consistency 采样取多数答案
- 无外部状态，输出即答案

**典型实现**：数学解题、逻辑分析、架构评估。

### 4.4 ReAct 文本协议 Agent

**定义**：工具型 Agent 的"文本协议"实现——不靠 API 级 function calling，靠 prompt 约束 + 文本解析。

**核心特征**：`Action / Action Input` 格式 + 正则解析（见 3.3），解析脆弱是主要缺点。

**典型实现**：LangChain 早期、AutoGPT 时代、Voyager（游戏 agent）。

### 4.5 代码执行型 Agent

**定义**：让 LLM 生成代码，harness 在沙箱里执行代码并回传 stdout——工具 = 语言运行时。

**核心特征**：
- 模型输出 Python/JS 代码块
- 执行环境通常是沙箱（防任意命令）
- 结果 = 程序输出（stdout / 文件 / 图表）

**典型实现**：OpenAI Code Interpreter、Jupyter 内核 agent。

**代码形态**：

```python
# 模型生成的代码
code = "print(1 + 1)"
# harness 沙箱执行
result = sandbox_run(code)   # "2"
```

---

## 5. 一句话总结

> Function calling 是**工具型 Agent** 的核心依赖——本项目（s01-s20）整个循环建立在 `stop_reason == "tool_use"` 之上，所以选模型必须支持它。但"Agent"不等于工具型：纯对话、纯推理、代码执行、文本协议（ReAct）都是合法的 Agent 形态，各自动力不同。**要不要 function calling，取决于你的 Agent 要不要行动接口。**

---

## 6. 关联阅读

- `学习记录/12-s17 自治 Agent 机制.md` — teammate 的 WORK→IDLE→SHUTDOWN 生命周期
- `学习记录/03-Harness 核心概念.md` — Harness 五要素（工具/知识/感知/行动/权限）
- `s17_autonomous_agents/demo_code.py` — 两层 function calling 的实际代码
- [Anthropic Tool Use 文档](https://docs.anthropic.com/zh-cn/docs/agents-and-tools/tool-use)

---

**文档生成时间：** 2026-08-05
