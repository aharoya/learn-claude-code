#!/usr/bin/env python3
"""
s04: Hooks — move extension logic out of the loop, onto hooks.

  User types query
       │
       ▼
  ┌──────────────────┐
  │ UserPromptSubmit │ ── trigger_hooks() before LLM
  └────────┬─────────┘
           ▼
  ┌────────────┐     ┌─────────────────────────────┐
  │  messages  │────▶│  LLM (stop_reason=tool_use?)│
  └────────────┘     │   No ──▶ Stop hooks ──▶ exit │
                     │   Yes ──▶ tool_use block ──┐ │
                     └────────────────────────────┘ │
                                                    ▼
                                          ┌──────────────────┐
                                          │ trigger_hooks()   │
                                          │  PreToolUse:      │
                                          │   permission_hook │
                                          │   log_hook        │
                                          └───────┬──────────┘
                                                  │ (not blocked)
                                          ┌───────▼──────────┐
                                          │ TOOL_HANDLERS[x]  │
                                          └───────┬──────────┘
                                                  │
                                          ┌───────▼──────────┐
                                          │ trigger_hooks()   │
                                          │  PostToolUse:     │
                                          │   large_output    │
                                          └───────┬──────────┘
                                                  │
                                          results ──▶ back to messages

Changes from s03:
  + HOOKS registry (event -> list of callbacks)
  + register_hook() / trigger_hooks()
  + context_inject_hook (UserPromptSubmit)
  + permission_hook, log_hook (PreToolUse)
  + large_output_hook (PostToolUse)
  + summary_hook (Stop)
  - check_permission() removed from loop body
    (logic moved into permission_hook, triggered via PreToolUse)

Run: python s04_hooks/demo_code.py
Needs: pip install anthropic python-dotenv + ANTHROPIC_API_KEY in .env
"""

# ======================================================================
# 执行流程（入口 → 结束）
# ======================================================================
#
#   1. 启动程序 → if __name__ == "__main__" 入口
#
#   2. 加载环境变量（load_dotenv + Anthropic 客户端初始化）
#      配置常量：WORKDIR, MODEL, SYSTEM, TOOLS, TOOL_HANDLERS
#
#   3. 注册所有 Hook 回调到 HOOKS 字典
#      - UserPromptSubmit: context_inject_hook
#      - PreToolUse: permission_hook, log_hook
#      - PostToolUse: large_output_hook
#      - Stop: summary_hook
#
#   4. 主循环等待用户输入（while True → input("s04 >> ")）
#
#   5. 用户输入 → trigger_hooks("UserPromptSubmit", query)
#      记录工作目录信息
#
#   6. 进入 agent_loop(history)：
#
#      a. 调用 LLM（client.messages.create）
#
#      b. stop_reason != "tool_use"？
#         └─ 是 → trigger_hooks("Stop") → 打印统计 → 返回
#
#      c. stop_reason == "tool_use" → 遍历 response.content：
#
#         i.   trigger_hooks("PreToolUse", block)
#              ├─ permission_hook：检查危险命令 / 越权写入
#              ├─ log_hook：记录工具调用日志
#              └─ 任一 hook 返回非 None → 工具被阻止执行
#
#         ii.  未被阻止 → TOOL_HANDLERS 分发执行
#
#         iii. trigger_hooks("PostToolUse", block, output)
#              └─ large_output_hook：输出 >100KB 时警告
#
#         iv.  结果收集到 results 列表
#
#      d. results 追加到 messages → 回到步骤 a（循环）
#
#   7. LLM 返回最终文本 → 打印输出 → 回到步骤 4（等待下一轮输入）
#
#   8. 用户输入 q/exit/空行 → 程序退出
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
# ANTHROPIC_BASE_URL：兼容第三方 API（DeepSeek/GLM/Kimi 等）
# MODEL_ID：指定模型名称
# ANTHROPIC_API_KEY：API 密钥
load_dotenv(override=True)
if os.getenv("ANTHROPIC_BASE_URL"):
    os.environ.pop("ANTHROPIC_AUTH_TOKEN", None)

# ---- 全局常量 ----
WORKDIR = Path.cwd()                                          # 工作目录（安全沙箱根目录）
client = Anthropic(base_url=os.getenv("ANTHROPIC_BASE_URL"))  # Anthropic 客户端（兼容多 provider）
MODEL = os.environ["MODEL_ID"]                                # 模型 ID（从环境变量读取）
SYSTEM = f"You are a coding agent at {WORKDIR}. Use tools to solve tasks. Act, don't explain."  # 系统提示词


