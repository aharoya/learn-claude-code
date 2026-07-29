#!/usr/bin/env python3
"""
s05: TodoWrite — 在 s04 hook 系统之上添加计划工具 + 提醒机制。

  +---------+      +-------+      +------------------+
  |  User   | ---> |  LLM  | ---> | TOOL_HANDLERS    |
  | prompt  |      |       |      |  bash            |
  +---------+      +---+---+      |  read_file       |
                        ^         |  write_file      |
                        | result  |  edit_file       |
                        +---------+  glob            |
                                      todo_write ← NEW
                                   +------------------+
                                        |
                         in-memory current_todos
                                        |
                        if rounds_since_todo >= 3:
                          inject <reminder>

Changes from s04:
  + todo_write tool + run_todo_write() 实现
  + nag 提醒（连续 3 轮未更新 todo 时自动注入提示）
  + SYSTEM 提示词包含 "plan before execute" 指导
  + rounds_since_todo 计数器
  循环不变：新工具通过 TOOL_HANDLERS 自动分发。

Run: python s05_todo_write/demo_code.py
Needs: pip install anthropic python-dotenv + ANTHROPIC_API_KEY in .env
"""

# ======================================================================
# 执行流程（入口 → 结束）
# ======================================================================
#
#   1. 启动程序 → if __name__ == "__main__" 入口
#
#   2. 加载环境变量，配置常量：
#      WORKDIR、client、MODEL、SYSTEM（含计划指导）
#      CURRENT_TODOS（全局待办列表，内存存储）
#
#   3. 注册所有 Hook 回调到 HOOKS 字典
#
#   4. 主循环等待用户输入（while True → input("s05 >> ")）
#
#   5. 用户输入 → trigger_hooks("UserPromptSubmit", query)
#      → 追加到 messages → 进入 agent_loop(history)
#
#   6. agent_loop 核心循环：
#
#      a. 【s05 新增】nag 提醒检查：
#         rounds_since_todo >= 3？→ 注入 <reminder> 提示 LLM 更新 todo
#
#      b. 调用 LLM
#
#      c. stop_reason != "tool_use"？
#         → trigger_hooks("Stop") → 打印统计 → 返回
#
#      d. rounds_since_todo += 1（每轮 LLM 调用后递增）
#
#      e. 遍历 response.content：
#         i.   trigger_hooks("PreToolUse", block)
#              ├─ permission_hook：检查危险命令
#              └─ log_hook：记录日志
#         ii.  被拦截？→ tool_result = 拦截原因
#         iii. TOOL_HANDLERS 分发执行（todo_write 一样走查表分发）
#         iv.  trigger_hooks("PostToolUse", block, output)
#         v.   【s05 新增】如果 block.name == "todo_write" → rounds_since_todo 归零
#         vi.  结果收集到 results
#
#      f. results 追加到 messages → 回到步骤 a
#
#   7. agent_loop 返回 → 打印 LLM 最终文本 → 回到步骤 4
#
#   8. 用户输入 q/exit/空行 → 程序退出
# ======================================================================

import ast, json, os, subprocess
from pathlib import Path

# ---- readline：让终端输入支持 UTF-8 和特殊字符（仅 Unix） ----
try:
    import readline
    readline.parse_and_bind('set bind-tty-special-chars off')
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

# ---- 全局待办列表：内存存储，跨轮次保持 ----
# 每次 todo_write 调用会全量替换此列表。
# 注意：内存存储，程序退出后丢失（s09 会引入持久化记忆）。
CURRENT_TODOS: list[dict] = []

# ---- 系统提示词（s05 更新：加入计划指导） ----
# s01-s04 的 SYSTEM 只描述了"你是谁"。
# s05 加入了行为指导："多步骤任务请用 todo_write 制定计划并更新状态。
SYSTEM = (
    f"You are a coding agent at {WORKDIR}. "
    "Before starting any multi-step task, use todo_write to plan your steps. "
    "Update status as you go."
)


