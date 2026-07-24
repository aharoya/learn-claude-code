#!/usr/bin/env python3
"""
s12: 任务系统 —— 文件持久化的任务图，含 blockedBy 依赖。

Run:  python s12_task_system/demo_code.py
Need: pip install anthropic python-dotenv + .env with ANTHROPIC_API_KEY

Changes from s11:
  - Task dataclass（id, subject, description, status, owner, blockedBy）
  - TASKS_DIR = .tasks/ 用于持久化 JSON 存储
  - create_task / save_task / load_task / list_tasks / get_task CRUD 操作
  - can_start：检查 blockedBy 全部 completed（缺失依赖 = 阻塞）
  - claim_task：设置 owner + pending → in_progress
  - complete_task：设置 completed + 报告解除阻塞的下游任务
  - 5 个新工具：create_task, list_tasks, get_task, claim_task, complete_task

Note: 教学代码保持基础 agent_loop，聚焦任务系统。
s11 的完整错误恢复（RecoveryState、退避、升级、reactive compact、
降级模型）在此省略——真实 CC 中 tasks.ts 和 withRetry 是独立的组合层。
"""

# ======================================================================
# 执行流程（入口 → 结束）
# ======================================================================
#
#   1. 启动 → 初始化 TASKS_DIR + context + SYSTEM
#
#   2. 主循环等待用户输入
#
#   3. 用户输入 → agent_loop(history, context)
#
#   4. agent_loop 核心循环：
#
#      a. 调用 LLM（SYSTEM 含 8 个工具声明）
#
#      b. stop_reason != "tool_use"？→ 返回
#
#      c. 遍历 tool_use → TOOL_HANDLERS 分发
#
#         【s12 新增】5 个任务工具：
#         create_task  → 生成 task_id → 写入 .tasks/task_xxx.json
#         list_tasks   → 读取 .tasks/ 下所有 task_*.json → 格式化输出
#         get_task     → 读取单个任务 JSON → 返回完整详情
#         claim_task   → pending→in_progress + 检查 can_start（依赖检查）
#         complete_task → in_progress→completed + 发现 unblocked 下游任务
#
#      d. 结果回写 → update_context → 回到步骤 a
#
#   5. agent_loop 返回 → 打印 LLM 文本 → 回到步骤 2
# ======================================================================

import os, subprocess, json, time, random
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
MEMORY_INDEX = MEMORY_DIR / "MEMORY.md"     # 记忆索引文件
client = Anthropic(base_url=os.getenv("ANTHROPIC_BASE_URL"))  # Anthropic 客户端
MODEL = os.environ["MODEL_ID"]              # 模型 ID


# ═══════════════════════════════════════════════════════════
#  NEW in s12: 任务系统
#
#  设计思想：
#    之前的 todo_write（s05）只是轻量级计划标注，存在内存中，
#    没有依赖关系、没有持久化、没有状态机。s12 引入正式任务系统。
#
#    核心特性：
#      1. 持久化存储：每个任务一个 .tasks/task_xxx.json 文件
#      2. 依赖图：blockedBy 字段建立任务间依赖 → DAG
#      3. 状态机：pending → in_progress → completed
#      4. 认领机制：owner 字段标记谁在执行
#      5. 级联影响：complete_task 自动发现并报告被解除阻塞的任务
#
#    与 todo_write 的区别：
#      todo_write：轻量标注，全量替换，无依赖，内存存储
#      Task 系统：独立文件，增量更新，有依赖，磁盘持久化
# ═══════════════════════════════════════════════════════════

TASKS_DIR = WORKDIR / ".tasks"     # 任务存储目录
TASKS_DIR.mkdir(exist_ok=True)     # 启动时确保目录存在


@dataclass
class Task:
    """任务数据类，对应 .tasks/ 下的一个 JSON 文件。

    字段：
      id：唯一标识符（格式：task_{timestamp}_{random}）
      subject：任务标题（一行摘要）
      description：任务详细描述
      status：任务状态（pending | in_progress | completed）
      owner：执行者标识（单 Agent 场景为 "agent"，多 Agent 场景为 Agent 名）
      blockedBy：依赖任务 ID 列表（只有当全部 completed 后才能 claim）
    """
    id: str
    subject: str
    description: str
    status: str          # pending | in_progress | completed
    owner: str | None    # Agent 名称（多 Agent 场景）
    blockedBy: list[str] # 依赖的任务 ID 列表


