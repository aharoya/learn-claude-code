#!/usr/bin/env python3
"""
s13: 后台任务 — 线程异步执行 + 通知注入。

Run:  python s13_background_tasks/demo_code.py
Need: pip install anthropic python-dotenv + .env with ANTHROPIC_API_KEY

Changes from s12:
  - threading.Thread 后台执行
  - background_tasks dict 生命周期追踪（bg_id, command, status）
  - background_results dict + threading.Lock 线程安全存储
  - should_run_background：模型显式声明 run_in_background 参数 或 启发式判断
  - is_slow_operation：模型未指定时的 fallback 启发式
  - start_background_task：分发到守护线程，返回后台任务 ID
  - collect_background_results：收集已完成的后台结果，返回 <task_notification> XML
  - agent_loop：慢操作 → 后台 + 占位符，注入通知到 messages
  - 通知使用 <task_notification> 格式，不复用 tool_use_id
"""

# ======================================================================
# 执行流程（入口 → 结束）
# ======================================================================
#
#   1. 启动 → 初始化 background_tasks/background_results/background_lock
#
#   2. 主循环等待用户输入
#
#   3. 用户输入 → agent_loop(history, context)
#
#   4. agent_loop 核心循环：
#
#      a. 调用 LLM
#
#      b. stop_reason != "tool_use"？→ 返回
#
#      c. 遍历 tool_use block：
#
#         【s13 新增】should_run_background(block)？
#
#         是（后台执行路径）：
#           i.   start_background_task(block)
#                创建 daemon thread → 线程内 execute_tool(block)
#                → 完成后回填 background_tasks[bg_id]["status"] = "completed"
#                回填 background_results[bg_id] = output
#           ii.  父线程立即返回占位 tool_result：
#                "[Background task bg_0001 started]..."
#                Agent 不等待，继续下一轮
#
#         否（同步执行路径）：
#           i.   execute_tool(block) → 直接执行 → 等待结果
#
#      d. collect_background_results()
#         检查是否有之前派发的后台任务已完成
#         有 → 构造 <task_notification> XML 注入到 user 消息
#
#      e. 结果 + 通知一起追加到 messages → 回到步骤 a
#
#   5. agent_loop 返回 → 打印 LLM 文本 → 回到步骤 2
# ======================================================================

import os, subprocess, json, time, random, threading
from pathlib import Path
from dataclasses import dataclass, asdict

# ---- readline：终端 UTF-8 支持 ----
try:
    import readline
    readline.parse_and_bind('set bind-tty-special-chars off')
except ImportError:
    pass

from anthropic import Anthropic
from dotenv import load_dotenv

# ---- 环境变量 ----
load_dotenv(override=True)
if os.getenv("ANTHROPIC_BASE_URL"):
    os.environ.pop("ANTHROPIC_AUTH_TOKEN", None)

# ---- 全局常量 ----
WORKDIR = Path.cwd()                        # 工作目录
MEMORY_DIR = WORKDIR / ".memory"            # 记忆目录
MEMORY_INDEX = MEMORY_DIR / "MEMORY.md"     # 记忆索引
client = Anthropic(base_url=os.getenv("ANTHROPIC_BASE_URL"))
MODEL = os.environ["MODEL_ID"]


# ═══════════════════════════════════════════════════════════
#  任务系统（s12 引入）
#
#  与 s12 完全一致：Task dataclass + CRUD + 依赖管理。
# ═══════════════════════════════════════════════════════════

TASKS_DIR = WORKDIR / ".tasks"
TASKS_DIR.mkdir(exist_ok=True)


@dataclass
class Task:
    """任务数据类：id/subject/description/status/owner/blockedBy。"""
    id: str
    subject: str
    description: str
    status: str          # pending | in_progress | completed
    owner: str | None
    blockedBy: list[str]


def _task_path(task_id: str) -> Path:
    return TASKS_DIR / f"{task_id}.json"


