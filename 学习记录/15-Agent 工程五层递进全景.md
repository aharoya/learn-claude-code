# Agent 工程五层递进：Prompt → Context → Harness → Loop → Graph

> 构建一个 Agent，复杂度从"单次模型调用"到"多 Agent 系统"逐层爬升。每一层的出现，都是因为上一层"不够用了"。本文用五层递进框架串起全部 Agent 工程概念，并为每层给出**代表性实现**（业界框架、论文、你的项目对应章节）。读完你会明白：Prompt 管"怎么问"，Context 管"看到什么"，Harness 管"能做什么"，Loop 管"怎么持续干"，Graph 管"多 Agent 怎么组织"。

---

## 核心逻辑：每层都是"上一层不够用"的产物

```
Prompt → Context → Harness → Loop → Graph
  ①        ②         ③        ④      ⑤
 单次调用   单次装不下  只能"说"    一步干不完  一条循环不够
```

| 层 | 管什么 | 上一层的局限（为什么需要这一层） |
|----|--------|--------------------------------|
| ① **Prompt Engineering** | 单个 LLM 调用的输入文本怎么写 | 起点。没有上一层 |
| ② **Context Engineering** | "送什么进上下文、怎么送" | Prompt 是死的话术，但 Agent 需要**动态上下文**（记忆、工具描述、检索结果、历史）——单次 prompt 装不下、也决定不了放什么 |
| ③ **Harness Engineering** | 模型**外部环境**：工具、权限、沙箱、感知 | 模型只能"说"，**不能"做"**——要操作世界，得造工具 + 权限 + 行动接口 |
| ④ **Loop Engineering** | Agent 循环：调模型→执行→回填→再调 | 单次调用只能做一步，**多步任务要靠循环串起来** |
| ⑤ **Graph Engineering** | 复杂控制流：多 Agent、分支、并行、中断 | 一条直线循环不够——**多 Agent 协作、条件分支、人机审批**需要流程图 |

---

## ① Prompt Engineering —— 单次调用的话术

**管什么**：单个 LLM 调用的输入文本怎么设计——system prompt、few-shot 示例、输出格式约束、思维链（CoT）引导。

**本质**：引导模型发挥**已有**能力。边界：模型不会算数，prompt 再好也算不对。

**代表性实现**：

| 实现 | 说明 |
|------|------|
| System Prompt 设计规范 | Anthropic / OpenAI 官方的 system prompt 最佳实践（角色、规则、输出格式） |
| Few-shot / 思维链（CoT） | 给示例引导格式，让模型"逐步思考"再回答 |
| Prompt 模板库 | LangChain `PromptTemplate`、Jinja2 模板化 prompt |
| Prompt 评测工具 | **promptfoo**、LangSmith——对同一任务批量测不同 prompt 版本 |

**对应本项目**：s10 的 `PROMPT_SECTIONS` 分段设计（虽然是 Context 层功能，但 prompt 文本怎么写属于这层）。

**局限**：Prompt 是写死的模板，Agent 需要"每轮不同"的输入 → 必须上 Context 层。

---

## ② Context Engineering —— 让模型"看到"对的上下文

**管什么**：**动态组装送进模型的全部上下文**——记忆注入、工具描述、RAG 检索结果、历史压缩、prompt caching。

**本质**：决定"这场对话，模型该看到什么、不该看到什么"。这是 Anthropic 官方明确提出的概念（*Context Engineering* 博客）。

**代表性实现**：

| 实现 | 说明 |
|------|------|
| **RAG（检索增强）** | LlamaIndex、LangChain + 向量库（Pinecone / Milvus / Weaviate）——把知识库向量化，按需检索注入 |
| **记忆系统** | **MemGPT / Letta**（论文 *MemGPT: Towards LLMs as Operating Systems*）——分层记忆：上下文 / 外部存储 / 工作记忆 |
| **上下文压缩** | 长对话自动摘要/裁剪，保住核心信息再送模型 |
| **Prompt Caching** | Anthropic 的 prompt caching——静态部分（工具描述、system）缓存，动态部分每轮变化，省 token 省钱 |
| **Token/上下文预算管理** | 监控上下文长度，决定历史截取策略（如本项目 `messages[-20:]`） |

**对应本项目**：s08（上下文压缩）、s09（持久化记忆）、s10（动态组装）、`messages[-20:]` 截断。

**局限**：模型只能"看"，看再多也不能改变世界 → 需要 Harness 层给行动接口。

---

## ③ Harness Engineering —— 让模型"能做事"

**管什么**：**模型外部的整个环境**——工具注册、权限门、沙箱、文件系统、网络、协议（MCP）、安全边界。即 CLAUDE.md 里的 **Harness = 工具 + 知识 + 感知 + 行动接口 + 权限**。

**本质**：造载具。模型是驾驶员，这层是车。**这是 Agent 工程里工程量最大的一层**。

**代表性实现**：