def _task_path(task_id: str) -> Path:
    """获取任务的 JSON 文件路径。

    参数 task_id：任务 ID（如 "task_1234567890_0001"）。
    返回：.tasks/{task_id}.json
    """
    return TASKS_DIR / f"{task_id}.json"


# ── CRUD 操作 ──────────────────────────────────────────────

def create_task(subject: str, description: str = "",
                blockedBy: list[str] | None = None) -> Task:
    """创建新任务并持久化到磁盘。

    参数 subject：任务标题（必填）。
    参数 description：任务描述（可选）。
    参数 blockedBy：依赖的任务 ID 列表（可选），为每个依赖项建立 DAG 边。

    任务 ID 生成：task_{timestamp}_{4位随机数}，保证唯一性。

    返回：创建的 Task 对象。
    """
    task = Task(
        id=f"task_{int(time.time())}_{random.randint(0, 9999):04d}",
        subject=subject,
        description=description,
        status="pending",       # 新任务始终从 pending 开始
        owner=None,             # 未认领
        blockedBy=blockedBy or [],
    )
    save_task(task)
    return task


def save_task(task: Task):
    """将任务对象序列化为 JSON 写入磁盘。

    使用 dataclass.asdict 转换 + json.dumps 序列化。
    文件路径：.tasks/{task.id}.json
    """
    _task_path(task.id).write_text(json.dumps(asdict(task), indent=2))


def load_task(task_id: str) -> Task:
    """从磁盘 JSON 文件加载任务对象。

    参数 task_id：任务 ID。
    返回：反序列化的 Task 对象。
    """
    return Task(**json.loads(_task_path(task_id).read_text()))


def list_tasks() -> list[Task]:
    """列出所有任务（按文件名排序）。

    扫描 .tasks/task_*.json，加载并返回 Task 对象列表。
    返回：Task 对象列表，按 ID（时间戳）排序。
    """
    return [Task(**json.loads(p.read_text()))
            for p in sorted(TASKS_DIR.glob("task_*.json"))]


def get_task(task_id: str) -> str:
    """获取单个任务的完整详情（JSON 格式）。

    参数 task_id：任务 ID。
    返回：格式化的 JSON 字符串。
    """
    task = load_task(task_id)
    return json.dumps(asdict(task), indent=2)


# ── 依赖管理 ──────────────────────────────────────────────

def can_start(task_id: str) -> bool:
    """检查任务的所有 blockedBy 依赖是否都已完成。

    核心规则：
      - 依赖文件不存在 = 阻塞（依赖被删除或无效引用）
      - 依赖状态 != "completed" = 阻塞（还在 pending 或 in_progress）
      - 所有依赖都 completed = 可以开始

    参数 task_id：要检查的任务 ID。
    返回：True（所有依赖已完成）或 False（有依赖未完成）。
    """
    task = load_task(task_id)
    for dep_id in task.blockedBy:
        # 依赖文件不存在 → 视为阻塞
        if not _task_path(dep_id).exists():
            return False
        # 依赖未完成 → 阻塞
        if load_task(dep_id).status != "completed":
            return False
    return True


def claim_task(task_id: str, owner: str = "agent") -> str:
    """认领任务：pending → in_progress，设置 owner。

    前提条件：
      1. 任务必须是 pending 状态
      2. 所有 blockedBy 依赖必须已完成（can_start == True）

    参数 task_id：要认领的任务 ID。
    参数 owner：认领者标识（默认 "agent"）。
    返回：成功消息或错误消息。
    """
    task = load_task(task_id)

    # 状态检查
    if task.status != "pending":
        return f"Task {task_id} is {task.status}, cannot claim"

    # 依赖检查
    if not can_start(task_id):
        deps = [d for d in task.blockedBy
                if not _task_path(d).exists() or load_task(d).status != "completed"]
        return f"Blocked by: {deps}"

    # 更新状态
    task.owner = owner
    task.status = "in_progress"
    save_task(task)

    print(f"  \033[36m[claim] {task.subject} → in_progress (owner: {owner})\033[0m")
    return f"Claimed {task.id} ({task.subject})"