def create_task(subject: str, description: str = "",
                blockedBy: list[str] | None = None) -> Task:
    task = Task(
        id=f"task_{int(time.time())}_{random.randint(0, 9999):04d}",
        subject=subject, description=description,
        status="pending", owner=None,
        blockedBy=blockedBy or [],
    )
    save_task(task)
    return task


def save_task(task: Task):
    _task_path(task.id).write_text(json.dumps(asdict(task), indent=2))


def load_task(task_id: str) -> Task:
    return Task(**json.loads(_task_path(task_id).read_text()))


def list_tasks() -> list[Task]:
    return [Task(**json.loads(p.read_text()))
            for p in sorted(TASKS_DIR.glob("task_*.json"))]


def get_task(task_id: str) -> str:
    """Return full task details as JSON."""
    task = load_task(task_id)
    return json.dumps(asdict(task), indent=2)


def can_start(task_id: str) -> bool:
    """检查所有 blockedBy 依赖是否已完成。缺失依赖 = 阻塞。"""
    task = load_task(task_id)
    for dep_id in task.blockedBy:
        if not _task_path(dep_id).exists():
            return False
        if load_task(dep_id).status != "completed":
            return False
    return True


def claim_task(task_id: str, owner: str = "agent") -> str:
    task = load_task(task_id)
    if task.status != "pending":
        return f"Task {task_id} is {task.status}, cannot claim"
    if not can_start(task_id):
        deps = [d for d in task.blockedBy
                if not _task_path(d).exists() or load_task(d).status != "completed"]
        return f"Blocked by: {deps}"
    task.owner = owner
    task.status = "in_progress"
    save_task(task)
    print(f"  \033[36m[claim] {task.subject} → in_progress (owner: {owner})\033[0m")
    return f"Claimed {task.id} ({task.subject})"


def complete_task(task_id: str) -> str:
    task = load_task(task_id)
    if task.status != "in_progress":
        return f"Task {task_id} is {task.status}, cannot complete"
    task.status = "completed"
    save_task(task)
    unblocked = [t.subject for t in list_tasks()
                 if t.status == "pending" and t.blockedBy and can_start(t.id)]
    print(f"  \033[32m[complete] {task.subject} ✓\033[0m")
    msg = f"Completed {task.id} ({task.subject})"
    if unblocked:
        msg += f"\nUnblocked: {', '.join(unblocked)}"
        print(f"  \033[33m[unblocked] {', '.join(unblocked)}\033[0m")
    return msg


# ═══════════════════════════════════════════════════════════
#  提示词组装系统（s10 引入）
# ═══════════════════════════════════════════════════════════

PROMPT_SECTIONS = {
    "identity": "You are a coding agent. Act, don't explain.",
    "tools": "Available tools: bash, read_file, write_file, "
             "create_task, list_tasks, get_task, claim_task, complete_task.",
    "workspace": f"Working directory: {WORKDIR}",
    "memory": "Relevant memories are injected below when available.",
}


def assemble_system_prompt(context: dict) -> str:
    sections = [PROMPT_SECTIONS["identity"],
                PROMPT_SECTIONS["tools"],
                PROMPT_SECTIONS["workspace"]]
    memories = context.get("memories", "")
    if memories:
        sections.append(f"Relevant memories:\n{memories}")
    return "\n\n".join(sections)


_last_context_key, _last_prompt = None, None


def get_system_prompt(context: dict) -> str:
    global _last_context_key, _last_prompt
    key = json.dumps(context, sort_keys=True, ensure_ascii=False, default=str)
    if key == _last_context_key and _last_prompt:
        return _last_prompt
    _last_context_key = key
    _last_prompt = assemble_system_prompt(context)
    return _last_prompt


# ═══════════════════════════════════════════════════════════
#  工具实现（3 个标准工具 + 5 个任务工具）
# ═══════════════════════════════════════════════════════════

