# Agent 构建范式全景：市面上有哪些、各自适合什么场景

> 本文盘点业界（不限于本项目）主流的 Agent 构建范式。每种范式给出一句话定义、核心流程、代表框架/论文，以及**最适合的场景**。读完你会明白：市面没有"最好的范式"，选型 = 看任务复杂度 + 协作需求 + 安全要求。与 `学习记录/13`（function calling 机制）互补——13 讲"工具怎么调用"，本文讲"Agent 整体怎么搭"。

---

## 1. ReAct（Reasoning + Acting）—— 最经典的自主范式

**一句话**：让模型在"思考"和"行动"之间交替——思考决定下一步做什么，行动调用工具/搜索，观察结果再思考。

```
Thought → Action → Observation → Thought → ...
```

- **出处**：Yao et al. 2022，论文 *ReAct: Synergizing Reasoning and Acting in Language Models*
- **代表**：早期 LangChain Agent、AutoGPT、BabyAGI
- **适合场景**：
  - 开放型任务，结果不预知（"研究一个问题并给出报告"）
  - 简单到中等复杂度的工具调用
  - 作为所有其他范式的地基（别的范式都是它的变形或叠加）

---

## 2. Function Calling / Tool-Use —— 现代工程标准

**一句话**：LLM API 原生支持"结构化工具调用"——模型返回 `tool_use` 块，harness 执行后回填 `tool_result`。本质是 ReAct 的工程化，但有原生 schema 约束，解析稳定。

- **代表**：OpenAI Function Calling、Anthropic Tool Use、所有主流 SDK
- **适合场景**：
  - 任何需要操作外部世界的 Agent（文件、Shell、网络、数据库）
  - 对解析稳定性有要求的生产系统（文本协议解析一崩整个流程就断）
  - **现代做工具型 Agent 的事实标准**——你的项目（s01-s20）就是这个范式

---

## 3. Plan-and-Execute —— 先规划后执行

**一句话**：模型**先产出完整计划**（可能多步），再逐步执行，执行后根据结果决定是否调整。解决 ReAct"走一步算一步、容易跑偏"的问题。

```
Plan（生成计划）→ Execute（逐步执行）→ 反馈 → 必要时重新规划
```

- **出处**：*Plan-and-Solve Prompting*（Wang et al. 2023）；LangChain 的 PlanAndExecute
- **适合场景**：
  - 多步骤、目标明确的长任务（"搭建一个完整项目后端"）
  - 任务需要顺序依赖（步骤 2 依赖步骤 1 的产物）
  - 容易偏离大方向的任务——先写计划，执行时对照检查

---

## 4. Reflexion —— 自我反思

**一句话**：Agent 失败后，基于反馈生成一条**反思摘要**存入记忆（episodic memory），下一次尝试时带着反思避免重复犯错。本质是"从错误中学习"。

```
执行 → 失败/反馈 → 反思 → 写入记忆 → 下次带反思重试
```

- **出处**：Shinn et al. 2023，*Reflexion: Language Agents with Verbal Reinforcement Learning*
- **适合场景**：
  - 需要多轮试错的任务（写代码反复编译报错、调试问题）
  - 测试驱动的任务——每轮失败都留下可复用的"坑"记录
  - 有明确成败信号的场景（测试通过/不通过、LLM 评审打分）

---

## 5. 记忆增强（Memory-augmented）—— 解决上下文有限

**一句话**：给 Agent 配分层记忆系统——短期（当前上下文）、长期（外部存储/向量库）、工作记忆。用 RAG 检索相关记忆，或压缩长上下文。

- **代表**：MemGPT / Letta（论文 *MemGPT: Towards LLMs as Operating Systems*）、LangChain 记忆组件
- **适合场景**：
  - 长对话（客服会话、多轮访谈）
  - 跨会话恢复（Agent 重启后还记得上次进度）
  - 需要领域知识的 Agent——把知识库向量化，按需检索注入

---

## 6. 多 Agent 范式 —— 多个 Agent 协作

**一句话**：把一个大任务拆给多个 Agent 协作，有四种主流子范式：

| 子范式 | 协作方式 | 代表 | 适合场景 |
|--------|---------|------|---------|
| **主从（Orchestrator-Worker）** | 一个主 Agent 拆任务、派给 worker、回收结果 | Anthropic 多 Agent 研究、CrewAI hierarchical | 任务可清晰拆分、worker 相互独立 |
| **群聊（Group Chat）** | 多个 Agent 围桌讨论，或互相辩论/评审 | AutoGen、ChatDev、MetaGPT（模拟软件公司） | 需要多视角讨论、互相审查（如代码评审、方案辩论） |
| **流水线接力（Sequential）** | Agent 按顺序传递中间产物 | CrewAI sequential process | 有明确上下游：需求 → 设计 → 编码 → 测试 |
| **市场自治（Task Board）** | Agent 自己看板认领，无中央分配 | 你的项目 s17 就是这种 | 任务量大、并行度高、不需要强协调 |

