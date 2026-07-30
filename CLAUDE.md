# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## 项目简介

这是一个**从零到一构建 AI Agent Harness（运行环境）的教程项目**。

### 核心理念

**Agent 的自主性（Agency）来自模型训练，而非外部代码编排。** 一个 AI Agent 产品 = 模型 + Harness。模型是驾驶员，Harness 是载具。本仓库教你造载具。

什么是 Harness？就是让模型能在特定环境中工作的全部基础设施：

```
Harness = 工具 + 知识 + 感知 + 行动接口 + 权限

  工具:          文件 I/O、Shell、网络、数据库、浏览器
  知识:          产品文档、领域参考、API 规范、风格指南
  感知:          git diff、错误日志、浏览器状态、传感器数据
  行动:          CLI 命令、API 调用、UI 交互
  权限:          沙箱隔离、审批工作流、信任边界
```

### 项目构成

共 20 个章节（s01-s20），每个章节在前一章基础上叠加一个新机制，最终在 s20 整合为一个完整的多 Agent 系统。

- **新版（推荐）**：根目录 `s01/`-`s20/`，每章一个文件夹，含 README（中英日三语）+ 可运行 `demo_code.py` + SVG 图解
- **旧版（保留）**：`agents/`（12 课可运行 Python）+ `docs/`（12 课文档）

### 学完你会得到什么

一个从零手写的 Agent 运行环境，包含 20 个递进机制：从最基本的 Agent Loop 到多 Agent 团队协作、MCP 协议集成、工作树隔离。所有代码独立可运行，不依赖任何 Agent 框架。

## 快速命令

```bash
# 安装依赖
pip install -r requirements.txt

# 复制环境变量并填写 ANTHROPIC_API_KEY
cp .env.example .env

# 运行任一章节的独立实现
python s01_agent_loop/demo_code.py

# 运行全量测试（需先 pip install pytest）
python -m pytest tests -q

# 运行单个测试文件
python -m pytest tests/test_agents_smoke.py -v

# 启动 Web 应用（交互式浏览课程）
cd web && npm install && npm run dev

# Web 应用类型检查
cd web && npx tsc --noEmit
```

## 项目结构

```
.
├── s01_agent_loop/          # 第 1 章 - 核心 Agent 循环
│   ├── README.md            #   中文教程文档（主源文档）
│   ├── README.en.md         #   英文翻译
│   ├── README.ja.md         #   日文翻译
│   ├── demo_code.py              #   独立可运行的实现
│   └── images/              #   SVG 图解
├── s02_tool_use/            # 第 2 章 - 工具分发
├── s03_permission/          # 第 3 章 - 权限门
├── ... (s04-s20 类似结构)
├── agents/                  # 旧版 12 课的可运行 Python 文件
│   ├── s01_agent_loop.py .. s12_worktree_task_isolation.py
│   ├── s_full.py            # 旧版完整实现（740 行）
│   └── __init__.py
├── docs/                    # 旧版 12 课文档（en/zh/ja 三语）
├── skills/                  # s07 Skill Loading 章节的技能文件
│   ├── agent-builder/       #   含 SKILL.md + references/ + scripts/
│   ├── code-review/         #   含 SKILL.md
│   ├── mcp-builder/         #   含 SKILL.md
│   └── pdf/                 #   含 SKILL.md
├── tests/                   # pytest 测试
│   ├── test_agents_smoke.py           # 编译检查 agents/*.py
│   ├── test_compaction_tool_pairs.py  # 压缩工具配对完整性
│   ├── test_s_full_background.py      # 后台任务管理
│   └── test_todo_write_string_input.py # JSON 字符串输入安全
├── web/                     # Next.js 16 Web 应用
└── .github/workflows/       # CI: CI 构建 + 两阶段测试
```

## 20 章全景

学习分为五个阶段：

| 阶段 | 章节 | 核心机制 |
|------|------|---------|
| **工具与执行** | s01-s04 | 循环、工具分发、权限门、生命周期钩子 |
| **规划与控制** | s05-s07, s10-s11 | 待办清单、子 Agent、技能加载、系统提示词、错误恢复 |
| **记忆管理** | s08-s09 | 上下文压缩、持久化记忆 |
| **并发与调度** | s13-s14 | 后台任务（线程池）、Cron 定时器 |
| **多 Agent 平台** | s12, s15-s20 | 任务系统、团队协作、协议、自治认领、工作树隔离、MCP、综合 |

所有章节共享同一个不可变的核心循环（来自 `s01_agent_loop/demo_code.py`）：

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

后续章节只在循环之上叠加机制，循环本身始终保持不变。

---

## 架构全景总结

完成 20 章后，你得到的不是一个"大杂烩"，而是一个**层次分明的 Agent 运行平台**。以下是各层级的全景视图：

### 第一层：核心循环（s01）
```
messages → model.generate() → stop_reason?
  ├── "tool_use" → dispatch → tool_result → messages ↩
  └── "end_turn" → 返回结果
```
整个系统的最内层，20 行代码，**所有后续机制都是对这层的外挂**。