def safe_path(p: str) -> Path:
    path = (WORKDIR / p).resolve()
    if not path.is_relative_to(WORKDIR):
        raise ValueError(f"Path escapes workspace: {p}")
    return path


def run_bash(command: str, run_in_background: bool = False) -> str:
    """执行 Shell 命令。run_in_background 由 agent_loop 层处理，此函数不感知。"""
    try:
        r = subprocess.run(command, shell=True, cwd=WORKDIR,
                           capture_output=True, text=True, timeout=120)
        out = (r.stdout + r.stderr).strip()
        return out[:50000] if out else "(no output)"
    except subprocess.TimeoutExpired:
        return "Error: Timeout (120s)"


def run_read(path: str, limit: int | None = None) -> str:
    try:
        lines = safe_path(path).read_text().splitlines()
        if limit and limit < len(lines):
            lines = lines[:limit] + [f"... ({len(lines) - limit} more lines)"]
        return "\n".join(lines)
    except Exception as e:
        return f"Error: {e}"


def run_write(path: str, content: str) -> str:
    try:
        fp = safe_path(path)
        fp.parent.mkdir(parents=True, exist_ok=True)
        fp.write_text(content)
        return f"Wrote {len(content)} bytes to {path}"
    except Exception as e:
        return f"Error: {e}"


# 任务工具包装（同 s12）
def run_create_task(subject: str, description: str = "",
                    blockedBy: list[str] | None = None) -> str:
    task = create_task(subject, description, blockedBy)
    deps = f" (blockedBy: {', '.join(blockedBy)})" if blockedBy else ""
    print(f"  \033[34m[create] {task.subject}{deps}\033[0m")
    return f"Created {task.id}: {task.subject}{deps}"


def run_list_tasks() -> str:
    tasks = list_tasks()
    if not tasks:
        return "No tasks. Use create_task to add some."
    lines = []
    for t in tasks:
        icon = {"pending": "○", "in_progress": "●",
                "completed": "✓"}.get(t.status, "?")
        deps = f" (blockedBy: {', '.join(t.blockedBy)})" if t.blockedBy else ""
        owner = f" [{t.owner}]" if t.owner else ""
        lines.append(f"  {icon} {t.id}: {t.subject} "
                     f"[{t.status}]{owner}{deps}")
    return "\n".join(lines)


def run_get_task(task_id: str) -> str:
    try:
        return get_task(task_id)
    except FileNotFoundError:
        return f"Error: Task {task_id} not found"


def run_claim_task(task_id: str) -> str:
    return claim_task(task_id, owner="agent")


def run_complete_task(task_id: str) -> str:
    return complete_task(task_id)


# ═══════════════════════════════════════════════════════════
#  工具定义（TOOLS）—— 发送给 LLM 的 JSON Schema
#
#  s13: bash 新增 run_in_background 参数（可选 bool）。
#  LLM 可以主动声明"这个命令可能需要很久，后台执行"。
#  共 8 个工具。
#
#  每个工具定义包含三个关键字段：name/description/input_schema。
# ═══════════════════════════════════════════════════════════