# ═══════════════════════════════════════════════════════════
#  FROM s02-s04 (unchanged): 工具实现
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
    返回：命令输出，最长 50000 字符；超时 120 秒。
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
    参数 limit：可选，最多读取的行数；超出行数时尾部追加 "... (N more lines)"。
    """
    try:
        lines = safe_path(path).read_text().splitlines()
        if limit and limit < len(lines):
            lines = lines[:limit] + [f"... ({len(lines) - limit} more lines)"]
        return "\n".join(lines)
    except Exception as e:
        return f"Error: {e}"

def run_write(path: str, content: str) -> str:
    """将内容写入文件（覆盖写入，自动创建不存在的父目录）。"""
    try:
        file_path = safe_path(path)
        file_path.parent.mkdir(parents=True, exist_ok=True)
        file_path.write_text(content)
        return f"Wrote {len(content)} bytes to {path}"
    except Exception as e:
        return f"Error: {e}"

def run_edit(path: str, old_text: str, new_text: str) -> str:
    """在文件中执行精确文本替换（只替换第一次出现）。

    参数 old_text：必须精确匹配（含空白字符），原文未找到则返回错误。
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
#  NEW in s05: todo_write 工具 —— 计划，不执行
#
#  核心理念：s01-s04 的 Agent 在收到请求后直接开始执行工具，
#  缺少"先计划再行动"的步骤。todo_write 给 LLM 一个工具来
#  声明意图、分解步骤、跟踪进度。
#
#  这不是真正的任务系统（s12 才是），而是一个轻量级的
#  计划协议——Agent 自己写计划，自己执行，自己更新状态。
# ═══════════════════════════════════════════════════════════

def _normalize_todos(todos):
    """验证并标准化待办列表输入。

    LLM 可能以多种格式传入 todos：
      - Python list[dict]（最常见）
      - JSON 字符串
      - Python 字面量字符串（ast.literal_eval 解析）

    验证每项的必填字段和状态枚举值。

    参数 todos：原始输入（可能是 list、JSON 字符串、或其他类型）。
    返回：(todos_list, None) 成功；(None, error_msg) 失败。
    """
    # 如果是字符串，尝试 JSON → AST 两种解析方式
    if isinstance(todos, str):
        try:
            todos = json.loads(todos)
        except json.JSONDecodeError:
            try:
                todos = ast.literal_eval(todos)
            except (SyntaxError, ValueError):
                return None, "Error: todos must be a list or JSON array string"
    # 必须是列表
    if not isinstance(todos, list):
        return None, "Error: todos must be a list"
    # 逐项验证：每条必须是 dict，包含 content + status，status 值合法
    for i, t in enumerate(todos):
        if not isinstance(t, dict):
            return None, f"Error: todos[{i}] must be an object"
        if "content" not in t or "status" not in t:
            return None, f"Error: todos[{i}] missing 'content' or 'status'"
        if t["status"] not in ("pending", "in_progress", "completed"):
            return None, f"Error: todos[{i}] has invalid status '{t['status']}'"
    return todos, None

def run_todo_write(todos: list) -> str:
    """更新全局待办列表并打印格式化输出。

    每次调用会全量替换 CURRENT_TODOS（不是增量更新）。
    LLM 负责维护完整的任务列表，每次更新都传完整列表。

    打印效果：
      ## Current Tasks
      [ ] 待做的任务          (pending)
      [▸] 正在做的任务         (in_progress, 青色)
      [✓] 已完成的任务         (completed, 绿色)

    参数 todos：待办列表，每项包含 content（文本）和 status（状态）。
    返回：更新成功信息。
    """
    global CURRENT_TODOS
    todos, error = _normalize_todos(todos)
    if error:
        return error
    CURRENT_TODOS = todos
    # 打印带图标的任务列表
    lines = ["\n\033[33m## Current Tasks\033[0m"]
    for t in CURRENT_TODOS:
        icon = {
            "pending": " ",
            "in_progress": "\033[36m▸\033[0m",     # 青色箭头
            "completed": "\033[32m✓\033[0m"       # 绿色勾
        }[t["status"]]
        lines.append(f"  [{icon}] {t['content']}")
    print("\n".join(lines))
    return f"Updated {len(CURRENT_TODOS)} tasks"

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
#  s05 新增 todo_write：LLM 用它来声明待办事项并更新状态。
#  其余 5 个工具（bash/read/write/edit/glob）与之前章节一致。
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
    # ── s05 新增：todo_write ──
    {"name": "todo_write", "description": "Create and manage a task list for your current coding session.",
     "input_schema": {"type": "object", "properties": {"todos": {"type": "array", "items": {"type": "object", "properties": {"content": {"type": "string"}, "status": {"type": "string", "enum": ["pending", "in_progress", "completed"]}}, "required": ["content", "status"]}}}, "required": ["todos"]}},
]