### 第二层：工具与安全（s02-s04）
```
Tool Dispatch → Permission Gate → PreToolUse Hook → 执行 → PostToolUse Hook
```
- **s02**：工具注册表 + 并发的 dispatch map
- **s03**：权限规则引擎，白名单/黑名单/审批流程
- **s04**：Pre/Post 钩子，可扩展的拦截点

### 第三层：规划与控制（s05-s07, s10-s11）
```
用户请求 → Todo 分解 → 子 Agent 委派 → 技能注入 → 系统提示词组装 → 执行
```
- **s05**：TodoWrite 机制，计划-执行分离
- **s06**：子 Agent，干净 `messages[]` 上下文隔离
- **s07**：技能加载，按需注入领域知识
- **s10**：运行时组装系统提示词
- **s11**：分级重试、降级模型、token 预算保护

### 第四层：记忆系统（s08-s09）
```
对话进行中 → snipCompact/microCompact 压缩 → 持久化记忆提取 → 跨会话恢复
```
- **s08**：多种上下文压缩策略（裁剪、摘要、自动触发）
- **s09**：记忆的筛选 → 提取 → 固化，跨会话持久化

### 第五层：并发与调度（s13-s14）
```
后台任务 → 线程池执行 → 通知队列 → 会话内 Cron 定时器
```
- **s13**：`concurrent.futures` 线程池，异步通知通道
- **s14**：类 Cron 调度器，支持持久化 + 会话级触发器

### 第六层：多 Agent 平台（s12, s15-s20）
```
┌──────────────────────────────────────────────────┐
│  Lead Agent                                       │
│  ├── Task 系统 (s12) — 任务状态、依赖、持久化      │
│  ├── Team 通信 (s15) — MessageBus、收件箱           │
│  ├── 团队协议 (s16) — 关闭握手机制、计划审批        │
│  ├── 自治认领 (s17) — 空闲轮询、自动接任务          │
│  ├── 工作树隔离 (s18) — 每个任务的独立 git worktree  │
│  ├── MCP 集成 (s19) — 多传输协议、工具池聚合        │
│  └── 综合系统 (s20) — 以上全部，一个循环            │
└──────────────────────────────────────────────────┘
```

### 关键设计原则

| 原则 | 说明 |
|------|------|
| **循环不可变** | s01 的 `while True` 循环在所有章节中保持不变 |
| **层层叠加** | 每个机制是独立的"外挂模块"，不侵入核心循环 |
| **独立可运行** | 每个 `demo_code.py` 可以单独 `python` 执行 |
| **模型无关** | Harness 不依赖特定模型，支持 Anthropic / DeepSeek / GLM 等 |
| **叙事驱动** | 中文 README 是主源文档，英文/日文是翻译 |

---

## 架构关键模式

- **`demo_code.py` 是独立可运行的**：每个章节的 `demo_code.py` 可直接 `python demo_code.py` 启动，无需跨章节依赖（但 s20 整合了所有前序机制）。
- **测试使用 `importlib` 动态加载**：`tests/` 中的测试通过 `importlib` 导入各章节代码，并注入 `unittest.mock` 替换 `anthropic`/`dotenv`/`yaml`，使其无需真实 API Key 即可运行。
- **旧版 `agents/` 保持同步**：旧版 12 课文件（`agents/s01_agent_loop.py` 等）应保持与新版 `s01/`-`s20/` 中对应章节的 `demo_code.py` 逻辑一致（见本文档末尾的章节对照表）。
- **README 只维护中文版**：`README.md`（中文）是主源文档，`README.en.md` 和 `README.ja.md` 是自动翻译，修改时只改中文版，其他两语不管。

## 环境变量

详见 `.env.example`，唯一必须配置的是 `ANTHROPIC_API_KEY`。支持 Anthropic 原生 API 和兼容 Anthropic 接口的第三方提供商（DeepSeek、智谱 GLM、MiniMax、Kimi 等）。

## 章节对照表（旧版 ↔ 新版）

| 旧版目录 | 新版目录 | 主题 |
|---------|---------|------|
| agents/s01 + docs/en/01 | s01_agent_loop | Agent 循环 |
| agents/s02 + docs/en/02 | s02_tool_use | 工具使用 |
| agents/s03 + docs/en/03 | s05_todo_write | 待办写入 |
| agents/s04 + docs/en/04 | s06_subagent | 子 Agent |
| agents/s05 + docs/en/05 | s07_skill_loading | 技能加载 |
| agents/s06 + docs/en/06 | s08_context_compact | 上下文压缩 |
| agents/s07 + docs/en/07 | s12_task_system | 任务系统 |
| agents/s08 + docs/en/08 | s13_background_tasks | 后台任务 |
| agents/s09 + docs/en/09 | s15_agent_teams | Agent 团队 |
| agents/s10 + docs/en/10 | s16_team_protocols | 团队协议 |
| agents/s11 + docs/en/11 | s17_autonomous_agents | 自治 Agent |
| agents/s12 + docs/en/12 | s18_worktree_isolation | 工作树隔离 |
| （新增） | s03, s04 | 权限门、钩子系统 |
| （新增） | s09, s10, s11 | 记忆、系统提示词、错误恢复 |
| （新增） | s14, s19, s20 | Cron、MCP、综合 |
