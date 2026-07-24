#!/usr/bin/env python3
"""
s06: 子 Agent — 用干净的 messages[] 创建隔离的子任务执行环境。

  Parent Agent                           Subagent
  +------------------+                  +------------------+
  | messages=[...]   |                  | messages=[task]  | <-- 全新，不继承父上下文
  |                  |   dispatch       |                  |
  | tool: task       | ---------------> | own while loop   |
  |   prompt="..."   |                  |   bash/read/...  |
  |                  |   summary only   |   (max 30 turns) |
  | result = "..."   | <--------------- | return last text |
  +------------------+                  +------------------+
        ^                                      |
        |       intermediate results DISCARDED  |
        +--------------------------------------+

  Subagent 工具：bash, read, write, edit, glob（NO task — 防止递归）

Changes from s05:
  + task 工具 + spawn_subagent() 实现
  + 子 Agent 用全新的 messages[]（上下文隔离）
  + 安全限制：每个子 Agent 最多 30 轮
  + extract_text() 辅助函数
  + 子 Agent 不能创建孙 Agent（SUB_TOOLS 中没有 task）
  主循环不变：task 通过 TOOL_HANDLERS 自动分发。

Run: python s06_subagent/demo_code.py
Needs: pip install anthropic python-dotenv + ANTHROPIC_API_KEY in .env
"""

# ======================================================================
# 执行流程（入口 → 结束）
# ======================================================================
#
#   1. 启动程序 → if __name__ == "__main__" 入口
#
#   2. 加载环境变量，配置常量：WORKDIR/client/MODEL/SYSTEM/SUB_SYSTEM/
#      CURRENT_TODOS/TOOLS/TOOL_HANDLERS/SUB_TOOLS/SUB_HANDLERS
#
#   3. 注册所有 Hook 回调到 HOOKS 字典
#
#   4. 主循环等待用户输入（while True → input("s06 >> ")）
#
#   5. 用户输入 → Hook → 追加到 messages → 进入 agent_loop(history)
#
#   6. agent_loop 核心循环（与 s05 结构一致）：
#
#      a. nag 提醒检查（>=3 轮未更新 todo → 注入提醒）
#      b. 调用 LLM（parent agent，能看到 task 工具）
#      c. 检查 stop_reason
#      d. 遍历 tool_use block：
#         i.   PreToolUse Hook → 权限检查 + 日志
#         ii.  被拦截？→ tool_result = 拦截原因
#         iii. TOOL_HANDLERS 分发执行
#
#              【s06 核心新增】如果 LLM 调用了 task 工具：
#                → spawn_subagent(description) 被调用：
#
#                1. 创建全新的 messages = [{"role": "user", "content": description}]
#                   （子 Agent 看不到父 Agent 的对话历史）
#
#                2. 进入子 Agent 自己的 while 循环（最多 30 轮）：
#                   a. 调用 LLM（子 Agent 用 SUB_SYSTEM + SUB_TOOLS）
#                   b. 检查 stop_reason
#                   c. 遍历 tool_use → PreToolUse Hook 也适用于子 Agent
#                   d. SUB_HANDLERS 分发执行（没有 task！）
#                   e. 结果回写 → 回到 a
#
#                3. 子 Agent 循环结束 → extract_text() 提取最终文本
#
#                4. 返回摘要给父 Agent（中间过程全部丢弃）
#
#         iv.  PostToolUse Hook
#         v.   如果是 todo_write → nag 计数器归零
#         vi.  结果收集到 results
#
#      e. results 追加到 messages → 回到步骤 a
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
client = Anthropic(base_url=os.getenv("ANTHROPIC_BASE_URL"))  # Anthropic 客户端（兼容多 provider）
MODEL = os.environ["MODEL_ID"]                                # 模型 ID（从环境变量读取）
CURRENT_TODOS: list[dict] = []                                # 全局待办列表（内存存储，跨轮次保持）

# ---- 系统提示词（s06 更新：加入子 Agent 委托指导） ----
# 告知父 Agent：复杂的子问题用 task 工具交给子 Agent 处理
SYSTEM = (
    f"You are a coding agent at {WORKDIR}. "
    "For complex sub-problems, use the task tool to spawn a subagent."
)