# ═══════════════════════════════════════════════════════════
#  FROM s02-s03 (unchanged): 工具实现
# ═══════════════════════════════════════════════════════════

def safe_path(p: str) -> Path:
    """将用户输入的相对路径解析为绝对路径，并确保不逃逸工作目录。

    作用：防止模型通过 "../" 读取或写入工作目录之外的文件。
    返回：合法的 Path 对象。
    异常：如果路径试图逃逸工作目录，抛出 ValueError。
    """
    path = (WORKDIR / p).resolve()
    if not path.is_relative_to(WORKDIR):
        raise ValueError(f"Path escapes workspace: {p}")
    return path

def run_bash(command: str) -> str:
    """执行 Shell 命令并返回 stdout/stderr。

    参数 command：要执行的 shell 命令字符串。
    返回：命令输出，最长 50000 字符；超时则返回错误提示。
    超时：120 秒。
    """
    try:
        r = subprocess.run(command, shell=True, cwd=WORKDIR,
                           capture_output=True, text=True, timeout=120)
        out = (r.stdout + r.stderr).strip()
        return out[:50000] if out else "(no output)"
    except subprocess.TimeoutExpired:
        return "Error: Timeout (120s)"

def run_read(path: str, limit: int | None = None) -> str:
    """读取文件内容。

    参数 path：文件路径（相对 WORKDIR）。
    参数 limit：可选，最多读取的行数。
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
    返回：成功时返回写入字节数；自动创建不存在的父目录。
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

    参数 path：目标文件路径。
    参数 old_text：要被替换的原文（必须精确匹配）。
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

    参数 pattern：glob 模式（如 "*.py"、"s*/*.md"）。
    返回：匹配到的文件路径（每行一个），无匹配时返回 "(no matches)"。
    确保所有结果都在工作目录内。
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
#  工具定义（TOOLS）—— 发送给 LLM 的 JSON Schema
#
#  这段列表定义了 Agent 有哪些工具可用。
#  它直接传入 client.messages.create(tools=TOOLS)，
#  LLM 根据这些定义判断何时调用哪个工具、传什么参数。
#
#  每个工具定义包含三个关键字段：
#    - name：工具名称，LLM 返回的 tool_use block.name 就是它
#    - description：工具用途说明，帮助 LLM 判断"现在该用这个吗？"
#    - input_schema：参数 JSON Schema，定义类型、属性和必填项
#
#  s04 的工具集合与 s02-s03 一致（5 个工具），新增的是钩子系统。
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
#  工具分发映射（TOOL_HANDLERS）—— 工具名 → 执行函数
#
#  这是 s02 引入的策略模式（Strategy Pattern）：
#  agent_loop 不再硬编码每个工具的执行逻辑，而是
#  通过 LLM 返回的 block.name 查表找到对应的 Python 函数。
#
#  使用方式（在 agent_loop 中）：
#    handler = TOOL_HANDLERS.get(block.name)   # block.name = "bash" → run_bash
#    output = handler(**block.input)            # block.input = {"command": "ls"}
#
#  新增工具只需两步：写函数 + 注册到这个映射表，循环代码零改动。
# ═══════════════════════════════════════════════════════════
TOOL_HANDLERS = {
    "bash": run_bash, "read_file": run_read, "write_file": run_write,
    "edit_file": run_edit, "glob": run_glob,
}


# ═══════════════════════════════════════════════════════════
#  NEW in s04: 钩子系统（Hook System）
#
#  设计思想：s03 的权限检查逻辑硬编码在 agent_loop 内部，
#  每加一个新功能就要改循环体。钩子系统将"扩展点"从循环
#  中抽离出来，变成可注册的回调函数。
#
#  工作流程：
#    1. 定义 HOOKS 字典，按事件类型分组存储回调
#    2. register_hook() 注册回调到指定事件
#    3. trigger_hooks() 在关键节点触发所有已注册的回调
#    4. 任一回调返回非 None → 视为"阻止/拦截"信号
# ═══════════════════════════════════════════════════════════

# ---- 钩子注册表：四种事件类型 ----
# UserPromptSubmit：用户输入提交时触发（在 LLM 之前）
# PreToolUse：工具执行前触发（可用于权限检查、日志记录）
# PostToolUse：工具执行后触发（可用于输出检查、后处理）
# Stop：agent_loop 即将返回时触发（可用于统计汇总）
HOOKS = {"UserPromptSubmit": [], "PreToolUse": [], "PostToolUse": [], "Stop": []}

def register_hook(event: str, callback):
    """将回调函数注册到指定事件。

    参数 event：事件名称（"UserPromptSubmit" | "PreToolUse" | "PostToolUse" | "Stop"）。
    参数 callback：回调函数，签名因事件类型而异。

    一个事件可注册多个回调，按注册顺序依次执行。
    """
    HOOKS[event].append(callback)

def trigger_hooks(event: str, *args):
    """触发指定事件上所有已注册的回调。

    参数 event：事件名称。
    参数 *args：传递给回调函数的参数（不同事件传不同参数）。
    返回：第一个返回非 None 的回调值（作为"拦截"信号）；
          所有回调都返回 None 时返回 None（表示"放行"）。
    """
    for callback in HOOKS[event]:
        result = callback(*args)
        if result is not None:  # 教学简化：任一回调返回非 None 即拦截
            return result
    return None


# ═══════════════════════════════════════════════════════════
#  Hook 回调函数实现
# ═══════════════════════════════════════════════════════════

# ---- 危险命令黑名单（直接拒绝） ----
# 包含这些字符串的 bash 命令会被无提示直接拒绝。
DENY_LIST = ["rm -rf /", "sudo", "shutdown", "reboot", "mkfs", "dd if="]

# ---- 潜在危险命令灰名单（需用户确认） ----
# 包含这些字符串的 bash 命令会弹出交互式确认提示。
DESTRUCTIVE = ["rm ", "> /etc/", "chmod 777"]

def permission_hook(block):
    """PreToolUse 钩子：权限检查（s03 的 check_permission() 逻辑迁移至此）。

    安全检查分两级：
      1. 黑名单匹配 → 直接拒绝（打印红色提示）
      2. 灰名单匹配 → 交互式确认（用户输入 y/yes 才放行）

    文件写入/编辑：额外检查路径是否在工作目录内。

    返回：None 表示放行，字符串表示拒绝（该字符串作为 tool_result 返回给 LLM）。
    """
    if block.name == "bash":
        # 黑名单检查：直接拒绝
        for pattern in DENY_LIST:
            if pattern in block.input.get("command", ""):
                print(f"\n\033[31m⛔ Blocked: '{pattern}'\033[0m")
                return "Permission denied by deny list"
        # 灰名单检查：交互确认
        for kw in DESTRUCTIVE:
            if kw in block.input.get("command", ""):
                print(f"\n\033[33m⚠  Potentially destructive command\033[0m")
                print(f"   Tool: {block.name}({block.input})")
                choice = input("   Allow? [y/N] ").strip().lower()
                if choice not in ("y", "yes"):
                    return "Permission denied by user"
    # 文件操作：工作目录边界检查
    if block.name in ("write_file", "edit_file"):
        path = block.input.get("path", "")
        if not (WORKDIR / path).resolve().is_relative_to(WORKDIR):
            print(f"\n\033[33m⚠  Writing outside workspace\033[0m")
            print(f"   Tool: {block.name}({block.input})")
            choice = input("   Allow? [y/N] ").strip().lower()
            if choice not in ("y", "yes"):
                return "Permission denied by user"
    return None

def log_hook(block):
    """PreToolUse 钩子：记录每次工具调用的日志。

    打印工具名和参数摘要（最多显示前 2 个参数值的前 60 字符）。
    始终返回 None（不拦截任何操作）。
    """
    args_preview = str(list(block.input.values())[:2])[:60]
    print(f"\033[90m[HOOK] {block.name}({args_preview})\033[0m")
    return None

def large_output_hook(block, output):
    """PostToolUse 钩子：检查工具输出是否过大。

    当工具返回超过 100KB 时打印黄色警告（大输出会消耗 LLM token 预算）。
    始终返回 None（不拦截任何操作）。
    """
    if len(str(output)) > 100000:
        print(f"\033[33m[HOOK] ⚠ Large output from {block.name}: {len(str(output))} chars\033[0m")
    return None

# UserPromptSubmit hook: log user input before it reaches the LLM
def context_inject_hook(query: str):
    """UserPromptSubmit 钩子：在用户消息发给 LLM 之前注入上下文信息。

    当前仅打印工作目录信息，可扩展为注入项目结构、当前分支等上下文。
    始终返回 None。
    """
    print(f"\033[90m[HOOK] UserPromptSubmit: working in {WORKDIR}\033[0m")
    return None

# Stop hook: print summary when loop is about to exit
def summary_hook(messages: list):
    """Stop 钩子：在 agent_loop 退出前打印会话统计。

    统计本轮对话中所有工具调用的次数。
    始终返回 None（不影响退出流程）。
    """
    tool_count = sum(1 for m in messages
                     for b in (m.get("content") if isinstance(m.get("content"), list) else [])
                     if isinstance(b, dict) and b.get("type") == "tool_result")
    print(f"\033[90m[HOOK] Stop: session used {tool_count} tool calls\033[0m")
    return None

# ---- 注册所有 Hook ----
# 注册顺序决定执行顺序：PreToolUse 先 permission_hook 再 log_hook
register_hook("UserPromptSubmit", context_inject_hook)
register_hook("PreToolUse", permission_hook)   # 先检查权限
register_hook("PreToolUse", log_hook)          # 再记录日志
register_hook("PostToolUse", large_output_hook)
register_hook("Stop", summary_hook)


# ═══════════════════════════════════════════════════════════
#  agent_loop — 核心循环（与 s03 结构相同，但权限检查改为 Hook 驱动）
#
#  s03 方式：if not check_permission(block): ...  （硬编码在循环内）
#  s04 方式：if trigger_hooks("PreToolUse", block): ...  （Hook 驱动）
#
#  优势：新增扩展功能只需写新 Hook + register_hook()，循环体不需要改动。
# ═══════════════════════════════════════════════════════════

def agent_loop(messages: list):
    """Agent 核心循环：与 LLM 交互直到任务完成。

    流程：
      1. 调用 LLM，传入当前消息历史 + 工具定义
      2. LLM 返回 stop_reason：
         - 不是 "tool_use" → 触发 Stop Hook → 返回
         - 是 "tool_use" → 遍历返回的 tool_use block
      3. 对每个 tool_use block：
         a. 触发 PreToolUse Hook（权限检查 + 日志记录）
         b. 被拦截？→ tool_result = 拦截原因（LLM 会看到并尝试修正）
         c. 未被拦截 → 查 TOOL_HANDLERS 执行，触发 PostToolUse Hook
      4. 所有工具结果收集到 results → 追加到 messages → 回到步骤 1

    参数 messages：消息历史列表（对话上下文）。
    """
    while True:
        # --- 步骤 1：调用 LLM ---
        response = client.messages.create(
            model=MODEL, system=SYSTEM, messages=messages,
            tools=TOOLS, max_tokens=8000,
        )
        messages.append({"role": "assistant", "content": response.content})

        # --- 步骤 2：检查停止原因 ---
        if response.stop_reason != "tool_use":
            # LLM 认为任务已完成 → 触发 Stop Hook → 如果 Hook 返回了内容
            # （如补充提示），则追加到 messages 并继续循环
            force = trigger_hooks("Stop", messages)
            if force:
                messages.append({"role": "user", "content": force})
                continue
            return  # 正常退出，返回调用方

        # --- 步骤 3：处理所有工具调用 ---
        results = []
        for block in response.content:
            if block.type != "tool_use":
                continue  # 跳过纯文本 block（思考内容等）

            # 3a：PreToolUse Hook — 权限检查 + 日志
            #     s04 关键改动：用 hook 替代 s03 的 check_permission()
            blocked = trigger_hooks("PreToolUse", block)
            if blocked:
                # 被拦截：将拦截原因作为 tool_result 返回给 LLM
                # LLM 看到后会尝试换一种方式完成任务
                results.append({"type": "tool_result", "tool_use_id": block.id,
                                "content": str(blocked)})
                continue

            # 3b：执行工具
            handler = TOOL_HANDLERS.get(block.name)
            output = handler(**block.input) if handler else f"Unknown: {block.name}"

            # 3c：PostToolUse Hook — 输出后处理（如大输出警告）
            trigger_hooks("PostToolUse", block, output)

            # 3d：收集结果
            results.append({"type": "tool_result", "tool_use_id": block.id, "content": output})

        # --- 步骤 4：工具结果追加到消息历史，回到步骤 1 ---
        messages.append({"role": "user", "content": results})


# ═══════════════════════════════════════════════════════════
#  主入口
# ═══════════════════════════════════════════════════════════
if __name__ == "__main__":
    print("s04: Hooks — extension logic on hooks, loop stays clean")
    print("Type a question, press Enter. Type q to quit.\n")

    history = []  # 对话历史，跨轮次复用
    while True:
        try:
            query = input("\033[36ms04 >> \033[0m")
        except (EOFError, KeyboardInterrupt):
            break
        if query.strip().lower() in ("q", "exit", ""):
            break
        # 步骤 5：UserPromptSubmit Hook — 用户输入前的上下文注入
        trigger_hooks("UserPromptSubmit", query)
        # 步骤 6：用户消息追加到 history，进入 agent_loop
        history.append({"role": "user", "content": query})
        agent_loop(history)
        # 步骤 7：打印 LLM 最终文本响应
        for block in history[-1]["content"]:
            if getattr(block, "type", None) == "text":
                print(block.text)
        print()
