# Harness 核心概念 —— 以 Claude Code 官方文档为主参考

> 本文档以 [Claude Code 官方文档](https://code.claude.com/docs/en/features-overview) 的扩展能力分类体系为主线，结合本项目 s01-s20 的实现进行对比说明。 

> 可以参考一下这篇博客[Tool System 横向对比：Tool、Skill、Plugin、MCP、Hook 到底怎么区分](https://hfl-ai-agent-lab.vercel.app/topics/tool-system-comparison)
---
Tool 是具体能力，Function Calling 是模型侧机制，Skill 是能力封装，Plugin 是扩展机制，MCP 是协议层，Hook 是治理入口。 它们不是互斥关系，而是处在不同抽象层。
## 先导：六大概念横向对比

先画一张全览表，把 six 个概念的定位一次对齐：

| 概念 | 解决的问题 | 更接近哪一层 | 典型关注点 |
|------|-----------|------------|-----------|
| **Tool** | Agent 能调用什么具体能力 | 执行层 | tool_name、input_schema、output_schema、permission_level |
| **Function Calling** | 模型如何表达工具调用意图 | 模型侧机制 | 模型输出结构化 tool_call、参数 JSON |
| **Skill** | 一类任务怎么做得专业 | 能力封装层 | 任务说明书、工作流协议、可加载知识包 |
| **Plugin** | 系统如何扩展能力 | 扩展层 | 注册 provider、channel、tool、skill |
| **MCP** | 工具如何标准化接入 | 协议层 | 工具发现、参数描述、资源暴露 |
| **Hook** | 执行过程如何被治理 | 治理层 | 执行前检查、工具调用拦截、输出审查、审计日志 |

> 面试表达
> - 我不会把 Tool、Skill、Plugin、MCP、Hook 都混成"函数调用"。在学习和架构抽象层面，Tool 是具体能力，Skill 是能力封装，Plugin 是扩展机制，MCP 是协议层，Hook 是治理入口。它们处在不同抽象层，解决不同的问题。
> - 生产级 Agent 工具系统要考虑 Schema、权限、错误处理、Trace、安全和评测。
>   - 从站内已有拆解内容看，Hermes 的 Tool Registry 自注册 + Skill 任务说明书 + MCP 外部接入提供了一种工具系统设计思路；
>   - OpenClaw 的 Tool + Skill + Plugin 三层能力体系 + Tool Policy + Exec Approval 提供了另一种思路；
>   - Harness Engineering 的 Tool Gateway + Permission & Governance + Hooks / Plugins / MCP 提供了第三种思路。
> - 这也是 Agent 从 Demo 走向工程系统的关键差异——Demo 只需要 Function Calling，生产系统需要完整的 Tool System。

## 第一步：官方怎么分类这些概念？

Claude Code 官方文档把扩展能力分成 8 个特征，按"什么时候加载、干什么用"排列：

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

### 官方决策表：什么时候该用什么

官方文档还提供了一张实用的决策表：

| 触发场景 | 该用 |
|---------|------|
| Claude 把同一个约定搞错两次 | 加到 **CLAUDE.md** |
| 你反复输入同一段 prompt 开始一个任务 | 存为 **Skill** |
| 你把同一套流程贴到对话里第三次了 | 写成 **Skill** |
| 你反复从浏览器复制数据给 Claude | 连一个 **MCP server** |
| 一个旁支任务刷爆了你的上下文 | 通过 **Subagent** 处理 |
| 某件事每次都要自动发生 | 写一个 **Hook** |
| 另一个仓库也需要同一套配置 | 打包成 **Plugin** |

---

## 第二步：官方文档对每个概念的定义

### Tool（工具）

官方文档把 Tool 分成两层：

- **内建工具（Built-in tools）**：文件操作、搜索、执行、网络访问——这些是 Claude Code 自带的核心能力，不需要配置
- **MCP 工具**：通过 MCP 协议从外部服务器发现的工具——**对 LLM 来说和内建工具没有区别**

在 API 层面就是 **Function Calling**：你传 `tools` 参数，LLM 通过 `stop_reason: "tool_use"` 选择调用哪个。

```python
# 定义（给 LLM 看——它据此决定调不调）
{"name": "bash", "description": "Run a shell command",
 "input_schema": {"properties": {"command": {"type": "string"}}}}

# 实现（Harness 执行）
def run_bash(command):
    return subprocess.run(...)
```

**本项目对应**：s01/s02 TOOLS + TOOL_HANDLERS 策略模式。s01 只有 1 个工具（bash，硬编码），s02 扩展到 5 个（bash/read/write/edit/glob，查表分发），s20 扩展到 26 个。

---

### Skill（技能）

官方定义：**可复用的知识和工作流。** 分为两种类型：

- **Reference skill（参考技能）**：知识型，Claude 在对话中按需参考（如 API 风格指南、数据库 schema 说明）
- **Action skill（行动技能）**：工作流型，通过 `/<name>` 触发（如 `/deploy`、`/review`、`/release`）

关键特性：

- Skill 可以 `context: fork` 在隔离上下文（子 Agent）中执行
- Skill 可以结合 MCP：MCP 提供连接，Skill 教 Claude 怎么用好它
- Skill 文件用 Markdown 编写，放在 `skills/` 目录下
- 描述在每次会话加载时注入，完整内容在调用时加载

```python
# 本项目 s07：两级注入
# 第一级：Skill 目录（常驻 SYSTEM）
"Skills catalog:\n- code-review: Review code changes\n- api-docs: API doc style guide"

# 第二级：模型调用 load_skill("code-review") → 完整文档（按需加载）
def load_skill(name):
    return SKILL_REGISTRY[name]["content"]
```

**差异：** 本项目 s07 只实现了 Reference skill（知识型），没有实现 Action skill（`/<name>` 触发的工作流）。在 Claude Code 中，Skill 是一个更广泛的概念。

---

### Hook（钩子）

官方定义：**在生命周期事件上自动触发的处理程序。** 支持 5 种类型：

| Hook 类型 | 做什么 |
|----------|--------|
| `command` | 跑 Shell 命令 |
| `http` | 发 HTTP POST 请求 |
| `mcp_tool` | 调已连接的 MCP 服务上的工具 |
| `prompt` | 发给 LLM 做单轮 yes/no 判断 |
| `agent` | 创建子 Agent 做复杂校验 |

支持 **27 种事件**，包括本项目 s04 用到的 `PreToolUse`、`PostToolUse`、`UserPromptSubmit`、`Stop`。

**官方特别强调：** Guardrails（护栏逻辑）必须放在 Hook 里。CLAUDE.md 或 Skill 里的"不要编辑 .env"只是请求，`PreToolUse` Hook 阻止编辑才是**强制执行**。

```python
# 本项目 s04：四种事件的 Hook 注册表
HOOKS = {"UserPromptSubmit": [], "PreToolUse": [],
         "PostToolUse": [], "Stop": []}

def trigger_hooks(event, *args):
    for callback in HOOKS[event]:
        result = callback(*args)
        if result is not None:
            return result  # 拦截信号

# 注册权限检查作为 Hook
register_hook("PreToolUse", permission_hook)
register_hook("PreToolUse", log_hook)
```

> **Permission 不是独立概念，而是 Hook 的一个实例。** 本项目 s03 单独成章是教学需要——先展示"硬编码权限检查到循环体"的问题，再引出 s04 的 Hook 抽象。

---

### Plugin（插件）

官方定义：**把 Skills、Hooks、MCP Servers、Subagents、LSP Servers 打包成一个可安装单元。** Plugin skills 有命名空间隔离（`/<plugin-name>:<skill-name>`），多个 Plugin 可以共存。

Plugin 是一个**打包和分发层**——它不引入新概念，而是把已有的概念组合在一起。

Plugin 目录结构示例：

```
my-plugin/
├── .claude-plugin/plugin.json   # 清单（可选）
├── skills/                       # Skills（SKILL.md 文件）
├── commands/                     # 旧版格式
├── agents/                       # 自定义子 Agent
├── hooks/hooks.json              # 事件处理程序
└── .mcp.json                     # MCP 服务器定义
```

---

## 第三步：官方文档的核心对比

### Tool vs Hook

| 方面 | Tool | Hook |
|------|------|------|
| **谁触发** | 模型主动（Function Calling） | 系统自动（事件触发） |
| **LLM 知道吗** | 是，模型主动选择 | 否，完全无感知 |
| **返回值去哪** | 必回传给 LLM（tool_result） | 可选——可拒绝（回传 LLM）、可静默记录、可注入额外上下文 |
| **本质** | 模型问"我要做这个"→ 系统帮它做 | 系统说"我先检查一下"→ 再做 |

### MCP vs Skill

| 方面 | MCP | Skill |
|------|-----|-------|
| **是什么** | 连接外部服务的协议 | 知识、工作流、参考材料 |
| **提供什么** | 工具和数据访问 | 知识、工作流、参考材料 |
| **例子** | Slack 集成、数据库查询 | 代码审查清单、API 风格指南 |

文档特别指出：**它们解决的问题不同，而且配合得很好。** MCP 提供连接，Skill 教 Claude 怎么用好那个连接。

> 例子：一个 MCP 服务器连接 Claude 到你的数据库。一个 Skill 教 Claude 你的数据模型、常用查询模式、以及不同任务应该用哪些表。

### Hook vs Skill

| 方面 | Hook | Skill |
|------|------|-------|
| **运行什么** | Shell/HTTP/LLM/Subagent | 指令文本，Claude 阅读后执行 |
| **触发方式** | 生命周期事件（保证触发） | 你输入 `/<name>` 或 Claude 自行判断 |
| **确定性** | 每次触发都执行，保证 | Claude 解读指令，结果可能不同 |
| **上下文成本** | 零（除非返回输出） | 描述每次会话加载 |
| **最适合** | 格式化、拦截危险命令、日志 | 需要推理的工作流、参考材料 |

### Subagent vs Agent Team

| 方面 | Subagent | Agent Team |
|------|----------|------------|
| **上下文** | 独立窗口，结果回主对话 | 完全独立 |
| **通信** | 只和主 Agent 通信 | 队友之间直接通信 |
| **协调** | 主 Agent 管理所有工作 | 共享任务看板，自治协调 |
| **最适合** | 只需要结果的任务 | 需要讨论和协作的复杂工作 |

### 技能和子 Agent 的配合

一个 Skill 可以触发多个 Subagent 并行工作，比如 `/audit` 技能可以同时启动安全、性能、风格三个子 Agent 做审查。

---

## 第四步：Plugin 和 MCP 到底是什么关系？

这是两个不同层次的概念。区分"概念"和"实现协议"就清楚了：

```
Plugin（概念：可插拔扩展）
  ├── 实现方式 A：ChatGPT Plugins（OpenAI 自己的协议）
  ├── 实现方式 B：MCP - Model Context Protocol（开放标准协议）
  └── 实现方式 C：自定义协议
```

类比 USB：

| | 类比 |
|---|------|
| **Plugin（概念）** | "能插上去用"这个想法 |
| **MCP（协议）** | USB 协议——规定了插口的形状、数据怎么传 |
| **MCP Server** | 一个 USB 设备（U 盘、打印机） |
| **connect_mcp()** | 把 USB 设备插上去 |

本项目 s19 的标题叫"MCP Plugin"正是这个意思——用 MCP 协议来实现插件能力。

---

## 第五步：和本项目 s01-s20 的关系

Claude Code 是生产级的商业产品，本教程是它的教学简化版。两者的概念映射如下：

| Claude Code 官方概念 | 本项目对应章节 | 差异 |
|---------------------|---------------|------|
| **Built-in Tools** | s01/s02 | 一致——都是硬编码的工具定义 |
| **CLAUDE.md** | s07 Skill 的一部分 | 官方将 CLAUDE.md 和 Skill 分开；本项目把持久化指令也视为 Skill 的一种 |
| **Skills** | s07 | 一致——目录+按需加载。本项目只有 Reference skill，缺少 Action skill（`/<name>` 触发） |
| **Hooks** | s04 | 一致——四种事件吻合。官方有 27 种事件 |
| **Permission** | s03 | 官方放在 PreToolUse Hook 里；本项目单独成章是教学需要 |
| **MCP** | s19 | 一致——`mcp__{server}__{tool}` 命名空间 |
| **Subagents** | s06 | 一致——隔离上下文 + 只返摘要 |
| **Context Compaction** | s08 | 官方叫 context window 管理 |
| **Memory** | s09 | 官方通过 CLAUDE.md + MCP memory server 实现 |
| **Task System** | s12 | 官方 Agent teams 里面有 TaskCreate |
| **Background Tasks** | s13 | 属于内建能力 |
| **Cron** | s14 | 无直接对应，可通过 Hook + MCP 组合实现 |
| **Agent Teams** | s15/s16/s17 | 官方 Agent teams（实验性），MessageBus/Protocol/Autonomous 对应 |
| **Worktree** | s18 | 官方有 WorktreeCreate/WorktreeRemove Hook 事件 |
| **Plugins** | 无 | 打包分发层，本项目没实现 |
| **Code Intelligence** | 无 | LSP 集成，本项目没覆盖 |

---

## 参考来源

- [Extend Claude Code — Official Docs](https://code.claude.com/docs/en/features-overview)
- [Hooks Reference](https://code.claude.com/docs/en/hooks)
- [Plugins Reference](https://code.claude.com/docs/en/plugins-reference)
- [Glossary — Agentic Harness](https://code.claude.com/docs/en/glossary)