TOOLS = [
    {"name": "bash", "description": "Run a shell command.",
     "input_schema": {"type": "object",
                      "properties": {
                          "command": {"type": "string"},
                          "run_in_background": {"type": "boolean"}},  # s13 新增
                      "required": ["command"]}},
    {"name": "read_file", "description": "Read file contents.",
     "input_schema": {"type": "object",
                      "properties": {"path": {"type": "string"},
                                     "limit": {"type": "integer"}},
                      "required": ["path"]}},
    {"name": "write_file", "description": "Write content to a file.",
     "input_schema": {"type": "object",
                      "properties": {"path": {"type": "string"},
                                     "content": {"type": "string"}},
                      "required": ["path", "content"]}},
    {"name": "create_task",
     "description": "Create a new task with optional blockedBy dependencies.",
     "input_schema": {"type": "object",
                      "properties": {
                          "subject": {"type": "string"},
                          "description": {"type": "string"},
                          "blockedBy": {"type": "array",
                                        "items": {"type": "string"}}},
                      "required": ["subject"]}},
    {"name": "list_tasks",
     "description": "List all tasks with status, owner, and dependencies.",
     "input_schema": {"type": "object", "properties": {},
                      "required": []}},
    {"name": "get_task",
     "description": "Get full details of a specific task by ID.",
     "input_schema": {"type": "object",
                      "properties": {"task_id": {"type": "string"}},
                      "required": ["task_id"]}},
    {"name": "claim_task",
     "description": "Claim a pending task. Sets owner, changes status to in_progress.",
     "input_schema": {"type": "object",
                      "properties": {"task_id": {"type": "string"}},
                      "required": ["task_id"]}},
    {"name": "complete_task",
     "description": "Complete an in-progress task. Reports unblocked downstream tasks.",
     "input_schema": {"type": "object",
                      "properties": {"task_id": {"type": "string"}},
                      "required": ["task_id"]}},
]

# ═══════════════════════════════════════════════════════════
#  工具分发映射（TOOL_HANDLERS）
# ═══════════════════════════════════════════════════════════

TOOL_HANDLERS = {
    "bash": run_bash, "read_file": run_read, "write_file": run_write,
    "create_task": run_create_task, "list_tasks": run_list_tasks,
    "get_task": run_get_task, "claim_task": run_claim_task,
    "complete_task": run_complete_task,
}


# ═══════════════════════════════════════════════════════════
#  NEW in s13: 后台任务系统
#
#  设计思想：
#    之前章节中所有工具调用都是同步的——Agent 调用 bash，
#    阻塞等待 120 秒直到命令完成才继续。这对 pip install
#    或 npm build 这类长操作是极大的浪费。
#
#    s13 引入后台任务：慢操作丢给守护线程，Agent 立即继续。
#    后台完成后，结果以 <task_notification> XML 注入到
#    下一轮对话的 user 消息中。
#
#  两个触发条件（满足任一即走后台）：
#    1. LLM 显式设置了 run_in_background=true（主动声明）
#    2. 启发式检测到慢操作关键词（is_slow_operation）
#
#  线程安全：background_lock 保护所有共享字典的读写。
# ═══════════════════════════════════════════════════════════

_bg_counter = 0                                          # 后台任务 ID 计数器
background_tasks: dict[str, dict] = {}                   # bg_id → {tool_use_id, command, status}
background_results: dict[str, str] = {}                  # bg_id → 工具输出文本
background_lock = threading.Lock()                       # 保护上述两个字典的互斥锁


def is_slow_operation(tool_name: str, tool_input: dict) -> bool:
    """启发式检测：命令是否可能是慢操作（>30 秒）。

    仅对 bash 工具生效。检查命令中是否包含已知慢操作关键词：
    install, build, test, deploy, compile, docker build,
    pip install, npm install, cargo build, pytest, make...

    参数 tool_name：工具名。
    参数 tool_input：工具参数字典。
    返回：True（可能是慢操作）或 False。
    """
    if tool_name != "bash":
        return False
    cmd = tool_input.get("command", "").lower()
    slow_keywords = ["install", "build", "test", "deploy", "compile",
                     "docker build", "pip install", "npm install",
                     "cargo build", "pytest", "make"]
    return any(kw in cmd for kw in slow_keywords)


def should_run_background(tool_name: str, tool_input: dict) -> bool:
    """判断工具调用是否应走后台执行。

    优先级：
      1. LLM 显式 run_in_background=true → 直接后台
      2. 否则 → 启发式检测（is_slow_operation）

    参数 tool_name：工具名。
    参数 tool_input：工具参数字典。
    返回：True（后台执行）或 False（同步执行）。
    """
    if tool_input.get("run_in_background"):
        return True  # 模型明确要求后台
    return is_slow_operation(tool_name, tool_input)