# ---- 子 Agent 系统提示词 ----
# 与父 Agent 不同的两个关键点：
#   1. "Complete the task...return a concise summary" — 强调只返回摘要
#   2. "Do not delegate further" — 禁止递归创建子 Agent
SUB_SYSTEM = (
    f"You are a coding agent at {WORKDIR}. "
    "Complete the task you were given, then return a concise summary. "
    "Do not delegate further."
)


# ═══════════════════════════════════════════════════════════
#  工具实现（6 个 + 1 个子 Agent 工具）
#
#  前 6 个工具（bash/read/write/edit/glob/todo_write）
#  与 s05 完全一致，无需修改。
#  父 Agent 和子 Agent 共享这些函数。
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
    返回：命令输出，最长 50000 字符（超长截断避免撑爆 LLM 上下文）。
    超时：120 秒。
    """
    try:
        # 创建一个 subprocess.run() 对象，并设置编码为 gbk，捕获输出和错误，并设置超时时间为 120 秒。
        # windows运行报错，加上encoding="gbk"后执行ok
        r = subprocess.run(command, shell=True, cwd=WORKDIR, encoding="gbk",
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

def _normalize_todos(todos):
    """验证并标准化待办列表输入（s05 引入）。

    LLM 可能以多种格式传入 todos：Python list[dict]、JSON 字符串、AST 字面量。
    逐项验证 content/status 字段和 status 枚举值。
    返回：(todos_list, None) 成功；(None, error_msg) 失败。
    """
    if isinstance(todos, str):
        try:
            todos = json.loads(todos)
        except json.JSONDecodeError:
            try:
                todos = ast.literal_eval(todos)
            except (SyntaxError, ValueError):
                return None, "Error: todos must be a list or JSON array string"
    if not isinstance(todos, list):
        return None, "Error: todos must be a list"
    for i, t in enumerate(todos):
        if not isinstance(t, dict):
            return None, f"Error: todos[{i}] must be an object"
        if "content" not in t or "status" not in t:
            return None, f"Error: todos[{i}] missing 'content' or 'status'"
        if t["status"] not in ("pending", "in_progress", "completed"):
            return None, f"Error: todos[{i}] has invalid status '{t['status']}'"
    return todos, None

def run_todo_write(todos: list) -> str:
    """更新全局待办列表并打印格式化输出（s05 引入）。

    每次调用全量替换 CURRENT_TODOS。打印带色彩图标的任务列表。
    """
    global CURRENT_TODOS
    todos, error = _normalize_todos(todos)
    if error:
        return error
    CURRENT_TODOS = todos
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
#  这段列表定义父 Agent 有哪些工具可用。
#  父 Agent 的工具集包含 7 个工具：bash/read/write/edit/glob/
#  todo_write/task。其中 task 是本版本新增的——LLM 用它委托
#  子任务给子 Agent。
#
#  每个工具定义包含三个关键字段：
#    - name：工具名称，LLM 返回的 tool_use block.name 就是它
#    - description：工具用途说明，帮助 LLM 判断何时调用
#    - input_schema：参数 JSON Schema，定义类型和必填项
#
#  注意：task 工具通过后续的 TOOLS.append() 添加（见 spawn_subagent 之后），
#  因为它的处理函数 spawn_subagent 定义在后面。
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
    {"name": "todo_write", "description": "Create and manage a task list for your current coding session.",
     "input_schema": {"type": "object", "properties": {"todos": {"type": "array", "items": {"type": "object", "properties": {"content": {"type": "string"}, "status": {"type": "string", "enum": ["pending", "in_progress", "completed"]}}, "required": ["content", "status"]}}}, "required": ["todos"]}},
]

# ═══════════════════════════════════════════════════════════
#  工具分发映射（TOOL_HANDLERS）—— 父 Agent 用
#
#  通过 block.name 查表找到对应的 Python 函数。
#  task 的处理函数 spawn_subagent 在定义之后注册（TOOL_HANDLERS["task"] = spawn_subagent）。
# ═══════════════════════════════════════════════════════════
TOOL_HANDLERS = {
    "bash": run_bash, "read_file": run_read, "write_file": run_write,
    "edit_file": run_edit, "glob": run_glob, "todo_write": run_todo_write,
}


# ═══════════════════════════════════════════════════════════
#  NEW in s06: 子 Agent 系统
#
#  设计思想：
#    复杂任务（如"重构整个模块"）不应在父 Agent 的上下文中
#    一步步执行，因为父 Agent 的 messages 历史已经很长、上下文
#    已经很"脏"。子 Agent 用全新的 messages[] 获得干净的思考空间。
#
#  关键设计决策：
#    1. 上下文隔离：子 Agent 只看到 task 的 description，看不到父对话
#    2. 仅返回摘要：子 Agent 的中间工具调用结果全部丢弃，节省父上下文
#    3. 防止递归：SUB_TOOLS 不含 task，子 Agent 无法再创建孙 Agent
#    4. 安全限制：最多 30 轮，防止子 Agent 无限循环
#    5. 权限共享：子 Agent 的工具调用同样经过 PreToolUse Hook
# ═══════════════════════════════════════════════════════════

# ---- 子 Agent 的工具集 ----
# 比父 Agent 少两个工具：todo_write 和 task。
# 没有 task 是最关键的——这防止了子 Agent 创建孙 Agent（无限递归）。
# 没有 todo_write 是因为子 Agent 的任务单一，不需要分步计划。
SUB_TOOLS = [
    {"name": "bash", "description": "Run a shell command.",
     "input_schema": {"type": "object", "properties": {"command": {"type": "string"}}, "required": ["command"]}},
    {"name": "read_file", "description": "Read file contents.",
     "input_schema": {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]}},
    {"name": "write_file", "description": "Write content to a file.",
     "input_schema": {"type": "object", "properties": {"path": {"type": "string"}, "content": {"type": "string"}}, "required": ["path", "content"]}},
    {"name": "edit_file", "description": "Replace exact text in a file once.",
     "input_schema": {"type": "object", "properties": {"path": {"type": "string"}, "old_text": {"type": "string"}, "new_text": {"type": "string"}}, "required": ["path", "old_text", "new_text"]}},
    {"name": "glob", "description": "Find files matching a glob pattern.",
     "input_schema": {"type": "object", "properties": {"pattern": {"type": "string"}}, "required": ["pattern"]}},
]
# 注意：这里故意没有 "task" 工具 —— 防止子 Agent 递归创建孙 Agent

# ---- 子 Agent 的工具分发映射 ----
# 独立的映射表，函数复用父 Agent 的（run_bash/run_read 等）
SUB_HANDLERS = {
    "bash": run_bash, "read_file": run_read, "write_file": run_write,
    "edit_file": run_edit, "glob": run_glob,
}

def extract_text(content) -> str:
    """从 message content 中提取纯文本。

    LLM 的 response.content 是一个 ContentBlock 对象列表，
    每个 block 有 type 属性（"text" / "tool_use"）。
    此函数从中提取所有 text 类型的 block 并拼接。

    参数 content：response.content（list 或 str）。
    返回：所有 text block 的拼接字符串。
    """
    if not isinstance(content, list):
        return str(content)
    return "\n".join(getattr(b, "text", "") for b in content if getattr(b, "type", None) == "text")

def spawn_subagent(description: str) -> str:
    """创建子 Agent，用全新上下文执行一个子任务。

    这是 s06 的核心新增功能。当父 Agent 遇到"需要深入思考"
    的复杂子问题时，调用 task 工具会触发此函数。

    执行过程：
      1. 创建全新的 messages = [{"role": "user", "content": description}]
         ——子 Agent 不知道父 Agent 之前聊了什么，只看得到任务描述
      2. 在自己的 while 循环中与 LLM 交互（最多 30 轮）
      3. 每轮同样经过 PreToolUse/PostToolUse Hook
      4. 循环结束后，extract_text() 提取最终文本摘要
      5. 返回摘要给父 Agent——中间的工具调用结果全部丢弃

    安全机制：
      - 最多 30 轮（防止无限循环消耗 API 费用）
      - 子 Agent 用 SUB_TOOLS（没有 task）——无法递归创建子 Agent
      - 子 Agent 的工具调用同样经过权限 Hook

    参数 description：父 Agent 给子 Agent 的任务描述。
    返回：子 Agent 的最终文本摘要（不是完整的 messages 历史）。
    """
    print(f"\n\033[35m[Subagent spawned]\033[0m")

    # 核心：全新的 messages 列表，只包含任务描述
    # 子 Agent 看不到父 Agent 的对话历史 = 上下文隔离
    messages = [{"role": "user", "content": description}]
    # 最多 30 轮的安全限制
    for _ in range(30):
        # 子 Agent 用自己的 SUB_SYSTEM + SUB_TOOLS
        response = client.messages.create(
            model=MODEL, system=SUB_SYSTEM,
            messages=messages, tools=SUB_TOOLS, max_tokens=8000,
        )
        messages.append({"role": "assistant", "content": response.content})
        if response.stop_reason != "tool_use":
            break  # 子 Agent 认为任务完成
        # 子 Agent 的工具执行（结构与父 agent_loop 一致）
        results = []
        for block in response.content:
            if block.type == "tool_use":
                # 注意：子 Agent 的工具调用同样经过 Hook（权限共享）
                blocked = trigger_hooks("PreToolUse", block)
                if blocked:
                    results.append({"type": "tool_result", "tool_use_id": block.id,
                                    "content": str(blocked)})
                    continue
                handler = SUB_HANDLERS.get(block.name)
                output = handler(**block.input) if handler else f"Unknown: {block.name}"
                trigger_hooks("PostToolUse", block, output)
                # 打印子 Agent 的工具调用（灰色，带 [sub] 标记）
                print(f"  \033[90m[sub] {block.name}: {str(output)[:100]}\033[0m")
                results.append({"type": "tool_result", "tool_use_id": block.id,
                                "content": output})
        messages.append({"role": "user", "content": results})

    # 提取子 Agent 的最终文本（摘要）
    # 只在出错时回溯找最后一个 assistant 消息
    result = extract_text(messages[-1]["content"])
    if not result:
        # last message is tool_result, look backwards for assistant text
        for msg in reversed(messages):
            if msg["role"] == "assistant":
                result = extract_text(msg["content"])
                if result:
                    break
        if not result:
            result = "Subagent stopped after 30 turns without final answer."
    print(f"\033[35m[Subagent done]\033[0m")
    # 只返回摘要——子 Agent 完整的 messages 历史被丢弃
    # 这保证父 Agent 上下文不会被大量中间结果污染
    return result

# ---- 将 task 工具注册到父 Agent 的工具集 ----
# 为什么用 append 而不是直接写在 TOOLS 列表里？
# 因为 spawn_subagent 定义在 TOOLS 之后，直接引用会 NameError。
# 这种"先定义函数，后注册工具"的模式在后续章节（s07 skill、s19 MCP）会复用。
TOOLS.append({
    "name": "task",
    "description": "Launch a subagent to handle a complex subtask. Returns only the final conclusion.",
    "input_schema": {"type": "object", "properties": {"description": {"type": "string"}}, "required": ["description"]},
})
TOOL_HANDLERS["task"] = spawn_subagent


# ═══════════════════════════════════════════════════════════
#  钩子系统
#
#  四种事件类型：UserPromptSubmit / PreToolUse / PostToolUse / Stop。
#  注册的 Hook 在父 Agent 和子 Agent 的工具执行中都会被触发。
# ═══════════════════════════════════════════════════════════

# ---- 钩子注册表 ----
HOOKS = {"UserPromptSubmit": [], "PreToolUse": [], "PostToolUse": [], "Stop": []}

def register_hook(event: str, callback):
    """将回调函数注册到指定事件。

    参数 event：事件名称（"UserPromptSubmit" | "PreToolUse" | "PostToolUse" | "Stop"）。
    参数 callback：回调函数。一个事件可注册多个回调，按顺序执行。
    """
    HOOKS[event].append(callback)

def trigger_hooks(event: str, *args):
    """触发指定事件上所有已注册的回调。

    参数 event：事件名称。参数 *args：传递给回调的参数（不同事件不同）。
    返回：第一个返回非 None 的回调值（"拦截"信号）；全 None 则放行。
    """
    for callback in HOOKS[event]:
        result = callback(*args)
        if result is not None:
            return result
    return None

# ---- 危险命令黑名单 ----
DENY_LIST = ["rm -rf /", "sudo", "shutdown", "reboot", "mkfs", "dd if="]

def permission_hook(block):
    """PreToolUse 钩子：黑名单权限检查。

    对 bash 命令扫描 DENY_LIST，命中则直接拒绝。
    父 Agent 和子 Agent 的 bash 调用都会经过此钩子。
    """
    if block.name == "bash":
        for p in DENY_LIST:
            if p in block.input.get("command", ""):
                print(f"\n\033[31m⛔ Blocked: '{p}'\033[0m")
                return "Permission denied"
    return None

def log_hook(block):
    """PreToolUse 钩子：记录每次工具调用的日志。始终返回 None。"""
    print(f"\033[90m[HOOK] {block.name}\033[0m")
    return None

def context_inject_hook(query: str):
    """UserPromptSubmit 钩子：打印工作目录信息。始终返回 None。"""
    print(f"\033[90m[HOOK] UserPromptSubmit: working in {WORKDIR}\033[0m")
    return None

def summary_hook(messages: list):
    """Stop 钩子：统计并打印工具调用次数。始终返回 None。"""
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
#  agent_loop — 与 s05 结构一致
#
#  task 工具的处理完全透明——当 LLM 调用 task 时，
#  TOOL_HANDLERS["task"] = spawn_subagent，自动执行。
#  循环代码不需要知道 task 是什么。
# ═══════════════════════════════════════════════════════════

rounds_since_todo = 0  # nag 计数器，跨 agent_loop 调用保持

def agent_loop(messages: list):
    """Agent 核心循环：在 s05 基础上透明支持子 Agent 委托。

    流程：
      1. nag 检查：>= 3 轮未更新 todo → 注入提醒
      2. 调用 LLM（父 Agent，TOOLS 含 task）
      3. 检查 stop_reason
      4. 遍历 tool_use block：
         a. PreToolUse Hook（权限 + 日志）
         b. TOOL_HANDLERS 分发（如果 block.name == "task"→spawn_subagent）
         c. PostToolUse Hook
         d. todo_write 调用了？→ nag 归零
      5. 结果追加 → nag 计数器 +1 → 回到步骤 1

    参数 messages：消息历史列表（对话上下文）。
    """
    global rounds_since_todo
    while True:
        # --- 步骤 1：nag 提醒（s05 引入） ---
        if rounds_since_todo >= 3 and messages:
            messages.append({"role": "user",
                             "content": "<reminder>Update your todos.</reminder>"})
            rounds_since_todo = 0

        # --- 步骤 2：调用 LLM（父 Agent） ---
        response = client.messages.create(
            model=MODEL, system=SYSTEM, messages=messages,
            tools=TOOLS, max_tokens=8000,
        )
        messages.append({"role": "assistant", "content": response.content})

        # --- 步骤 3：LLM 认为任务完成？ ---
        if response.stop_reason != "tool_use":
            force = trigger_hooks("Stop", messages)
            if force:
                messages.append({"role": "user", "content": force})
                continue
            return

        # --- 步骤 4：计数器递增 ---
        rounds_since_todo += 1
        # --- 步骤 5：处理所有工具调用 ---
        results = []
        for block in response.content:
            if block.type != "tool_use":
                continue

            # 5a：PreToolUse Hook
            blocked = trigger_hooks("PreToolUse", block)
            if blocked:
                results.append({"type": "tool_result", "tool_use_id": block.id,
                                "content": str(blocked)})
                continue

            # 5b：工具分发执行
            # 如果 block.name == "task" → spawn_subagent 在此执行
            # 父 Agent 阻塞等待子 Agent 完成，拿到摘要后继续
            handler = TOOL_HANDLERS.get(block.name)
            output = handler(**block.input) if handler else f"Unknown: {block.name}"

            # 5c：PostToolUse Hook
            trigger_hooks("PostToolUse", block, output)

            # 5d：todo_write → nag 归零
            if block.name == "todo_write":
                rounds_since_todo = 0

            results.append({"type": "tool_result", "tool_use_id": block.id,
                            "content": output})

        # --- 步骤 6：结果追加到 messages，回到步骤 1 ---
        messages.append({"role": "user", "content": results})


# ═══════════════════════════════════════════════════════════
#  主入口
# ═══════════════════════════════════════════════════════════
if __name__ == "__main__":
    print("s06: Subagent — spawn sub-agents with fresh context, summary only")
    print("Type a question, press Enter. Type q to quit.\n")

    history = []  # 对话历史，跨轮次复用
    while True:
        try:
            query = input("\033[36ms06 >> \033[0m")
        except (EOFError, KeyboardInterrupt):
            break
        if query.strip().lower() in ("q", "exit", ""):
            break
        # UserPromptSubmit Hook → 追加用户消息 → agent_loop → 打印结果
        trigger_hooks("UserPromptSubmit", query)
        history.append({"role": "user", "content": query})
        agent_loop(history)
        for block in history[-1]["content"]:
            if getattr(block, "type", None) == "text":
                print(block.text)
        print()
