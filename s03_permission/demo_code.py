#!/usr/bin/env python3
"""
s03_permission.py - 权限门系统

在工具执行前插入三道安全门：

    Gate 1: 黑名单直接拒绝（rm -rf /, sudo, ...）
    Gate 2: 规则匹配（写入工作目录外？破坏性命令？）
    Gate 3: 用户确认（暂停等待用户输入 y/n）

    +-------+    +--------+    +--------+    +--------+    +------+
    | Tool  | -> | Gate 1 | -> | Gate 2 | -> | Gate 3 | -> | Exec |
    | call  |    | deny?  |    | match? |    | allow? |    |      |
    +-------+    +--------+    +--------+    +--------+    +------+
         |            |             |             |
         v            v             v             v
      (normal)     (blocked)    (ask user)   (user says no?)

agent_loop 只加了一行：

    if not check_permission(block):
        continue

基于 s02 构建（多工具分发）。

    python s03_permission/demo_code.py
    Needs: pip install anthropic python-dotenv + ANTHROPIC_API_KEY in .env
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
#   3. 主循环等待用户输入（while True → input("s03 >> ")）
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
#      c. stop_reason != "tool_use"？→ 返回（LLM 认为任务完成）
#
#      d. stop_reason == "tool_use" → 遍历 response.content：
#
#         i.   打印工具名（青色）
#
#         ii.  【s03 核心新增】check_permission(block) 三道权限门：
#              Gate 1  check_deny_list(command) → 命中？→ 拒绝（红色）
#              Gate 2  check_rules(tool_name, args) → 命中？→ 进入 Gate 3
#              Gate 3  ask_user(tool_name, args, reason) → 用户输入 y/yes？
#
#         iii. 被拒绝 → tool_result = 拒绝原因
#              LLM 收到拒绝后会自动尝试更安全的方式
#
#         iv.  通过 → TOOL_HANDLERS 查表分发，执行工具函数
#
#         v.   打印输出前 200 字符，结果收集到 results
#
#      e. results 追加到 messages → 回到步骤 a（循环继续）
#
#   6. agent_loop 返回 → 打印 LLM 最终文本响应 → 回到步骤 3（等待下一轮输入）
#
#   7. 用户输入 q/exit/空行 → 程序退出
# ======================================================================

import os, subprocess
from pathlib import Path

# ---- readline：让终端输入支持 UTF-8 和特殊字符（仅 Unix） ----
# macOS 的 libedit 在处理中文输入时有退格问题，这四行修复它
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
WORKDIR = Path.cwd()                                          # 工作目录（安全沙箱根目录，所有路径操作的边界）
client = Anthropic(base_url=os.getenv("ANTHROPIC_BASE_URL"))  # Anthropic 客户端（兼容多 provider）
MODEL = os.environ["MODEL_ID"]                                # 模型 ID（从环境变量读取）

# 系统提示词：s03 新增 "All destructive operations require user approval"
# 明确告知 LLM 安全边界的存在，让 LLM 对权限检查有预期
SYSTEM = f"You are a coding agent at {WORKDIR}. All destructive operations require user approval."


# ═══════════════════════════════════════════════════════════
#  工具实现（5 个）
#
#  注意：s02 的 run_bash 内部有危险命令黑名单。
#  s03 将其移除，改为由外部的 check_permission() 统一做安全检查。
#  设计原则：安全逻辑不属于工具函数，属于运行环境。
# ═══════════════════════════════════════════════════════════

def safe_path(p: str) -> Path:
    """将用户输入的相对路径解析为绝对路径，并确保不逃逸工作目录。

    这是所有文件操作（read/write/edit）的安全基础。
    每个文件工具在执行前都先调用 safe_path 做路径边界检查。

    参数 p：用户或 LLM 提供的路径字符串（相对 WORKDIR）。
    返回：解析后的绝对 Path 对象（保证在 WORKDIR 内）。
    异常：如果路径试图通过 "../" 等逃逸工作目录，抛出 ValueError。
    """
    path = (WORKDIR / p).resolve()
    if not path.is_relative_to(WORKDIR):
        raise ValueError(f"Path escapes workspace: {p}")
    return path


def run_bash(command: str) -> str:
    """执行 Shell 命令并返回 stdout/stderr。

    注意：s03 的 run_bash 不含安全黑名单——危险命令检测
    已移至 check_permission() 中的 Gate 1（DENY_LIST）。
    这样做的好处：安全策略集中管理，新增危险模式只需改 DENY_LIST。

    参数 command：要执行的 shell 命令字符串。
    返回：命令输出，最长 50000 字符（超长输出截断以避免撑爆 LLM 上下文）。
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
    参数 limit：可选，最多读取的行数。超出行数时尾部追加 "... (N more lines)"，
          提示 LLM 文件还有未读完的内容。
    返回：文件文本内容；文件不存在时返回以 "Error:" 开头的错误信息。
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
    返回：成功时返回 "Wrote N bytes to {path}"。
    副作用：自动创建不存在的父目录（等价于 mkdir -p）。
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

    这是比 write 更"精确"的文件修改方式——LLM 指定要替换的原文，
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
    所有结果均通过 is_relative_to 确保在工作目录内。
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
#  这段列表定义了 Agent 有哪些"手"可以用。
#  它被直接传入 client.messages.create(tools=TOOLS)，
#  LLM 根据这些定义判断何时调用哪个工具、传什么参数。
#
#  每个工具定义包含三个关键字段：
#    - name：工具名称，LLM 返回的 tool_use block.name 就是它
#    - description：工具用途说明，帮助 LLM 判断"现在该用这个吗？"
#    - input_schema：参数 JSON Schema，定义类型、属性和必填项
#
#  s03 的工具集合与 s02 一致（5 个工具），新增的是权限系统。
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
#    output = handler(**block.input)            # block.input = {"command": "ls"} → run_bash(command="ls")
#
#  新增工具只需两步：写函数 + 注册到这个映射表，循环代码零改动。
# ═══════════════════════════════════════════════════════════

