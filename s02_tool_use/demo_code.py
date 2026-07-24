#!/usr/bin/env python3
"""
s02: 工具分发 — 在 s01 基础上新增 4 个工具 + 分发映射。

运行: python s02_tool_use/demo_code.py
需要: pip install anthropic python-dotenv + .env 中配置 ANTHROPIC_API_KEY

本文件 = s01 的全部代码 + 以下新增:
  + run_read / run_write / run_edit / run_glob 四个工具实现
  + TOOL_HANDLERS 分发映射（替代 s01 中硬编码的 run_bash 调用）
  + safe_path 路径安全校验

循环本身（agent_loop）与 s01 完全一致，只改了工具执行那一行。
"""

# ======================================================================
# 执行流程（入口 → 结束）
# ======================================================================
#
#   1. 启动程序 → if __name__ == "__main__" 入口
#
#   2. 加载环境变量（load_dotenv + Anthropic 客户端初始化）
#      配置常量：WORKDIR、client、MODEL、SYSTEM、TOOLS、TOOL_HANDLERS
#
#   3. 主循环等待用户输入（while True → input("s02 >> ")）
#
#   4. 用户输入 → 追加到 messages → 进入 agent_loop(history)
#
#   5. agent_loop 核心循环：
#
#      a. 调用 LLM（client.messages.create）
#         LLM 看到的 TOOLS 包含 5 个工具（bash/read/write/edit/glob），
#         会根据用户意图自主选择调用哪个
#
#      b. 追加 assistant 消息到 messages
#
#      c. stop_reason != "tool_use"？→ 返回
#
#      d. stop_reason == "tool_use" → 遍历 response.content：
#         i.   找到 tool_use block → 打印工具名
#         ii.  查 TOOL_HANDLERS 映射表，取出对应函数
#              (s02 关键改动：不再硬编码 run_bash，改为动态查表)
#         iii. 带参数调用 handler(**block.input)
#         iv.  打印输出前 200 字符
#         v.   构造 tool_result 对象
#
#      e. 结果追加到 messages → 回到步骤 a
#
#   6. agent_loop 返回 → 打印 LLM 最终文本 → 回到步骤 3
#
#   7. 用户输入 q/exit/空行 → 程序退出
# ======================================================================

import os, subprocess
from pathlib import Path

# ---- readline：让终端输入支持 UTF-8 和特殊字符（仅 Unix） ----
try:
    import readline
    readline.parse_and_bind('set bind-tty-special-chars off')
    readline.parse_and_bind('set input-meta on')
    readline.parse_and_bind('set output-meta on')
    readline.parse_and_bind('set convert-meta off')
except ImportError:
    pass

from anthropic import Anthropic
from dotenv import load_dotenv

# ---- 环境变量：加载 .env 文件，配置 API 端点和模型 ----
load_dotenv(override=True)
if os.getenv("ANTHROPIC_BASE_URL"):
    os.environ.pop("ANTHROPIC_AUTH_TOKEN", None)

# ---- 全局常量 ----
WORKDIR = Path.cwd()                                          # 工作目录（安全沙箱根目录）
client = Anthropic(base_url=os.getenv("ANTHROPIC_BASE_URL"))  # Anthropic 客户端
MODEL = os.environ["MODEL_ID"]                                # 模型 ID
SYSTEM = f"You are a coding agent at {WORKDIR}. Use tools to solve tasks. Act, don't explain."  # 系统提示词


# ═══════════════════════════════════════════════════════════
#  FROM s01 (unchanged): bash 工具
# ═══════════════════════════════════════════════════════════

def run_bash(command: str) -> str:
    """执行 Shell 命令并返回 stdout/stderr。

    参数 command：要执行的 shell 命令字符串。
    返回：命令输出，最长 50000 字符；超时 120 秒。
    内置简单黑名单（s03 会升级为正式权限系统）。
    """
    dangerous = ["rm -rf /", "sudo", "shutdown", "reboot", "> /dev/"]
    if any(d in command for d in dangerous):
        return "Error: Dangerous command blocked"
    try:
        r = subprocess.run(command, shell=True, cwd=WORKDIR,
                           capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=120)
        out = (r.stdout + r.stderr).strip()
        return out[:50000] if out else "(no output)"
    except subprocess.TimeoutExpired:
        return "Error: Timeout (120s)"
    except (FileNotFoundError, OSError) as e:
        return f"Error: {e}"