def complete_task(task_id: str) -> str:
    """完成任务：in_progress → completed + 发现被解除阻塞的下游任务。

    副作用：完成后自动扫描所有 pending 任务，找出哪些的任务的
    blockedBy 现在全部满足（这个任务可能是它们的最后一个阻塞依赖）。

    参数 task_id：要完成的任务 ID。
    返回：完成消息 + 被解除阻塞任务列表。
    """
    task = load_task(task_id)

    # 状态检查
    if task.status != "in_progress":
        return f"Task {task_id} is {task.status}, cannot complete"

    # 标记完成
    task.status = "completed"
    save_task(task)

    # 级联发现：扫描被此任务阻塞的 pending 任务
    # 如果某个 pending 任务的所有 blockedBy 现在都满足了 → 它被解除了阻塞
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
    # s12: tools 片段包含任务工具的声明
    "tools": "Available tools: bash, read_file, write_file, "
             "create_task, list_tasks, get_task, claim_task, complete_task.",
    "workspace": f"Working directory: {WORKDIR}",
    "memory": "Relevant memories are injected below when available.",
}

def assemble_system_prompt(context: dict) -> str:
    """根据 context 选择 + 拼接提示词片段。"""
    sections = [PROMPT_SECTIONS["identity"],
                PROMPT_SECTIONS["tools"],
                PROMPT_SECTIONS["workspace"]]
    memories = context.get("memories", "")
    if memories:
        sections.append(f"Relevant memories:\n{memories}")
    return "\n\n".join(sections)

_last_context_key, _last_prompt = None, None

def get_system_prompt(context: dict) -> str:
    """获取 SYSTEM 提示词（带确定性缓存）。"""
    global _last_context_key, _last_prompt
    key = json.dumps(context, sort_keys=True, ensure_ascii=False, default=str)
    if key == _last_context_key and _last_prompt:
        return _last_prompt
    _last_context_key = key
    _last_prompt = assemble_system_prompt(context)
    return _last_prompt


# ═══════════════════════════════════════════════════════════
#  工具实现（3 个标准工具）
# ═══════════════════════════════════════════════════════════

def safe_path(p: str) -> Path:
    """将用户输入的相对路径解析为绝对路径，确保不逃逸工作目录。"""
    path = (WORKDIR / p).resolve()
    if not path.is_relative_to(WORKDIR):
        raise ValueError(f"Path escapes workspace: {p}")
    return path

def run_bash(command: str) -> str:
    """执行 Shell 命令。最长 50000 字符，超时 120 秒。"""
    try:
        r = subprocess.run(command, shell=True, cwd=WORKDIR,
                           capture_output=True, text=True, timeout=120)
        out = (r.stdout + r.stderr).strip()
        return out[:50000] if out else "(no output)"
    except subprocess.TimeoutExpired:
        return "Error: Timeout (120s)"

def run_read(path: str, limit: int | None = None) -> str:
    """读取文件内容。limit 可选，限制行数。"""
    try:
        lines = safe_path(path).read_text().splitlines()
        if limit and limit < len(lines):
            lines = lines[:limit] + [f"... ({len(lines) - limit} more lines)"]
        return "\n".join(lines)
    except Exception as e:
        return f"Error: {e}"

def run_write(path: str, content: str) -> str:
    """写入文件（覆盖写入，自动创建父目录）。"""
    try:
        fp = safe_path(path)
        fp.parent.mkdir(parents=True, exist_ok=True)
        fp.write_text(content)
        return f"Wrote {len(content)} bytes to {path}"
    except Exception as e:
        return f"Error: {e}"


# ── 任务工具包装函数 ──────────────────────────────────────

def run_create_task(subject: str, description: str = "",
                    blockedBy: list[str] | None = None) -> str:
    """工具包装：创建任务 + 打印日志。"""
    task = create_task(subject, description, blockedBy)
    deps = f" (blockedBy: {', '.join(blockedBy)})" if blockedBy else ""
    print(f"  \033[34m[create] {task.subject}{deps}\033[0m")
    return f"Created {task.id}: {task.subject}{deps}"


def run_list_tasks() -> str:
    """工具包装：列出所有任务，带格式化图标。

    输出格式：
      ○ task_xxx: subject [pending]
      ● task_yyy: subject [in_progress] [owner]
      ✓ task_zzz: subject [completed]
    每个任务附加依赖信息。
    """
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
    """工具包装：获取单个任务 JSON 详情。"""
    try:
        return get_task(task_id)
    except FileNotFoundError:
        return f"Error: Task {task_id} not found"