TOOL_HANDLERS = {
    "bash": run_bash, "read_file": run_read, "write_file": run_write,
    "edit_file": run_edit, "glob": run_glob,
}


# ═══════════════════════════════════════════════════════════
#  NEW in s03: 三道门权限管道（Three-Gate Permission Pipeline）
#
#  设计思想：
#    安全不是工具函数的责任，而是运行环境的责任。
#    把安全检查从工具代码中抽离到循环层，所有工具统一经过同一道权限门。
#
#  三道门依次执行，任一门拒绝即终止：
#    Gate 1（自动）：黑名单匹配 → 无提示直接拒绝
#    Gate 2（自动）：规则匹配 → 命中则进入 Gate 3
#    Gate 3（交互）：用户确认 → y/yes 放行，其他拒绝
#
#  被拒绝的工具调用会以 tool_result 的形式返回给 LLM，
#  LLM 看到 "Permission denied" 后会尝试换一种安全方式完成任务。
# ═══════════════════════════════════════════════════════════

# ---- Gate 1: 硬黑名单 —— 始终禁止，无提示直接拒绝 ----
# 这些是绝对不允许执行的危险操作。
# 包含这些字符串的任何 bash 命令都会被直接拦截，不给用户确认机会。
# 命中时打印红色 ⛔ 并返回 False。
DENY_LIST = ["rm -rf /", "sudo", "shutdown", "reboot", "mkfs", "dd if=", "> /dev/sda"]

def check_deny_list(command: str) -> str | None:
    """遍历黑名单，检查命令是否命中（Gate 1）。

    参数 command：LLM 要执行的 bash 命令字符串。
    返回：命中时返回错误描述字符串（如 "Blocked: 'sudo' is on the deny list"）；
          未命中返回 None（放行到 Gate 2）。
    """
    for pattern in DENY_LIST:
        if pattern in command:
            return f"Blocked: '{pattern}' is on the deny list"
    return None