| 实现 | 说明 |
|------|------|
| **Claude Code** | Anthropic 官方终端 Agent——完整的 harness（工具 + 权限审批 + hooks + 工作树隔离 + 多 Agent） |
| **MCP（Model Context Protocol）** | Anthropic 开源的标准协议——Agent 通过统一协议连外部工具/服务，工具池可插拔 |
| **OpenAI Assistants API** | OpenAI 托管版 harness——工具、知识库、代码解释器开箱即用 |
| **工具定义 + 分发** | 工具 Schema（`input_schema`）+ 注册表 + `TOOL_HANDLERS` 分发 |
| **沙箱 / 权限门** | 容器隔离（Docker）、权限审批流、命令黑名单 |

**对应本项目**：s02（工具分发）、s03（权限门）、s04（钩子）、s19（MCP）——**你的项目 90% 在这一层**。

**局限**：有了工具，但没人驱动它们反复工作 → 需要 Loop 层。

---

## ④ Loop Engineering —— 把"一次调用"变成"持续工作"

**管什么**：Agent 循环——`调模型 → 解析 tool_use → 执行工具 → 回填 tool_result → 再调`，直到模型说"做完了"。

**本质**：**把单次调用串成多步自主工作**。这是 Agent 与"一次问答"的分水岭。

**代表性实现**：

| 实现 | 说明 |
|------|------|
| **Anthropic Agent Loop 官方模式** | 文档里的标准循环——`messages.append → 检查 stop_reason → 执行工具 → 回填` |
| **ReAct 循环** | Yao et al. 2022——Thought/Action/Observation 交替 |
| **LangChain AgentExecutor / create_agent** | 框架封装的 agent 循环 |
| **AutoGPT / BabyAGI** | 早期自主循环（任务队列 + 循环执行） |
| **pydantic-ai** | 结构化 agent 循环（类型安全的工具调用） |

**对应本项目**：**s01 那 20 行不可变核心循环**——整个项目的地基，s02-s11 全是它的外挂：

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

**局限**：循环是"一条直线"，复杂任务需要分支/并行/多 Agent → 需要 Graph 层。

---

## ⑤ Graph Engineering —— 从"一条线"到"一张网"

**管什么**：**显式控制流**——节点是步骤、边是转移条件，支持循环、条件分支、并行、人机中断、多 Agent 协作。

**本质**：Loop 是直线，Graph 是**多路径的流程图**。**这是多 Agent 系统和精细流程控制的主战场**。

**代表性实现**：

| 实现 | 说明 |
|------|------|
| **LangGraph**（业界最主流） | 有向图建模 Agent 流程——节点/边/条件转移/循环/interrupt（人机审批点） |
| **CrewAI Flows** | 声明式流程 + 事件驱动，简化版 graph |
| **AutoGen Group Chat** | 多 Agent 围桌讨论（节点 = agent，边 = 发言顺序） |
| **Temporal** | 通用工作流编排引擎——Agent 流程的持久化、重试、恢复（生产级） |
| **Azure AI Agent Service** | 托管编排——多 Agent + 工具 + 记忆 + 云部署 |

**对应本项目**：s12-s20 的多 Agent 系统（任务、团队、协议、自治认领）——用 Loop 硬写出来的"伪 Graph"（MessageBus + 任务状态机 + 协议，本质上在模拟 graph 的节点和边）。

### ⑤+ Graph 与 Workflow 的区别：都是图，但静态 vs 动态

**从图论意义上，Graph 确实是 Workflow 的一种**（都是"节点 + 边"）。但在 Agent 工程语境里，两个词约定俗成指向不同的东西——区别不在于"是不是图"，而在于"**下一步由谁决定、边是怎么连的**"：

| 维度 | Workflow（传统工作流） | Graph（Agent 图，如 LangGraph） |
|------|----------------------|-------------------------------|
| 谁来定下一步 | 开发者 / 硬编码 if-else | 模型 / 运行时基于状态动态路由 |
| 边的性质 | 固定边（A 做完必去 B） | **条件边**（节点根据输出/状态选下一跳） |
| 结构 | 通常是 **DAG**（有向无环，单向流动） | 允许**循环、回退**（失败重试、重新规划） |
| 模型决策是否参与流程 | 没有，流程是代码画死的 | 有，LLM 节点的输出决定走哪条边 |
| 动态性 | 静态：结构在运行前完全确定 | 动态：结构显式，但**走向运行时才确定** |

**一句话版本**：
- Workflow = 图，但每条边固定，模型不参与路由，流程是开发者画死的
- Graph（Agent 图）= 图，但边可以是"条件路由"，模型/运行时决定走哪条边，允许循环和中断

**直观示例对比**：

```
传统 Workflow（Airflow / n8n / LCEL Chain）:
  拉数据 ─→ 清洗 ─→ 建模 ─→ 出报告
  每个箭头写死在代码里，没有分支，没有模型决策

Agent Graph（LangGraph）:
  用户提问 ─→ 需要工具吗? ──是──→ 调工具 ─→ 结果够了吗? ──不够──→ 再调
                    │                        │
                    └────否───→ 直接回答      └──够了──→ 结束
  条件边：由模型/状态决定走哪条，可以循环回退
```