def run_claim_task(task_id: str) -> str:
    """工具包装：认领任务，owner 固定为 "agent"。"""
    return claim_task(task_id, owner="agent")


def run_complete_task(task_id: str) -> str:
    """工具包装：完成任务。"""
    return complete_task(task_id)


# ═══════════════════════════════════════════════════════════
#  工具定义（TOOLS）—— 发送给 LLM 的 JSON Schema
#
#  s12 工具集：3 个基础工具 + 5 个任务工具 = 8 个。
#  每个工具定义包含三个关键字段：
#    - name：工具名称
#    - description：工具用途
#    - input_schema：参数 JSON Schema
# ═══════════════════════════════════════════════════════════

TOOLS = [
    {"name": "bash", "description": "Run a shell command.",
     "input_schema": {"type": "object",
                      "properties": {"command": {"type": "string"}},
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
    # ── s12 新增：5 个任务工具 ──
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
#  工具分发映射（TOOL_HANDLERS）—— 工具名 → 执行函数
# ═══════════════════════════════════════════════════════════

TOOL_HANDLERS = {
    "bash": run_bash, "read_file": run_read, "write_file": run_write,
    "create_task": run_create_task, "list_tasks": run_list_tasks,
    "get_task": run_get_task, "claim_task": run_claim_task,
    "complete_task": run_complete_task,
}


# ═══════════════════════════════════════════════════════════
#  上下文评估（s10 引入）
# ═══════════════════════════════════════════════════════════

def update_context(context: dict, messages: list) -> dict:
    """从真实状态推导上下文字典。

    返回：{enabled_tools, workspace, memories}
    """
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
#  agent_loop — 聚焦任务系统的简化版
#
#  本版本有意省略 s11 的完整错误恢复（RecoveryState/退避/升级），
#  以保持教学聚焦。真实实现中 tasks.ts 和 withRetry 是独立的
#  组合层——任务系统的核心逻辑（创建/认领/完成/依赖检查）与
#  错误恢复逻辑正交无关。
# ═══════════════════════════════════════════════════════════

def agent_loop(messages: list, context: dict):
    """Agent 核心循环：带任务系统工具集的 LLM 交互。

    流程：
      1. get_system_prompt(context) → SYSTEM
      2. 调用 LLM（TOOLS 含 5 个任务工具）
      3. 检查 stop_reason
      4. 遍历 tool_use → TOOL_HANDLERS 分发
      5. 结果回写 → update_context → 回到步骤 1

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
            # 简化的错误处理：构造 error 消息并退出
            messages.append({"role": "assistant", "content": [
                {"type": "text",
                 "text": f"[Error] {type(e).__name__}: {e}"}]})
            return

        messages.append({"role": "assistant", "content": response.content})

        # ─── LLM 任务完成？ ───
        if response.stop_reason != "tool_use":
            return

        # ─── 工具分发 ───
        results = []
        for block in response.content:
            if block.type != "tool_use":
                continue
            print(f"\033[36m> {block.name}\033[0m")
            handler = TOOL_HANDLERS.get(block.name)
            output = handler(**block.input) if handler else f"Unknown: {block.name}"
            print(str(output)[:300])
            results.append({"type": "tool_result",
                            "tool_use_id": block.id, "content": output})
        messages.append({"role": "user", "content": results})

        # ─── 每轮重新评估 context ───
        context = update_context(context, messages)
        system = get_system_prompt(context)


# ═══════════════════════════════════════════════════════════
#  主入口
# ═══════════════════════════════════════════════════════════
if __name__ == "__main__":
    print("s12: task system")
    print("Enter a question, press Enter to send. Type q to quit.\n")

    history = []
    context = update_context({}, [])
    while True:
        try:
            query = input("\033[36ms12 >> \033[0m")
        except (EOFError, KeyboardInterrupt):
            break
        if query.strip().lower() in ("q", "exit", ""):
            break
        history.append({"role": "user", "content": query})
        agent_loop(history, context)
        context = update_context(context, history)
        # 打印 LLM 最终文本（兼容 dict 和 object 两种 block 格式）
        for block in history[-1]["content"]:
            if getattr(block, "type", None) == "text":
                print(block.text)
            elif isinstance(block, dict) and block.get("type") == "text":
                print(block.get("text", ""))
        print()