# ---- Gate 2: 规则引擎 —— 上下文相关的条件检查 ----
# 与黑名单不同，规则引擎是"有条件"的拦截——只有当特定条件满足时才触发。
# 每条规则是一个 dict，包含三个字段：
#   tools：规则适用哪些工具（列表，可多选，如 ["write_file", "edit_file"]）
#   check：lambda 函数，接收工具参数 dict，返回 True 表示"触发此规则"
#   message：触发时展示给用户的警告文字
# 扩展方式：新增安全策略只需往 PERMISSION_RULES 追加一条 dict，
# 不需要改动 check_permission() 或 agent_loop。
PERMISSION_RULES = [
    # 规则 1：写入/编辑操作时，检查目标路径是否在工作目录内
    {"tools": ["write_file", "edit_file"],
     "check": lambda args: not (WORKDIR / args.get("path", "")).resolve().is_relative_to(WORKDIR),
     "message": "Writing outside workspace"},
    # 规则 2：bash 操作时，检查命令是否包含破坏性关键词
    {"tools": ["bash"],
     "check": lambda args: any(kw in args.get("command", "") for kw in ["rm ", "> /etc/", "chmod 777"]),
     "message": "Potentially destructive command"},
]

def check_rules(tool_name: str, args: dict) -> str | None:
    """遍历所有权限规则，检查当前工具调用是否触发（Gate 2）。

    参数 tool_name：工具名称（"bash" / "write_file" / "edit_file" 等）。
    参数 args：工具参数的字典（即 block.input）。
    返回：触发规则时返回该规则的 message 字符串（作为进入 Gate 3 的原因）；
          未触发任何规则返回 None（直接放行到执行阶段，跳过 Gate 3）。
    """
    for rule in PERMISSION_RULES:
        if tool_name in rule["tools"] and rule["check"](args):
            return rule["message"]
    return None


# ---- Gate 3: 用户交互确认 ----
# 当 Gate 2 触发时，在此暂停执行并等待用户判断。
# 打印黄色 ⚠ 警告 + 工具名称/参数，让用户了解风险后决定。
def ask_user(tool_name: str, args: dict, reason: str) -> str:
    """暂停执行，向用户展示风险并等待确认（Gate 3）。

    参数 tool_name：触发规则的工具名称。
    参数 args：工具参数（展示给用户以判断风险）。
    参数 reason：来自 Gate 2 的触发原因（如 "Potentially destructive command"）。
    返回："allow"（用户输入 y 或 yes）或 "deny"（其他任何输入，默认拒绝）。
    """
    print(f"\n\033[33m⚠  {reason}\033[0m")
    print(f"   Tool: {tool_name}({args})")
    choice = input("   Allow? [y/N] ").strip().lower()
    return "allow" if choice in ("y", "yes") else "deny"


# ---- 权限管道：串联三道门 ----
def check_permission(block) -> bool:
    """对 tool_use block 执行三道门检查，返回是否允许执行。

    这是 agent_loop 和权限系统之间的唯一接口。
    在工具分发前调用，决定"这个工具调用允许执行吗？"

    参数 block：LLM 返回的 tool_use block 对象，包含：
          block.name  — 工具名称（"bash"/"write_file" 等）
          block.input — 工具参数字典（如 {"command": "rm file.txt"}）
          block.id    — 工具调用 ID（用于 tool_result 关联）

    执行顺序：
      1. Gate 1 - check_deny_list：对 bash 做黑名单扫描，命中→直接返回 False
      2. Gate 2 - check_rules：对所有工具做规则匹配，命中→进入 Gate 3
      3. Gate 3 - ask_user：暂停等待用户输入，deny→返回 False

    返回：True = 允许执行，进入 TOOL_HANDLERS 分发；
          False = 拒绝执行，agent_loop 将构造 "Permission denied" 返回给 LLM。

    为什么被拒绝后不直接退出而是返回 tool_result？
      让 LLM 看到拒绝原因后可以自行调整策略——这是"反馈循环"的一环，
      比直接报错退出更符合 Agent 的自主性。
    """
    # Gate 1：黑名单扫描（仅对 bash 生效）
    if block.name == "bash":
        reason = check_deny_list(block.input.get("command", ""))
        if reason:
            print(f"\n\033[31m⛔ {reason}\033[0m")
            return False
    # Gate 2 + Gate 3：规则匹配 → 用户确认
    reason = check_rules(block.name, block.input)
    if reason:
        decision = ask_user(block.name, block.input, reason)
        if decision == "deny":
            return False
    return True


