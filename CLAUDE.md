# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## 项目简介

这是一个**从零到一构建 AI Agent Harness（运行环境）的教程项目**。核心理念：Agent 的自主性来自模型训练，而非外部代码；一个 AI Agent 产品 = 模型 + Harness。

项目共有 20 个章节（s01-s20），每个章节在前一章基础上叠加一个新机制，最终在 s20 整合为一个完整的 Agent 系统。新旧两版并存：新版在根目录 `s01/`-`s20/`，旧版在 `agents/`（12 课）+ `docs/`。

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

后续章节只**在循环之上叠加机制**（权限、钩子、子 Agent、记忆、MCP 等），循环本身始终保持不变。

## 架构关键模式

- **`demo_code.py` 是独立可运行的**：每个章节的 `demo_code.py` 可直接 `python demo_code.py` 启动，无需跨章节依赖（但 s20 整合了所有前序机制）。
- **测试使用 `importlib` 动态加载**：`tests/` 中的测试通过 `importlib` 导入各章节代码，并注入 `unittest.mock` 替换 `anthropic`/`dotenv`/`yaml`，使其无需真实 API Key 即可运行。
- **旧版 `agents/` 保持同步**：旧版 12 课文件（`agents/s01_agent_loop.py` 等）应保持与新版 `s01/`-`s20/` 中对应章节的 `demo_code.py` 逻辑一致（见本文档末尾的章节对照表）。
- **三语维护**：每个章节的 `README.md`（中文）是源文档，`README.en.md` 和 `README.ja.md` 是翻译，需保持同步更新。

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