def execute_tool(block) -> str:
    """执行工具调用 block，返回输出字符串。

    用于后台线程中的工具执行（与 agent_loop 中的 TOOL_HANDLERS 分发逻辑一致）。
    """
    handler = TOOL_HANDLERS.get(block.name)
    if handler:
        return handler(**block.input)
    return f"Unknown tool: {block.name}"


def start_background_task(block) -> str:
    """在守护线程中启动工具执行，立即返回后台任务 ID。

    流程：
      1. 生成唯一 bg_id（bg_0001, bg_0002, ...）
      2. 在 background_lock 保护下注册任务（status="running"）
      3. 创建 daemon thread 执行 worker()
      4. worker 完成后：background_tasks[bg_id]["status"] = "completed"
                       background_results[bg_id] = output
      5. 立即返回 bg_id（不等待线程完成）

    参数 block：tool_use block 对象。
    返回：后台任务 ID 字符串（如 "bg_0001"）。
    """
    global _bg_counter
    _bg_counter += 1
    bg_id = f"bg_{_bg_counter:04d}"
    cmd = block.input.get("command", block.name)

    def worker():
        """后台线程工作函数。"""
        result = execute_tool(block)
        with background_lock:
            background_tasks[bg_id]["status"] = "completed"
            background_results[bg_id] = result

    # 注册任务（加锁）
    with background_lock:
        background_tasks[bg_id] = {
            "tool_use_id": block.id,
            "command": cmd,
            "status": "running",
        }

    # 启动守护线程（daemon=True：主线程退出时自动终止）
    thread = threading.Thread(target=worker, daemon=True)
    thread.start()
    print(f"  \033[33m[background] dispatched {bg_id}: {cmd[:40]}\033[0m")
    return bg_id


def collect_background_results() -> list[str]:
    """收集所有已完成后台任务的结果，返回 <task_notification> 列表。

    遍历 background_tasks，找出 status=="completed" 的任务。
    对每个完成任务：
      1. 从 background_tasks/background_results 中取出（pop）
      2. 构造 <task_notification> XML 字符串
      3. 打印完成日志

    注意：pop 操作确保每个任务只被收集一次。
    调用时机：每轮 LLM 调用后（在 append results 之前）。

    返回：<task_notification> 字符串列表（可能为空）。
    """
    with background_lock:
        ready_ids = [bid for bid, task in background_tasks.items()
                     if task["status"] == "completed"]
    notifications = []
    for bg_id in ready_ids:
        with background_lock:
            task = background_tasks.pop(bg_id)
            output = background_results.pop(bg_id, "")
        summary = output[:200] if len(output) > 200 else output
        notifications.append(
            f"<task_notification>\n"
            f"  <task_id>{bg_id}</task_id>\n"
            f"  <status>completed</status>\n"
            f"  <command>{task['command']}</command>\n"
            f"  <summary>{summary}</summary>\n"
            f"</task_notification>")
        print(f"  \033[32m[background done] {bg_id}: "
              f"{task['command'][:40]} ({len(output)} chars)\033[0m")
    return notifications


# ═══════════════════════════════════════════════════════════
#  上下文评估
# ═══════════════════════════════════════════════════════════

def update_context(context: dict, messages: list) -> dict:
    """Derive context from real state."""
    memories = ""
    if MEMORY_INDEX.exists():
        content = MEMORY_INDEX.read_text().strip()
        if content:
            memories = content
    return {
        "enabled_tools": list(TOOL_HANDLERS.keys()),
        "workspace": str(WORKDIR),
        "memories": memories,
    }