---

## 7. 图/状态机范式（Graph）—— 显式控制流

**一句话**：把 Agent 流程建模成**有向图**——节点是步骤，边是转移条件。支持循环、条件分支、并行、**人类中断点**。介于"纯 Workflow"和"纯自主"之间：模型决定分支走向，但流程结构是显式定义的。

- **代表**：**LangGraph**（业界最主流）、CrewAI Flows
- **适合场景**：
  - 需要精细控制流程、含分支逻辑复杂的任务
  - 需要并行执行多个子流程再汇总
  - **必须有人机交互点**的场景（审批、确认）——Graph 的 interrupt 是标准做法
  - 生产级多 Agent 系统，几乎都选它当底座

---

## 8. Human-in-the-loop —— 人机协作

**一句话**：在 Agent 流程中显式插入**审批/确认点**，高风险操作（改文件、发邮件、下单）必须经人工确认才继续。

- **代表**：LangGraph interrupt、OpenAI Assistants
- **适合场景**：
  - 高风险、不可逆的操作（删数据、发对外邮件、真金白银的订单）
  - 需要人工监督的行业流程（医疗、金融、法律）
  - **生产级 Agent 必备**——可信度的来源。Claude Code 的权限审批也是这个

---

## 9. Write-and-Execute（代码执行型）—— 工具即语言运行时

**一句话**：让模型**生成代码**，在沙箱里执行并回传输出（stdout/文件/图表），工具就是"解释器"本身。

- **代表**：OpenAI Code Interpreter（Advanced Data Analysis）、Codex
- **适合场景**：
  - 数据分析（读取 CSV → 画图 → 出报告）
  - 需要复杂计算、模型单靠文本算不出的任务
  - 结果以代码产物形态交付的场景（生成脚本、生成图表）

---

## 10. Workflow / Pipeline —— 严格说不算"自主"

**一句话**：预定义固定步骤，没有模型决策循环，只是按序串联调用。常被归入"Agent 编排"讨论。

- **代表**：LangChain LCEL Chain、CrewAI sequential
- **适合场景**：
  - 固定流程（ETL、定时报告、数据同步）
  - 步骤和顺序都确定的批处理任务
  - 成本敏感、不需要"自主"的场景——Workflow 便宜、可预测、可测试

---

## 选型指南：不同场景怎么选

### 按任务复杂度

| 任务类型 | 推荐范式 |
|---------|---------|
| 单步问答 / 简单工具调用 | ReAct / Function Calling 就够了 |
| 多步骤、目标明确的长任务 | + Plan-and-Execute（先规划） |
| 多轮试错、有成败信号 | + Reflexion（自我反思） |
| 长对话 / 需要跨会话知识 | + Memory（记忆增强） |
| 任务可拆分、需要并行 | 多 Agent（主从 或 市场自治） |
| 需要多视角讨论/审查 | 多 Agent（群聊） |
| 流程复杂、有分支/并行/审批 | **Graph（LangGraph）** |
| 有高风险不可逆操作 | + Human-in-the-loop（审批点） |
| 结果要精确计算/图表 | Write-and-Execute（代码执行） |
| 固定重复流程 | Workflow（别上 Agent） |

### 两个关键判断

1. **要不要"自主"？** 固定流程 → Workflow；开放任务 → Agent 循环。这是最省钱、最该先做的决策。
2. **谁做决策？** 单 Agent 自主 → ReAct 系；需要协调 → 多 Agent；需要人把关 → HITL。核心是回答"谁在循环里决定下一步、要不要人插手"。

### 一个现实提醒

> **范式不是互斥的，是叠加的**。真实生产系统几乎总是"ReAct/Function Calling 为底 + 按需叠加规划、记忆、反思、多 Agent、Graph 控制流、审批点"。选型不是挑一个"最好的范式"，而是回答三个问题：**任务多复杂？要不要协作？要不要人审？** 答案的组合决定了最终架构。

---

## 关联阅读

- `学习记录/13-LLM function calling 详解.md` — 工具调用的机制层（tool_use 块、两层 function calling、ReAct 文本协议）
- `学习记录/12-s17 自治 Agent 机制.md` — 多 Agent 市场自治范式的具体实现
- `学习记录/03-Harness 核心概念.md` — Harness 五要素（工具/知识/感知/行动/权限）
- 参考blog：[面试官：说一下 Agent 的常见范式，如何选型？](https://www.cnblogs.com/yifeng-coding/p/20176474)

---

**文档生成时间：** 2026-08-05