# ═══════════════════════════════════════════════════════════
#  NEW in s02: 4 个文件/搜索工具
# ═══════════════════════════════════════════════════════════

def safe_path(p: str) -> Path:
    """将用户输入的相对路径解析为绝对路径，并确保不逃逸工作目录。

    作用：防止模型通过 "../" 读取或写入工作目录之外的文件。
    这是所有文件操作（read/write/edit）的安全基础。
    返回：合法的 Path 对象。
    异常：如果路径试图逃逸工作目录，抛出 ValueError。
    """
    path = (WORKDIR / p).resolve()
    if not path.is_relative_to(WORKDIR):
        raise ValueError(f"Path escapes workspace: {p}")
    return path


def run_read(path: str, limit: int | None = None) -> str:
    """读取文件内容。

    参数 path：文件路径（相对 WORKDIR）。
    参数 limit：可选，最多读取的行数；超出行数时尾部追加 "... (N more lines)"。
    返回：文件文本内容；文件不存在时返回错误信息。
    """
    try:
        lines = safe_path(path).read_text().splitlines()
        if limit and limit < len(lines):
            lines = lines[:limit] + [f"... ({len(lines) - limit} more lines)"]
        return "\n".join(lines)
    except Exception as e:
        return f"Error: {e}"


def run_write(path: str, content: str) -> str:
    """将内容写入文件（覆盖写入）。

    参数 path：目标文件路径（相对 WORKDIR）。
    参数 content：要写入的文本内容。
    返回：成功时返回写入字节数。
    副作用：自动创建不存在的父目录（mkdir -p 行为）。
    """
    try:
        file_path = safe_path(path)
        file_path.parent.mkdir(parents=True, exist_ok=True)
        file_path.write_text(content)
        return f"Wrote {len(content)} bytes to {path}"
    except Exception as e:
        return f"Error: {e}"


def run_edit(path: str, old_text: str, new_text: str) -> str:
    """在文件中执行精确文本替换（只替换第一次出现）。

    这是比 write 更"精确"的文件修改方式——LLM 指定旧文本→新文本，
    只改一处，避免覆盖整个文件导致意外。

    参数 path：目标文件路径。
    参数 old_text：要被替换的原文（必须精确匹配，包含所有空白字符）。
    参数 new_text：替换后的新文本。
    返回：成功时返回 "Edited {path}"；原文未找到时返回错误。
    """
    try:
        file_path = safe_path(path)
        text = file_path.read_text()
        if old_text not in text:
            return f"Error: text not found in {path}"
        file_path.write_text(text.replace(old_text, new_text, 1))
        return f"Edited {path}"
    except Exception as e:
        return f"Error: {e}"


def run_glob(pattern: str) -> str:
    """按 glob 模式匹配文件列表。

    参数 pattern：glob 模式（如 "*.py"、"s*/*.md"、"**/*.ts"）。
    返回：匹配到的文件路径（每行一个），无匹配时返回 "(no matches)"。
    所有结果均确保在工作目录内。
    """
    import glob as g
    try:
        results = []
        for match in g.glob(pattern, root_dir=WORKDIR):
            if (WORKDIR / match).resolve().is_relative_to(WORKDIR):
                results.append(match)
        return "\n".join(results) if results else "(no matches)"
    except Exception as e:
        return f"Error: {e}"


# ═══════════════════════════════════════════════════════════
#  NEW in s02: 工具定义（s01 只有一个 bash，现在扩展到 5 个）
#
#  每个工具定义是一个 JSON Schema 对象，包含：
#    - name：工具名称（LLM 会在 tool_use block 中返回此名称）
#    - description：工具用途（帮助 LLM 判断何时调用）
#    - input_schema：参数 schema（定义类型和必填项）
# ═══════════════════════════════════════════════════════════

TOOLS = [
    {"name": "bash", "description": "Run a shell command.",
     "input_schema": {"type": "object", "properties": {"command": {"type": "string"}}, "required": ["command"]}},
    {"name": "read_file", "description": "Read file contents.",
     "input_schema": {"type": "object", "properties": {"path": {"type": "string"}, "limit": {"type": "integer"}}, "required": ["path"]}},
    {"name": "write_file", "description": "Write content to a file.",
     "input_schema": {"type": "object", "properties": {"path": {"type": "string"}, "content": {"type": "string"}}, "required": ["path", "content"]}},
    {"name": "edit_file", "description": "Replace exact text in a file once.",
     "input_schema": {"type": "object", "properties": {"path": {"type": "string"}, "old_text": {"type": "string"}, "new_text": {"type": "string"}}, "required": ["path", "old_text", "new_text"]}},
    {"name": "glob", "description": "Find files matching a glob pattern.",
     "input_schema": {"type": "object", "properties": {"pattern": {"type": "string"}}, "required": ["pattern"]}},
]