上面两个流程都是"图"，但**唯一的区别**就在边的性质——传统 Workflow 的边是固定的（拉完数据必去清洗），Agent Graph 的边是"带问号的条件"（要不要调工具？结果够不够？），由模型/运行时在运行时决定。

**权威说法（Anthropic 官方 *Building effective agents*）**：
- **Workflows**：LLM 和工具通过**预定义的代码路径**编排（路径运行前定死）
- **Agents**：LLM **动态主导自己的流程**，控制"任务怎么完成"

而 LangGraph 的位置是：**用它一套图结构，两种都能表达**——纯固定顺序的图 = Workflow，带模型节点 + 条件路由的图 = Agent。官方自称 "low-level orchestration"，就是要统一 workflow 和 agent。

**打个比方**：Workflow 是铺好的轨道，火车只能沿着跑；Graph 是轨道 + 道岔，道岔由司机（模型）根据路况决定扳向哪边。都是铁路系统，但一个是"跑死线路"，一个是"有决策的跑法"。

**落回本项目**：s12-s20 就是**手写 Workflow**——流程走向（谁认领任务、走 shutdown 还是 plan 协议）靠 `if/else` 在代码里画死，没有显式的图结构。它和 LangGraph 的差别正是"Graph vs Workflow"的差别：LangGraph 把"流程图"本身做成了可编辑、可中断、可并行、可恢复的一等公民。**学完 s20 再回头看 LangGraph，你已经手写过它的简化版了。**

---

## 关键洞察

### 洞察 1：前四层是"单 Agent 内部"，第五层才跨 Agent

```
单 Agent 内部（1→4）               跨 Agent（5）
─────────────────               ─────────────────
Prompt → Context → Harness → Loop
                              │
                              └─→ Graph：多个 Loop 怎么编排
                                    分支 / 并行 / 审批 / 多 Agent
```

**Loop 是单个 Agent 的骨架，Graph 是多个 Agent 的组织方式。** 你的项目 s01-s11 都是单 Agent（Loop 内），s12-s20 才进入多 Agent（Graph 的简化版）。

### 洞察 2：构建范式 = 在 Graph 层画什么样的图

之前讨论的"构建范式"（ReAct、Plan-and-Execute、Human-in-the-loop、多 Agent）**说白了就是第五层画图方式**：

| 构建范式 | 画出来的图 |
|---------|-----------|
| ReAct | 最简：一条直线循环（Loop 即 Graph 的特例） |
| Plan-and-Execute | 两条路径：规划节点 → 执行节点 → 反馈回规划 |
| Human-in-the-loop | 在图上插入"审批"节点，暂停等人工 |
| 多 Agent 主从 | 一个 Orchestrator 节点 + N 个 Worker 子图 |
| 多 Agent 群聊 | 环状图：Agent A → B → C → A 轮流发言 |

**所以五层递进的终点，就是"画图"**——前面四层决定每个节点能干什么，第五层决定节点怎么连。

---

## 速查表：五层 × 代表性实现 × 对应项目

| 层 | 一句话 | 代表实现（业界） | 对应本项目 |
|----|--------|-----------------|-----------|
| ① Prompt | 怎么问 | system 规范、CoT、promptfoo | s10（prompt 文本设计） |
| ② Context | 看到什么 | RAG（LlamaIndex）、MemGPT/Letta、prompt caching | s08/s09/s10 |
| ③ Harness | 能做什么 | Claude Code、MCP、Assistants API | s02/s03/s04/s19 |
| ④ Loop | 怎么持续干 | Anthropic Agent Loop、LangChain AgentExecutor | **s01（20 行核心循环）** |
| ⑤ Graph | 多 Agent 怎么组织 | **LangGraph**、CrewAI Flows、AutoGen、Temporal | s12-s20（伪 Graph） |

---

## 一句话总结

> Agent 工程从"给模型写一句话"（Prompt）出发，逐步扩展：单次装不下 → 管理上下文（Context）；只说不做 → 造环境（Harness）；一步干不完 → 串成循环（Loop）；一条线不够 → 画成图（Graph）。**每层解决上一层解决不了的问题，最终落地为"画一张多 Agent 流程图"**——这就是五层递进的全景。

---

## 关联阅读

- `学习记录/13-LLM function calling 详解.md` — ④ Loop 层的核心机制（tool_use 块、两层 function calling）
- `学习记录/14-Agent 构建范式全景.md` — 洞察 2 的展开（各种范式 = 各种图）
- `学习记录/03-Harness 核心概念.md` — ③ Harness 层的五要素
- Anthropic *Context Engineering* 博客 — ② Context 层的官方论述

---

**文档生成时间：** 2026-08-05