# ═══════════════════════════════════════════════════════════
#  agent_loop — 核心循环
#
#  与 s02 的结构完全一致，只在一个地方加了权限检查：
#
#  s02: handler = TOOL_HANDLERS.get(block.name)
#       output = handler(**block.input)
#
#  s03: if not check_permission(block):       ← 新增这一行
#           ...(返回 "Permission denied")...
#           continue
#       handler = TOOL_HANDLERS.get(block.name)
#       output = handler(**block.input)
#
#  循环本身（while True → LLM → 工具执行 → 结果回写）保持不变。
# ═══════════════════════════════════════════════════════════

def agent_loop(messages: list):
    """Agent 核心循环：与 LLM 交互直到任务完成。

    这是整个教程中最重要的函数——s01 定义它的基本形态，
    s02 加上多工具分发，s03 在分发前插入权限门。
    后续所有章节都在这个循环之上叠加，循环本身始终保持不变。

    流程：
      1. 调用 LLM（client.messages.create），传入 5 个工具定义
      2. 追加 assistant 消息到 messages
      3. 检查 stop_reason：
         - != "tool_use" → LLM 任务完成，退出循环返回
         - == "tool_use" → 进入步骤 4
      4. 遍历每个 tool_use block：
         a. check_permission(block) — s03 新增的三道门
            → 被拒绝：tool_result = "Permission denied."
                       LLM 下轮会看到并尝试修正
            → 放行：进入步骤 b
         b. TOOL_HANDLERS 查表 → 执行工具函数
         c. 打印输出前 200 字符
      5. 所有 tool_result 收集到 results → 追加到 messages → 回到步骤 1

    参数 messages：消息历史列表，格式为 [{"role": ..., "content": ...}, ...]。
    """
    while True:
        # --- 步骤 1：调用 LLM ---
        response = client.messages.create(
            model=MODEL, system=SYSTEM, messages=messages,
            tools=TOOLS, max_tokens=8000,
        )
        # 步骤 2：追加 assistant 消息到对话历史
        messages.append({"role": "assistant", "content": response.content})

        # --- 步骤 3：LLM 是否认为任务完成了？ ---
        if response.stop_reason != "tool_use":
            return  # 不是工具调用 → 任务完成，退出循环

        # --- 步骤 4：LLM 想调用工具，逐个处理 ---
        results = []
        for block in response.content:
            if block.type != "tool_use":
                continue  # 跳过非 tool_use 的 block（如纯文本思考）

            print(f"\033[36m> {block.name}\033[0m")

            # === s03 核心改动：权限门 ===
            # 在工具分发之前，先通过三道门检查
            if not check_permission(block):
                # 被拒绝 → 返回 tool_result 给 LLM
                # LLM 收到后会理解"这条路走不通"，尝试换一种方式
                results.append({"type": "tool_result", "tool_use_id": block.id,
                                "content": "Permission denied."})
                continue

            # --- 步骤 4b：查表分发执行 ---
            # TOOL_HANDLERS 是一个字典，key = 工具名，value = 函数
            handler = TOOL_HANDLERS.get(block.name)
            output = handler(**block.input) if handler else f"Unknown: {block.name}"

            # 打印输出前 200 字符（避免超长输出刷屏）
            print(str(output)[:200])
            # 收集 tool_result：tool_use_id 用于 LLM 关联请求和响应
            results.append({"type": "tool_result", "tool_use_id": block.id, "content": output})

        # --- 步骤 5：工具结果写入 messages，循环回到步骤 1 ---
        # LLM 下一轮会看到这些结果，据此决定下一步
        messages.append({"role": "user", "content": results})


# ═══════════════════════════════════════════════════════════
#  主入口
# ═══════════════════════════════════════════════════════════
if __name__ == "__main__":
    print("s03: Permission")
    print("输入问题，回车发送。输入 q 退出。\n")

    # history：跨轮次共享的对话历史，累积所有上下文
    history = []
    while True:
        try:
            query = input("\033[36ms03 >> \033[0m")
        except (EOFError, KeyboardInterrupt):
            break
        if query.strip().lower() in ("q", "exit", ""):
            break
        # 追加用户消息 → 进入核心循环 → 打印 LLM 最终文本响应
        history.append({"role": "user", "content": query})
        agent_loop(history)
        for block in history[-1]["content"]:
            if getattr(block, "type", None) == "text":
                print(block.text)
        print()