# ═══════════════════════════════════════════════════════════
#  工具分发映射（TOOL_HANDLERS）—— 工具名 → 执行函数
#
#  通过 LLM 返回的 block.name 查表找到对应的 Python 函数。
#  使用方式：handler = TOOL_HANDLERS.get(block.name)
#           output = handler(**block.input)
#
#  todo_write 和其他 5 个工具一样走查表分发——循环代码不需要任何改动。
#  新增工具只需两步：写函数 + 注册到这个映射表。
# ═══════════════════════════════════════════════════════════
TOOL_HANDLERS = {
    "bash": run_bash, "read_file": run_read, "write_file": run_write,
    "edit_file": run_edit, "glob": run_glob, "todo_write": run_todo_write,
}


# ═══════════════════════════════════════════════════════════
#  FROM s04 (unchanged): 钩子系统
# ═══════════════════════════════════════════════════════════

# ---- 钩子注册表：四种事件类型 ----
# UserPromptSubmit：用户输入提交时触发
# PreToolUse：工具执行前触发（权限检查 + 日志）
# PostToolUse：工具执行后触发
# Stop：agent_loop 即将返回时触发
HOOKS = {"UserPromptSubmit": [], "PreToolUse": [], "PostToolUse": [], "Stop": []}

def register_hook(event: str, callback):
    """将回调函数注册到指定事件。

    参数 event：事件名称（"UserPromptSubmit" | "PreToolUse" | "PostToolUse" | "Stop"）。
    参数 callback：回调函数。
    一个事件可注册多个回调，按注册顺序依次执行。
    """
    HOOKS[event].append(callback)

def trigger_hooks(event: str, *args):
    """触发指定事件上所有已注册的回调。

    参数 event：事件名称。
    参数 *args：传递给回调函数的参数。
    返回：第一个返回非 None 的回调值（作为"拦截"信号）；
          所有回调都返回 None 时返回 None（放行）。
    """
    for callback in HOOKS[event]:
        result = callback(*args)
        if result is not None:  # 任一回调返回非 None 即拦截
            return result
    return None

# ---- 危险命令黑名单 ----
DENY_LIST = ["rm -rf /", "sudo", "shutdown", "reboot", "mkfs", "dd if="]

def permission_hook(block):
    """PreToolUse 钩子：黑名单权限检查。

    对 bash 命令扫描 DENY_LIST，命中则直接拒绝。
    返回：None 放行，字符串拒绝。
    """
    if block.name == "bash":
        for p in DENY_LIST:
            if p in block.input.get("command", ""):
                print(f"\n\033[31m⛔ Blocked: '{p}'\033[0m")
                return "Permission denied"
    return None

def log_hook(block):
    """PreToolUse 钩子：记录每次工具调用的日志。
    始终返回 None（不拦截）。
    """
    print(f"\033[90m[HOOK] {block.name}\033[0m")
    return None

def context_inject_hook(query: str):
    """UserPromptSubmit 钩子：在用户消息发给 LLM 之前打印工作目录信息。
    始终返回 None。
    """
    print(f"\033[90m[HOOK] UserPromptSubmit: working in {WORKDIR}\033[0m")
    return None

def summary_hook(messages: list):
    """Stop 钩子：在 agent_loop 退出前统计工具调用次数。
    始终返回 None。
    """
    tool_count = sum(1 for m in messages
                     for b in (m.get("content") if isinstance(m.get("content"), list) else [])
                     if isinstance(b, dict) and b.get("type") == "tool_result")
    print(f"\033[90m[HOOK] Stop: session used {tool_count} tool calls\033[0m")
    return None

# ---- 注册所有 Hook ----
register_hook("UserPromptSubmit", context_inject_hook)
register_hook("PreToolUse", permission_hook)
register_hook("PreToolUse", log_hook)
register_hook("Stop", summary_hook)