# ═══════════════════════════════════════════════════════════
#  agent_loop — 后台任务分发 + 通知注入
#
#  关键变化（对比 s12）：
#    工具执行不再是一条直线，而是分叉为两条路径：
#
#    同步路径：execute_tool → 立即得到结果 → 追加到 messages
#    后台路径：start_background_task → 立即返回占位符 →
#              下一轮 collect_background_results → 注入通知
#
#  后台路径的关键设计决策：
#    为什么用 <task_notification> 而不是 tool_result？
#    因为 tool_result 需要 tool_use_id 与 LLM 的 tool_use 配对。
#    后台任务完成后，LLM 已经不在等待那个 tool_call，
#    强行配对会产生 context 污染。独立的通知格式更清晰。
# ═══════════════════════════════════════════════════════════

def agent_loop(messages: list, context: dict):
    """Agent 核心循环：支持后台任务分发的 LLM 交互。

    流程：
      1. 调用 LLM
      2. 检查 stop_reason
      3. 遍历 tool_use：
         后台？→ start_background_task → 占位 tool_result
         同步？→ execute_tool → 完整 tool_result
      4. collect_background_results → <task_notification> 列表
      5. 结果 + 通知合并追加到 messages → 回到步骤 1

    参数 messages：消息历史列表。
    参数 context：当前上下文字典。
    """
    system = get_system_prompt(context)
    while True:
        # ─── 调用 LLM ───
        try:
            response = client.messages.create(
                model=MODEL, system=system, messages=messages,
                tools=TOOLS, max_tokens=8000)
        except Exception as e:
            messages.append({"role": "assistant", "content": [
                {"type": "text",
                 "text": f"[Error] {type(e).__name__}: {e}"}]})
            return

        messages.append({"role": "assistant", "content": response.content})
        if response.stop_reason != "tool_use":
            return

        # ─── 工具分发（两条路径） ───
        results = []
        for block in response.content:
            if block.type != "tool_use":
                continue
            print(f"\033[36m> {block.name}\033[0m")

            # 判断：后台执行 or 同步执行？
            if should_run_background(block.name, block.input):
                # ── 后台路径 ──
                # 派发到守护线程，立即返回占位符——Agent 不等待
                bg_id = start_background_task(block)
                results.append({"type": "tool_result",
                                "tool_use_id": block.id,
                                "content": f"[Background task {bg_id} started] "
                                           f"Command: {block.input.get('command', '')}. "
                                           f"Result will be available when complete."})
            else:
                # ── 同步路径 ──
                output = execute_tool(block)
                print(str(output)[:300])
                results.append({"type": "tool_result",
                                "tool_use_id": block.id,
                                "content": output})

        # ─── 通知注入 ───
        # 检查之前派发的后台任务是否完成了，完成的通知以 <task_notification>
        # XML 混入 user 消息。LLM 下一轮会看到这些通知并可以据此采取行动。
        user_content = list(results)
        bg_notifications = collect_background_results()
        if bg_notifications:
            for notif in bg_notifications:
                user_content.append({"type": "text", "text": notif})
            print(f"  \033[32m[inject] {len(bg_notifications)} background "
                  f"notification(s)\033[0m")
        messages.append({"role": "user", "content": user_content})
        context = update_context(context, messages)
        system = get_system_prompt(context)


# ═══════════════════════════════════════════════════════════
#  主入口
# ═══════════════════════════════════════════════════════════
if __name__ == "__main__":
    print("s13: background tasks")
    print("Enter a question, press Enter to send. Type q to quit.\n")
    history = []
    context = update_context({}, [])
    while True:
        try:
            query = input("\033[36ms13 >> \033[0m")
        except (EOFError, KeyboardInterrupt):
            break
        if query.strip().lower() in ("q", "exit", ""):
            break
        history.append({"role": "user", "content": query})
        agent_loop(history, context)
        context = update_context(context, history)
        for block in history[-1]["content"]:
            if getattr(block, "type", None) == "text":
                print(block.text)
            elif isinstance(block, dict) and block.get("type") == "text":
                print(block.get("text", ""))
        print()