# ═══════════════════════════════════════════════════════════
#  NEW in s02: 工具分发映射
#
#  s01 只有一个工具（bash），直接硬编码：
#     output = run_bash(block.input["command"])
#
#  s02 有 5 个工具，需要根据 LLM 返回的 block.name 动态分发：
#     handler = TOOL_HANDLERS[block.name]
#     output = handler(**block.input)
#
#  映射表 = 工具名 → Python 函数
#  这是一个简单的策略模式：新增工具只需加函数 + 注册到映射表。
# ═══════════════════════════════════════════════════════════

TOOL_HANDLERS = {
    "bash": run_bash, "read_file": run_read, "write_file": run_write,
    "edit_file": run_edit, "glob": run_glob,
}


# ═══════════════════════════════════════════════════════════
#  agent_loop — 与 s01 结构完全一致
#
#  s01: output = run_bash(block.input["command"])        直接调用
#  s02: output = TOOL_HANDLERS[block.name](**block.input) 查表分发
#
#  核心循环（while True → LLM → 工具执行 → 结果回写）不变。
# ═══════════════════════════════════════════════════════════

def agent_loop(messages: list):
    """Agent 核心循环：与 LLM 交互直到任务完成。

    流程：
      1. 调用 LLM（client.messages.create），传入 5 个工具定义
      2. 追加 assistant 消息到 messages
      3. 检查 stop_reason：
         - != "tool_use" → 返回（LLM 任务完成）
         - == "tool_use" → 遍历 tool_use block
      4. 对每个 block：查 TOOL_HANDLERS 获取处理函数 → 执行 → 收集结果
      5. 结果追加到 messages → 回到步骤 1

    参数 messages：消息历史列表（对话上下文）。
    """
    while True:
        # --- 步骤 1：调用 LLM ---
        response = client.messages.create(
            model=MODEL, system=SYSTEM, messages=messages,
            tools=TOOLS, max_tokens=8000,
        )
        messages.append({"role": "assistant", "content": response.content})

        # --- 步骤 2：判断 LLM 是否完成了任务 ---
        if response.stop_reason != "tool_use":
            return  # 不是工具调用 → 任务完成，退出循环

        # --- 步骤 3：遍历所有 tool_use block，逐个执行 ---
        results = []
        for block in response.content:
            if block.type == "tool_use":
                # 打印工具名（黄色），方便观察 Agent 行为
                print(f"\033[33m> {block.name}\033[0m")

                # s02 关键改动：查表分发 (strategy pattern)
                # block.name → 函数, block.input → 参数
                handler = TOOL_HANDLERS.get(block.name)
                output = handler(**block.input) if handler else f"Unknown: {block.name}"

                # 打印输出前 200 字符（避免刷屏）
                print(str(output)[:200])

                # 构造 tool_result，tool_use_id 供 LLM 关联请求与结果
                results.append({"type": "tool_result", "tool_use_id": block.id, "content": output})

        # --- 步骤 4：工具结果追加到 messages，继续循环 ---
        messages.append({"role": "user", "content": results})


# ═══════════════════════════════════════════════════════════
#  主入口
# ═══════════════════════════════════════════════════════════
if __name__ == "__main__":
    print("s02: Tool Use — 在 s01 基础上加了 4 个工具")
    print("输入问题，回车发送。输入 q 退出。\n")

    history = []  # 对话历史，跨轮次复用
    while True:
        try:
            query = input("\033[36ms02 >> \033[0m")
        except (EOFError, KeyboardInterrupt):
            break
        if query.strip().lower() in ("q", "exit", ""):
            break

        # 追加用户消息 → 进入 agent_loop → 打印 LLM 最终文本
        history.append({"role": "user", "content": query})
        agent_loop(history)
        for block in history[-1]["content"]:
            if getattr(block, "type", None) == "text":
                print(block.text)
        print()