# ═══════════════════════════════════════════════════════════
#  agent_loop — 与 s04 相同，+ nag 提醒机制
#
#  s05 新增：
#    rounds_since_todo 计数器：每轮 LLM 调用后 +1，
#    todo_write 调用时归零。
#
#    当计数器 >= 3（模型连续 3 轮没更新 todo），
#    自动注入 <reminder> 消息，督促 LLM 更新计划。
#
#  为什么需要 nag？
#    LLM 可能"忘记"使用 todo_write——聊到一半话题偏离，
#    todo 列表过时。nag 是轻量级的自愈机制。
# ═══════════════════════════════════════════════════════════

def agent_loop(messages: list):
    """Agent 核心循环：在 s04 基础上新增计划提醒。

    流程：
      1. nag 检查：计数器 >= 3？→ 注入提醒 → 计数器归零
      2. 调用 LLM
      3. 检查 stop_reason
      4. 遍历 tool_use block：
         a. PreToolUse Hook（权限 + 日志）
         b. 工具分发执行
         c. PostToolUse Hook
         d. 如果是 todo_write → 计数器归零
      5. 结果追加 → 计数器 +1 → 回到步骤 1

    参数 messages：消息历史列表（对话上下文）。
    """
    rounds_since_todo = 0
    while True:
        # --- 步骤 1：nag 提醒 ---
        # 如果连续 3 轮 LLM 响应没有调用 todo_write，注入提醒
        # 这确保 Agent 在执行长任务时不会"忘记"更新计划
        if rounds_since_todo >= 3 and messages:
            messages.append({"role": "user",
                             "content": "<reminder>Update your todos.</reminder>"})
            rounds_since_todo = 0  # 注入后归零，避免连续轰炸

        # --- 步骤 2：调用 LLM ---
        response = client.messages.create(
            model=MODEL, system=SYSTEM, messages=messages,
            tools=TOOLS, max_tokens=8000,
        )
        messages.append({"role": "assistant", "content": response.content})

        # --- 步骤 3：检查停止原因 ---
        if response.stop_reason != "tool_use":
            force = trigger_hooks("Stop", messages)
            if force:
                messages.append({"role": "user", "content": force})
                continue
            return  # 正常退出

        # --- 步骤 4：计数器递增 ---
        # 每轮 tool_use 响应后 +1，todo_write 调用时归零（见步骤 5d）
        rounds_since_todo += 1
        # --- 步骤 5：处理所有工具调用 ---
        results = []
        for block in response.content:
            if block.type != "tool_use":
                continue

            # 5a：PreToolUse Hook（权限 + 日志）
            blocked = trigger_hooks("PreToolUse", block)
            if blocked:
                results.append({"type": "tool_result", "tool_use_id": block.id,
                                "content": str(blocked)})
                continue

            # 5b：工具分发执行（todo_write 在此和其他工具一样走查表）
            handler = TOOL_HANDLERS.get(block.name)
            output = handler(**block.input) if handler else f"Unknown: {block.name}"

            # 5c：PostToolUse Hook
            trigger_hooks("PostToolUse", block, output)

            # 5d：【s05 新增】todo_write 调用 → 重置 nag 计数器
            # LLM 刚更新了计划，不需要再提醒
            if block.name == "todo_write":
                rounds_since_todo = 0

            # 5e：收集结果
            results.append({"type": "tool_result", "tool_use_id": block.id,
                            "content": output})

        # --- 步骤 6：结果追加到 messages，回到步骤 1 ---
        messages.append({"role": "user", "content": results})


# ═══════════════════════════════════════════════════════════
#  主入口
# ═══════════════════════════════════════════════════════════
if __name__ == "__main__":
    print("s05: TodoWrite — plan before execute, nag if you forget")
    print("Type a question, press Enter. Type q to quit.\n")

    history = []  # 对话历史，跨轮次复用
    while True:
        try:
            query = input("\033[36ms05 >> \033[0m")
        except (EOFError, KeyboardInterrupt):
            break
        if query.strip().lower() in ("q", "exit", ""):
            break
        # 步骤 5：UserPromptSubmit Hook → 追加用户消息 → agent_loop
        trigger_hooks("UserPromptSubmit", query)
        history.append({"role": "user", "content": query})
        agent_loop(history)
        # 步骤 7：打印 LLM 最终文本响应
        for block in history[-1]["content"]:
            if getattr(block, "type", None) == "text":
                print(block.text)
        print()
