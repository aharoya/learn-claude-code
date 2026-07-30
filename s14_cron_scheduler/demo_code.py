#!/usr/bin/env python3
"""
s14: Cron 调度器 — 独立守护线程 + 队列处理器。

Run:  python s14_cron_scheduler/demo_code.py
Need: pip install anthropic python-dotenv + .env with ANTHROPIC_API_KEY

Changes from s13:
  - CronJob dataclass（id, cron, prompt, recurring, durable）
  - cron_matches：5 字段 cron 表达式匹配，DOM/DOW 为 OR 语义
  - schedule_job / cancel_job：注册/删除 cron 任务（含验证）
  - cron_scheduler_loop：独立守护线程，每 1 秒轮询
  - cron_queue：线程安全队列，调度器写入，队列处理器投递
  - queue_processor_loop：cron_queue 有工作时自动触发 agent_loop
  - 持久化存储：.scheduled_tasks.json（重启后恢复）
  - 3 个新工具：schedule_cron, list_crons, cancel_cron

四层架构：
  1. Scheduler：守护线程检查时间 → 触发匹配的任务
  2. Queue：cron_queue 解耦调度器与 agent_loop
  3. Queue processor：Agent 空闲且有队列工作 → 自动唤醒执行
  4. Consumer：agent_loop 消费队列任务，注入到 messages
"""

# ======================================================================
# 执行流程（入口 → 结束）
# ======================================================================
#
#   1. 启动 → 加载持久化 cron 任务 → 启动 cron_scheduler_loop 线程
#            → 启动 queue_processor_loop 线程 → 进入主循环
#
#   2. 主循环等待用户输入（带 agent_lock 互斥）
#
#   3. 用户输入 → 获取 agent_lock → run_agent_turn_locked(query)
#
#   4. agent_loop 核心循环：
#
#      a. consume_cron_queue()
#         消费所有已触发的 cron 任务 → 注入到 messages 开头的 user 消息
#
#      b. 调用 LLM
#
#      c. stop_reason != "tool_use"？→ 返回 context
#
#      d. 遍历 tool_use：
#         后台？→ start_background_task（同 s13）
#         同步？→ execute_tool（含 cron 工具：schedule/list/cancel）
#
#      e. collect_background_results → 注入通知
#
#      f. 结果回写 → 回到步骤 a
#
#   5. 并行线程（始终运行）：
#      cron_scheduler_loop：每 1 秒检查一次，匹配的任务放入 cron_queue
#      queue_processor_loop：每 0.2 秒检查一次，cron_queue 非空 + agent 空闲 → 自动调用 agent_loop
#
#   6. agent_loop 返回 → 打印 LLM 文本 → 释放 agent_lock → 回到步骤 2
# ======================================================================

import os, subprocess, json, time, random, threading
from pathlib import Path
from datetime import datetime
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
#  任务系统（s12 引入）
#
#  Task dataclass + CRUD + 依赖管理。与 s12/s13 一致。
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
    owner: str | None    # 执行者标识
    blockedBy: list[str] # 依赖任务 ID 列表

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
    """返回任务的完整 JSON 详情。"""
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
    """尝试认领任务。返回成功信息或失败原因。"""
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
    """完成任务：in_progress → completed + 发现被解除阻塞的下游任务。"""
    task = load_task(task_id)
    if task.status != "in_progress":
        return f"Task {task_id} is {task.status}, cannot complete"
    task.status = "completed"
    save_task(task)
    # 级联发现：检查哪些 pending 任务现在被解除了阻塞
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
#
#  PROMPT_SECTIONS 片段字典 + assemble_system_prompt + 确定性缓存。
#  s14 的 tools 片段包含 11 个工具（多了 3 个 cron 工具）。
# ═══════════════════════════════════════════════════════════

PROMPT_SECTIONS = {
    "identity": "You are a coding agent. Act, don't explain.",
    "tools": "Available tools: bash, read_file, write_file, "
             "create_task, list_tasks, get_task, claim_task, complete_task, "
             "schedule_cron, list_crons, cancel_cron.",
    "workspace": f"Working directory: {WORKDIR}",
    "memory": "Relevant memories are injected below when available.",
}

def assemble_system_prompt(context: dict) -> str:
    """根据 context 选择 + 拼接提示词片段。仅 memories 条件加载。"""
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
#  工具实现（3 个标准工具 + 5 个任务工具）
# ═══════════════════════════════════════════════════════════

def safe_path(p: str) -> Path:
    """将相对路径解析为绝对路径，确保不逃逸工作目录。"""
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

# ── 任务工具包装函数 ──
def run_create_task(subject: str, description: str = "",
                    blockedBy: list[str] | None = None) -> str:
    """工具包装：创建任务并打印日志。

    调用底层 create_task → 格式化返回给 LLM。
    blockedBy 参数控制依赖关系——LLM 可通过此字段表达"先做 A 再做 B"。
    """
    task = create_task(subject, description, blockedBy)
    deps = f" (blockedBy: {', '.join(blockedBy)})" if blockedBy else ""
    print(f"  \033[34m[create] {task.subject}{deps}\033[0m")
    return f"Created {task.id}: {task.subject}{deps}"

def run_list_tasks() -> str:
    """工具包装：列出所有任务，带状态图标和依赖信息。

    状态图标：○ pending / ● in_progress / ✓ completed
    返回格式化文本给 LLM 查看全局进度。
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
    """工具包装：获取单个任务的完整 JSON 详情。"""
    try:
        return get_task(task_id)
    except FileNotFoundError:
        return f"Error: Task {task_id} not found"

def run_claim_task(task_id: str) -> str:
    """工具包装：认领一个 pending 任务，owner 设为 'agent'。"""
    return claim_task(task_id, owner="agent")

def run_complete_task(task_id: str) -> str:
    """工具包装：完成一个 in_progress 任务，自动检查解除了哪些阻塞。"""
    return complete_task(task_id)


# ═══════════════════════════════════════════════════════════
#  后台任务系统（s13 引入）
#
#  线程异步执行 + 通知注入。与 s13 一致。
# ═══════════════════════════════════════════════════════════

_bg_counter = 0                           # 后台任务 ID 计数器
background_tasks: dict[str, dict] = {}    # bg_id → {tool_use_id, command, status}
background_results: dict[str, str] = {}   # bg_id → 工具输出
background_lock = threading.Lock()        # 保护上述字典的互斥锁

def is_slow_operation(tool_name: str, tool_input: dict) -> bool:
    """启发式检测：命令是否可能是慢操作。检查 install/build/test 等关键词。

    这是 s13 引入的 heuristic 机制（非 CC 源码策略）：
      CC 没有"命令含 test → 后台执行"的硬编码规则，而是由 LLM 自主决定。
      教学版的 heuristic 是为了演示"自动将长任务标记为后台执行"的概念。

    返回 True 意味着 agent_loop 会走后台路径（start_background_task），
    不阻塞 agent_loop 继续处理其他工具。
    """
    if tool_name != "bash":
        return False
    cmd = tool_input.get("command", "").lower()
    slow_keywords = ["install", "build", "test", "deploy", "compile",
                     "docker build", "pip install", "npm install",
                     "cargo build", "pytest", "make"]
    return any(kw in cmd for kw in slow_keywords)

def should_run_background(tool_name: str, tool_input: dict) -> bool:
    """判断是否后台执行：LLM 显式声明 或 启发式检测。

    判定优先级：
      1. LLM 显式设置 run_in_background=True → 后台（LLM 的意图优先）
      2. heuristic 检测到慢命令 → 后台（避免阻塞循环）
      以上都不命中 → 同步执行（默认路径）

    这个函数在 agent_loop 的 tool_use 分发处被调用（950行附近），
    决定每个 tool 走 start_background_task 还是 execute_tool。
    """
    if tool_input.get("run_in_background"):
        return True
    return is_slow_operation(tool_name, tool_input)

def execute_tool(block) -> str:
    """执行工具调用 block，返回输出字符串。

    内联分发映射（非 dict + 列表式 TOOLS 注册）：包含 11 个工具。
    cron 工具（schedule_cron/list_crons/cancel_cron）也在此注册。

    执行流程：
      1. 根据 block.name 查 handler 映射表
      2. 调用 handler(**block.input)——block.input 是 LLM 填的参数 dict
      3. handler 返回的字符串直接作为 tool_result 给 LLM

    特别注意——后台路径 vs 同步路径的分叉点不在 execute_tool 内部：
      execute_tool 只负责"同步执行"；
      "是否后台执行"的判断在 agent_loop 中由 should_run_background 决定。
      execute_tool 在后台线程中调用时（start_background_task → worker），
      内部的 tool_result 不返回给 agent_loop，而是存到 background_results。
    """
    handler = {
        "bash": run_bash, "read_file": run_read, "write_file": run_write,
        "create_task": run_create_task, "list_tasks": run_list_tasks,
        "get_task": run_get_task, "claim_task": run_claim_task,
        "complete_task": run_complete_task,
        "schedule_cron": run_schedule_cron, "list_crons": run_list_crons,
        "cancel_cron": run_cancel_cron,
    }.get(block.name)
    if handler:
        return handler(**block.input)
    return f"Unknown tool: {block.name}"

def start_background_task(block) -> str:
    """在守护线程中启动工具执行，立即返回后台任务 ID。

    数据流（跨线程传递工具结果）：
      ┌─ agent_loop（主线程）────┐
      │  start_background_task    │ → 注册 background_tasks[bg_id]（running）
      │  └→ 返回 bg_id 给 LLM    │
      │  └→ collect_background_results() → 检查 completed
      └───────────────────────────┘

      ┌─ worker（守护线程）──────┐
      │  execute_tool(block)     │ → 同步执行（可能耗时较长）
      │  with lock:              │ → 写 background_results + 改 status
      │    status = "completed"  │
      └───────────────────────────┘

    worker 线程是 daemon=True——主线程退出时 worker 不会阻止进程退出。
    但这也意味着：如果主线程在 worker 完成前退出，结果会丢失。
    """
    global _bg_counter
    _bg_counter += 1
    bg_id = f"bg_{_bg_counter:04d}"
    cmd = block.input.get("command", block.name)

    def worker():
        result = execute_tool(block)
        with background_lock:
            background_tasks[bg_id]["status"] = "completed"
            background_results[bg_id] = result

    with background_lock:
        background_tasks[bg_id] = {
            "tool_use_id": block.id,
            "command": cmd,
            "status": "running",
        }
    threading.Thread(target=worker, daemon=True).start()
    print(f"  \033[33m[background] dispatched {bg_id}: {cmd[:40]}\033[0m")
    return bg_id

def collect_background_results() -> list[str]:
    """收集已完成后台任务的结果，返回 <task_notification> XML 列表。

    每个已完成任务 pop 后只通知一次。
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
#  NEW in s14: Cron 调度器
#
#  设计思想：
#    Agent 不只是响应用户的"被动服务"，还应能按时间表"主动工作"。
#    比如每天 9 点检查 CI 状态、每小时拉取最新数据、30 分钟后提醒自己。
#    Cron 调度器让 Agent 具备时间感知和定时自主触发能力。
#
#  四层架构：
#    1. Scheduler（cron_scheduler_loop）：守护线程，每秒检查时间 → 匹配则放入队列
#    2. Queue（cron_queue）：线程安全队列，解耦调度器和 Agent
#    3. Queue processor（queue_processor_loop）：Agent 空闲 + 队列有工作 → 自动获取锁并执行
#    4. Consumer（agent_loop）：消费队列中的 cron 任务，注入 [Scheduled] 消息
#
#  两层互斥锁：
#    cron_lock：保护 scheduled_jobs/cron_queue/_last_fired
#    agent_lock：保护 agent_loop，防止用户输入和 cron 触发同时执行
#
#  Cron 表达式标准（5 字段）：
#    分钟  小时  日期  月份  星期
#    *     *     *     *     *
#    特殊语法：*/5（每5分钟）、1,3,5（列举）、1-5（范围）
#    DOM 和 DOW 为 OR 语义——满足任一即可（标准 cron 行为）
# ═══════════════════════════════════════════════════════════

DURABLE_PATH = WORKDIR / ".scheduled_tasks.json"  # 持久化 cron 任务存储

@dataclass
class CronJob:
    """Cron 定时任务数据类。

    字段：
      id：唯一标识（cron_{6位随机数}）
      cron：5 字段 cron 表达式（如 "0 9 * * *" = 每天 9:00）
      prompt：触发时注入到 messages 的提示文本
      recurring：True = 重复执行，False = 一次性（触发后自动删除）
      durable：True = 持久化到 .scheduled_tasks.json（重启后恢复）
    """
    id: str
    cron: str        # "0 9 * * *"（分钟 小时 日期 月份 星期）
    prompt: str      # 触发时注入的消息
    recurring: bool  # 重复执行？
    durable: bool    # 持久化到磁盘？

# ── 运行时状态 ──
scheduled_jobs: dict[str, CronJob] = {}  # 所有已注册任务（job_id → CronJob）
cron_queue: list[CronJob] = []            # 已触发等待消费的任务队列
cron_lock = threading.Lock()              # 保护 scheduled_jobs/cron_queue/_last_fired
agent_lock = threading.Lock()             # 保护 agent_loop，防止并发执行
_last_fired: dict[str, str] = {}          # job_id → "YYYY-MM-DD HH:MM"，防止同一分钟重复触发


# ── Cron 表达式匹配 ──────────────────────────────────────

def _cron_field_matches(field: str, value: int) -> bool:
    """单个 cron 字段与值匹配。

    支持语法：
      *       → 通配（永远匹配）
      */N     → 每 N 个单位（如 */5 = 每 5 分钟）
      A,B,C   → 列举（匹配任一值）
      A-B     → 范围（闭区间）
      数字    → 精确匹配

    参数 field：cron 字段字符串。
    参数 value：当前时间对应字段的整数值。
    返回：True（匹配）或 False。
    """
    if field == "*":
        return True
    if field.startswith("*/"):
        step = int(field[2:])
        return step > 0 and value % step == 0
    if "," in field:
        return any(_cron_field_matches(f.strip(), value)
                   for f in field.split(","))
    if "-" in field:
        lo, hi = field.split("-", 1)
        return int(lo) <= value <= int(hi)
    return value == int(field)


def cron_matches(cron_expr: str, dt: datetime) -> bool:
    """检查 5 字段 cron 表达式是否匹配给定时间。

    Cron 字段顺序：minute hour day-of-month month day-of-week

    关键规则——DOM 和 DOW 的 OR 语义：
      如果 DOM 和 DOW 都被约束（都不是 *），则满足任一即可。
      这是标准 Unix cron 行为：比如 "0 9 15 * 1" 表示
      "每月 15 号 or 每周一" 的 9:00，而不是 "必须是 15 号且是周一"。

    参数 cron_expr：5 字段 cron 表达式。
    参数 dt：要检查的日期时间。
    返回：True（匹配）或 False。
    """
    fields = cron_expr.strip().split()
    if len(fields) != 5:
        return False
    minute, hour, dom, month, dow = fields

    # Python weekday: Monday=0 → cron Sunday=0（转换）
    dow_val = (dt.weekday() + 1) % 7

    m = _cron_field_matches(minute, dt.minute)
    h = _cron_field_matches(hour, dt.hour)
    dom_ok = _cron_field_matches(dom, dt.day)
    month_ok = _cron_field_matches(month, dt.month)
    dow_ok = _cron_field_matches(dow, dow_val)

    # 分钟、小时、月份必须都匹配（AND）
    if not (m and h and month_ok):
        return False

    # DOM 和 DOW：如果两者都被约束，任一匹配即可（OR）
    dom_unconstrained = dom == "*"
    dow_unconstrained = dow == "*"
    if dom_unconstrained and dow_unconstrained:
        return True
    if dom_unconstrained:
        return dow_ok
    if dow_unconstrained:
        return dom_ok
    return dom_ok or dow_ok


# ── Cron 表达式验证 ──────────────────────────────────────
def _validate_cron_field(field: str, lo: int, hi: int) -> str | None:
    """验证单个 cron 字段是否在 [lo, hi] 合法范围内。

    返回：None（合法）或错误描述字符串。
    """
    if field == "*":
        return None
    if field.startswith("*/"):
        step_str = field[2:]
        if not step_str.isdigit():
            return f"Invalid step: {field}"
        step = int(step_str)
        if step <= 0:
            return f"Step must be > 0: {field}"
        return None
    if "," in field:
        for part in field.split(","):
            err = _validate_cron_field(part.strip(), lo, hi)
            if err: return err
        return None
    if "-" in field:
        parts = field.split("-", 1)
        if not parts[0].isdigit() or not parts[1].isdigit():
            return f"Invalid range: {field}"
        a, b = int(parts[0]), int(parts[1])
        if a < lo or a > hi or b < lo or b > hi:
            return f"Range {field} out of bounds [{lo}-{hi}]"
        if a > b:
            return f"Range start > end: {field}"
        return None
    if not field.isdigit():
        return f"Invalid field: {field}"
    val = int(field)
    if val < lo or val > hi:
        return f"Value {val} out of bounds [{lo}-{hi}]"
    return None


def validate_cron(cron_expr: str) -> str | None:
    """验证完整 5 字段 cron 表达式。

    各字段范围：
      minute       0-59
      hour         0-23
      day-of-month 1-31
      month        1-12
      day-of-week  0-6（0=Sunday）

    返回：None（合法）或错误描述字符串。
    """
    fields = cron_expr.strip().split()
    if len(fields) != 5:
        return f"Expected 5 fields, got {len(fields)}"
    bounds = [(0, 59), (0, 23), (1, 31), (1, 12), (0, 6)]
    names = ["minute", "hour", "day-of-month", "month", "day-of-week"]
    for i, (field, (lo, hi), name) in enumerate(zip(fields, bounds, names)):
        err = _validate_cron_field(field, lo, hi)
        if err:
            return f"{name}: {err}"
    return None


# ── 持久化存储 ───────────────────────────────────────────

def save_durable_jobs():
    """将 durable=True 的任务持久化到 .scheduled_tasks.json。

    每次 schedule_job/cancel_job 后调用，保证磁盘与内存同步。
    """
    durable = [asdict(j) for j in scheduled_jobs.values() if j.durable]
    DURABLE_PATH.write_text(json.dumps(durable, indent=2))


def load_durable_jobs():
    """启动时从 .scheduled_tasks.json 加载持久化任务。

    加载前验证 cron 表达式合法性——跳过无效的（不报错崩溃）。
    打印加载成功的任务数量。
    """
    if not DURABLE_PATH.exists():
        return
    try:
        jobs = json.loads(DURABLE_PATH.read_text())
        for j in jobs:
            job = CronJob(**j)
            err = validate_cron(job.cron)
            if err:
                print(f"  \033[31m[cron] skipping invalid job {job.id}: {err}\033[0m")
                continue
            scheduled_jobs[job.id] = job
        valid = [j for j in jobs if j["id"] in scheduled_jobs]
        if valid:
            print(f"  \033[35m[cron] loaded {len(valid)} durable job(s)\033[0m")
    except Exception:
        pass  # 文件损坏 → 静默跳过，不阻塞启动


# ── 任务管理 ────────────────────────────────────────────

def schedule_job(cron: str, prompt: str, recurring: bool = True,
                 durable: bool = True) -> CronJob | str:
    """注册新的 cron 任务。

    参数 cron：5 字段 cron 表达式。
    参数 prompt：触发时注入到 Agent 对话的消息。
    参数 recurring：True=重复执行，False=一次性（触发后自动删除）。
    参数 durable：True=持久化到磁盘。

    返回：CronJob 对象（成功）或错误字符串（cron 表达式非法）。
    """
    err = validate_cron(cron)
    if err:
        return err
    job = CronJob(
        id=f"cron_{random.randint(0, 999999):06d}",
        cron=cron, prompt=prompt,
        recurring=recurring, durable=durable,
    )
    with cron_lock:
        scheduled_jobs[job.id] = job
    if durable:
        save_durable_jobs()
    print(f"  \033[35m[cron register] {job.id} '{cron}' → {prompt[:40]}\033[0m")
    return job


def cancel_job(job_id: str) -> str:
    """取消 cron 任务（从内存和持久化中移除）。"""
    with cron_lock:
        job = scheduled_jobs.pop(job_id, None)
    if not job:
        return f"Job {job_id} not found"
    if job.durable:
        save_durable_jobs()
    print(f"  \033[31m[cron cancel] {job_id}\033[0m")
    return f"Cancelled {job_id}"


# ── 调度线程 ────────────────────────────────────────────

def cron_scheduler_loop():
    """独立守护线程：每 1 秒轮询，匹配的任务放入 cron_queue。

    执行流程：
      1. sleep(1) → 避免忙等
      2. datetime.now() → 获取当前时间
      3. 遍历 scheduled_jobs（持有 cron_lock 保护）
      4. 对每个 job 调用 cron_matches(job.cron, now)
      5. 匹配成功？→ 检查 _last_fired 防止重复触发 → 放入 cron_queue

    关键设计——date-aware minute 标记（防止重复触发）：
      _last_fired 存储 "YYYY-MM-DD HH:MM" 而非仅 "HH:MM"。
      这防止以下场景：*/5 任务在 9:05 触发，Agent 花了 3 分钟处理，
      到 9:08 调度器再次检查，发现 9:05 分钟匹配但 _last_fired
      还是 "09:00"（上一次），于是重复触发。
      用完整日期时间标记彻底解决此问题。

    一次性任务的处理：
      recurring=False → 放入队列后立即从 scheduled_jobs 删除。
      这意味着 cron_queue 中的一次性任务即使未被消费（Agent 忙），
      也不会重复触发。队列处理器后面唤醒时从 cron_queue 中消费它。

    单任务异常保护：
      每个 cron 任务的匹配/触发逻辑包裹在 try/except 中。
      一个任务出 bug 不会杀死整个调度线程。
    """
    while True:
        time.sleep(1)
        now = datetime.now()
        minute_marker = now.strftime("%Y-%m-%d %H:%M")  # 精确到分钟
        with cron_lock:
            for job in list(scheduled_jobs.values()):
                try:
                    if cron_matches(job.cron, now):
                        # 防止同一分钟重复触发：同一个 minute_marker 只触发一次
                        if _last_fired.get(job.id) != minute_marker:
                            cron_queue.append(job)
                            _last_fired[job.id] = minute_marker
                            print(f"  \033[35m[cron fire] {job.id} → "
                                  f"{job.prompt[:40]}\033[0m")
                        # 一次性任务 → 触发后立即删除
                        if not job.recurring:
                            scheduled_jobs.pop(job.id, None)
                            if job.durable:
                                save_durable_jobs()
                except Exception as e:
                    print(f"  \033[31m[cron error] {job.id}: {e}\033[0m")


def consume_cron_queue() -> list[CronJob]:
    """消费 cron_queue 中所有已触发的任务（由 agent_loop 调用）。

    返回：已触发的 CronJob 列表（消费后清空队列）。
    """
    with cron_lock:
        fired = list(cron_queue)
        cron_queue.clear()
    return fired


def has_cron_queue() -> bool:
    """检查 cron_queue 是否有待消费的任务。

    被 queue_processor_loop 调用，用于判断是否需要自动唤醒 Agent。
    """
    with cron_lock:
        return bool(cron_queue)


# ── 启动时：加载持久化任务 + 启动调度线程 ──
load_durable_jobs()
threading.Thread(target=cron_scheduler_loop, daemon=True).start()
print("  \033[35m[cron] scheduler thread started\033[0m")


# ═══════════════════════════════════════════════════════════
#  Cron 工具包装函数
#
#  schedule_cron / list_crons / cancel_cron 被 execute_tool
#  内联映射引用，因此定义在 execute_tool 之前的顺序关系。
# ═══════════════════════════════════════════════════════════

def run_schedule_cron(cron: str, prompt: str,
                      recurring: bool = True, durable: bool = True) -> str:
    """工具包装：调度 cron 任务。

    调用底层 schedule_job → 格式化 cron id 和表达式返回给 LLM。
    如果 cron 表达式不合法，schedule_job 返回错误字符串，原样传递。

    参数 recurring=True：重复执行（直到被 cancel）。
    recurring=False：触发一次后自动删除——适合"30分钟后提醒我"场景。
    参数 durable=True：持久化到 .scheduled_tasks.json（跨会话恢复）。
    """
    result = schedule_job(cron, prompt, recurring, durable)
    if isinstance(result, str):
        return f"Error: {result}"
    return f"Scheduled {result.id}: '{cron}' → {prompt}"


def run_list_crons() -> str:
    """工具包装：列出所有已注册的 cron 任务。

    返回格式示例：
      cron_123456: '0 9 * * *' → Check CI status [recurring, durable]
      cron_789012: '*/30 * * * *' → Check on build [recurring, session]

    标签说明：
      recurring — 重复执行（需手动 cancel 停止）
      one-shot  — 触发生效后自动删除
      durable   — 已持久化到 .scheduled_tasks.json
      session   — 仅存在于内存，重启后丢失
    """
    with cron_lock:
        jobs = list(scheduled_jobs.values())
    if not jobs:
        return "No cron jobs. Use schedule_cron to add one."
    lines = []
    for j in jobs:
        tag = "recurring" if j.recurring else "one-shot"
        dur = "durable" if j.durable else "session"
        lines.append(f"  {j.id}: '{j.cron}' → {j.prompt[:40]} "
                     f"[{tag}, {dur}]")
    return "\n".join(lines)


def run_cancel_cron(job_id: str) -> str:
    """工具包装：取消 cron 任务。"""
    return cancel_job(job_id)


# ═══════════════════════════════════════════════════════════
#  工具定义（TOOLS）—— 发送给 LLM 的 JSON Schema
#
#  s14：11 个工具（8 个原有 + 3 个 cron 工具）。
#  每个工具定义包含三个关键字段：name/description/input_schema。
# ═══════════════════════════════════════════════════════════

TOOLS = [
    {"name": "bash", "description": "Run a shell command.",
     "input_schema": {"type": "object",
                      "properties": {
                          "command": {"type": "string"},
                          "run_in_background": {"type": "boolean"}},
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
    # ── s14 新增：3 个 cron 工具 ──
    {"name": "schedule_cron",
     "description": "Schedule a cron job. cron is 5-field: min hour dom month dow.",
     "input_schema": {"type": "object",
                      "properties": {
                          "cron": {"type": "string",
                                   "description": "5-field cron expression"},
                          "prompt": {"type": "string",
                                     "description": "Message to inject when fired"},
                          "recurring": {"type": "boolean",
                                        "description": "True=recurring, False=one-shot"},
                          "durable": {"type": "boolean",
                                      "description": "True=persist to disk"}},
                      "required": ["cron", "prompt"]}},
    {"name": "list_crons",
     "description": "List all registered cron jobs.",
     "input_schema": {"type": "object", "properties": {},
                      "required": []}},
    {"name": "cancel_cron",
     "description": "Cancel a cron job by ID.",
     "input_schema": {"type": "object",
                      "properties": {"job_id": {"type": "string"}},
                      "required": ["job_id"]}},
]


# ═══════════════════════════════════════════════════════════
#  上下文评估
# ═══════════════════════════════════════════════════════════

def update_context(context: dict, messages: list) -> dict:
    """从真实状态推导上下文字典。

    与 get_system_prompt 配合使用——每次 LLM 调用前更新 context，
    确保 system prompt 反映最新状态（如 memory 文件变化）。

    返回：{enabled_tools, workspace, memories}
          enabled_tools：当前可用的工具列表（用于 PROMPT_SECTIONS 中的 tools 片段）
          workspace：工作目录路径
          memories：记忆索引内容（当 .memory/MEMORY.md 存在时）

    参数 context 在 s14 中接收但不使用——注意：这不是拼写错误。
    设计意图是 s14 的 context 直接从真实文件状态推导（memories 读文件），
    不依赖前一轮的 context 缓存。s10/s11 中 context 用于传递大文件缓存状态，
    s14 中 context 被实时重建，因此入参 context 不参与计算。
    参数 messages 同样只在完整实现中用于扫描对话提取记忆关键词。
    两个参数在此版中保留是为了保持与 s10/s11 的接口一致。
    """
    memories = ""
    if MEMORY_INDEX.exists():
        content = MEMORY_INDEX.read_text().strip()
        if content:
            memories = content
    return {
        "enabled_tools": [t["name"] for t in TOOLS],
        "workspace": str(WORKDIR),
        "memories": memories,
    }


# ═══════════════════════════════════════════════════════════
#  agent_loop — 消费 cron 队列 + 正常 LLM 交互
#
#  s14 关键变化（对比 s13）：
#    1. agent_loop 开头消费 cron_queue，将触发的 cron 任务
#       以 "[Scheduled] prompt" 格式注入 messages
#    2. agent_loop 现在返回 context（非 void），供 queue_processor_loop 复用
#    3. 主入口使用 run_agent_turn_locked + agent_lock 确保互斥
#
#  queue_processor_loop 和用户输入通过 agent_lock 互斥：
#    用户输入 → 获取锁 → run_agent_turn_locked → 释放锁
#    cron 触发 → queue_processor_loop 获取锁 → run_agent_turn_locked → 释放锁
#    两者永远不会同时执行 agent_loop。
# ═══════════════════════════════════════════════════════════

def agent_loop(messages: list, context: dict) -> dict:
    """Agent 核心循环：消费 cron 任务 + LLM 交互。

    流程（相比 s13 多了第 1 步的 cron 消费）：
      1. consume_cron_queue → 将已触发的 cron 任务以
         [Scheduled] 格式注入到 messages 开头（每条一条 user 消息）
      2. 调用 LLM（带 tools + system prompt）
      3. stop_reason != "tool_use"？→ 退出循环，返回 context
      4. 遍历 tool_use blocks：
         后台路径（should_run_background 判定）→ start_background_task
         同步路径 → execute_tool
      5. collect_background_results → 注入 <task_notification>
      6. tool_result + 通知回写 → 更新 context → 回到步骤 1

    s14 与 s13 的关键区别：
      1. agent_loop 开头多了一步 consume_cron_queue 消费
      2. agent_loop 返回 context（s13 返回 void）
         这是因为 queue_processor_loop 需要 context 来维持状态
      3. context 在每次循环末尾更新（update_context），同步最新状态

    cron 消息注入的位置：
      cron 任务注入的 [Scheduled] 消息在 LLM 调用之前、其他 user 消息之后。
      这意味着 cron 触发和用户输入在同一个 LLM 调用中混合处理。
      如果 cron 队列中有大量积压任务，它们会一次性全部注入。

    参数 messages：消息历史列表（跨 turns 追加）。
    参数 context：当前上下文字典（供 assemble_system_prompt 使用）。
    返回：更新后的上下文字典（供外部复用）。
    """
    system = get_system_prompt(context)
    while True:
        # ─── Layer 4：消费已触发的 cron 任务 → 注入 messages ───
        fired = consume_cron_queue()
        for job in fired:
            messages.append({"role": "user",
                             "content": f"[Scheduled] {job.prompt}"})
            print(f"  \033[35m[inject cron] {job.prompt[:50]}\033[0m")

        # ─── 调用 LLM ───
        try:
            response = client.messages.create(
                model=MODEL, system=system, messages=messages,
                tools=TOOLS, max_tokens=8000)
        except Exception as e:
            messages.append({"role": "assistant", "content": [
                {"type": "text",
                 "text": f"[Error] {type(e).__name__}: {e}"}]})
            return context

        messages.append({"role": "assistant", "content": response.content})
        if response.stop_reason != "tool_use":
            return context

        # ─── 工具分发 ───
        results = []
        for block in response.content:
            if block.type != "tool_use":
                continue
            print(f"\033[36m> {block.name}\033[0m")

            if should_run_background(block.name, block.input):
                # 后台路径
                bg_id = start_background_task(block)
                results.append({"type": "tool_result",
                                "tool_use_id": block.id,
                                "content": f"[Background task {bg_id} started] "
                                           f"Result will be available when complete."})
            else:
                # 同步路径
                output = execute_tool(block)
                print(str(output)[:300])
                results.append({"type": "tool_result",
                                "tool_use_id": block.id,
                                "content": output})

        # ─── 通知注入 ───
        user_content = list(results)
        bg_notifications = collect_background_results()
        if bg_notifications:
            for notif in bg_notifications:
                user_content.append({"type": "text", "text": notif})
        messages.append({"role": "user", "content": user_content})
        context = update_context(context, messages)
        system = get_system_prompt(context)


# ═══════════════════════════════════════════════════════════
#  会话管理 + 队列处理器
#
#  session_history / session_context：跨 turns 的全局状态。
#  用户输入和 cron 触发共享同一个 session_history。
#
#  queue_processor_loop：独立守护线程，监控 cron_queue。
#  当队列非空且 agent_lock 未被持有时，自动获取锁并调用
#  run_agent_turn_locked（不传 user_query → 处理 cron 触发）。
# ═══════════════════════════════════════════════════════════

session_history: list = []                         # 跨 turns 的对话历史
session_context = update_context({}, [])            # 跨 turns 的上下文


def print_latest_assistant_text(messages: list):
    """打印最后一条 assistant 消息中的 text 块。

    兼容 dict 和 object 两种 block 格式：
      dict 格式：    {"type": "text", "text": "..."}
      object 格式：  block.type == "text", block.text

    需要兼容两种格式的原因是：
      Anthropic SDK 的 response.content 返回的对象（有 .type/.text 属性），
      但 messages 中的历史消息通过 dict 序列化后重新加载，
      此时 content blocks 是 dict 而非 object。
    两种格式在 agent_loop 中交替出现——新响应是 object，
      回写到 messages 后下次读取时变为 dict。
    """
    if not messages:
        return
    msg = messages[-1]
    if not isinstance(msg, dict) or msg.get("role") != "assistant":
        return
    content = msg.get("content", "")
    if isinstance(content, str):
        print(content)
        return
    for block in content:
        if getattr(block, "type", None) == "text":
            print(block.text)
        elif isinstance(block, dict) and block.get("type") == "text":
            print(block.get("text", ""))


def run_agent_turn_locked(user_query: str | None = None):
    """执行一轮 Agent turn。调用者必须持有 agent_lock。

    参数 user_query：用户输入。
      - 非 None：追加到 session_history 后触发 agent_loop（用户主动输入）
      - None：直接触发 agent_loop，不追加 user 消息（cron 自动触发）
      这个区别很重要——cron 触发时消息已经在 agent_loop 内部通过
      consume_cron_queue 注入 [Scheduled] 消息，不需要额外追加 user 消息。

    这个函数是 s14 新增的抽象层——封装了"一次 Agent 交互"的完整过程：
      1. （可选）追加用户消息到 session_history
      2. 进入 agent_loop（消费 cron → LLM → 工具 → 结果）
      3. agent_loop 返回 → 更新 session_context
      4. 打印 LLM 最后一条文本输出

    agent_lock 保证了用户输入和 cron 触发不会同时执行 agent_loop。
    注意：lock 由调用者（with agent_lock:）持有，本函数不再重复加锁。
    """
    global session_context
    if user_query is not None:
        session_history.append({"role": "user", "content": user_query})
    session_context = agent_loop(session_history, session_context)
    session_context = update_context(session_context, session_history)
    print_latest_assistant_text(session_history)
    print()


def queue_processor_loop():
    """守护线程：Agent 空闲时自动投递已触发的 cron 任务。

    这个线程是 s14 的"第三层"——连接调度器（第一层）和 Agent 循环（第四层）的桥梁。
    cron_scheduler_loop 只管"匹配时间→放入队列"，不关心 Agent 是否空闲。
    queue_processor_loop 管"队列有货→唤醒 Agent"。

    执行流程：
      1. sleep(0.2) → 低开销轮询
      2. has_cron_queue()？→ 无工作，continue
      3. agent_lock.acquire(blocking=False)？→ 锁被占（用户正在交互），continue
      4. Double-check：获取锁的间隙队列可能已被消费
      5. 调用 run_agent_turn_locked() → 不传 user_query → 处理 cron 触发
      6. 释放 lock

    参数 blocking=False 的含义：
      不阻塞等待锁。如果 agent_lock 被用户输入持有，直接跳过。
      cron 任务不紧急——等用户这一轮交互完再做也行。

    Double-check pattern 的必要性：
      获取 cron_lock 和获取 agent_lock 不是原子操作。
      T0: has_cron_queue() = True（有工作）
      T1: 另一个线程调用了 run_agent_turn_locked，消费了队列
      T2: agent_lock.acquire() = True（队列已经被消费了）
      如果不做 double-check，就会执行一次"空"的 agent_loop。

    s14 与 s13 的对比：
      s13 的 background_delivery_loop 只能在 agent_loop 结束后"顺带"发送通知。
      s14 的 queue_processor_loop 可以在 Agent 空闲时"主动"触发一轮 agent_loop。
      这是"被动响应"到"主动触发"的关键转变。
    """
    global session_context
    while True:
        time.sleep(0.2)
        if not has_cron_queue():
            continue  # 无待处理任务
        # 尝试获取锁——blocking=False，失败即返回（不等待）
        if not agent_lock.acquire(blocking=False):
            continue  # Agent 正忙（用户交互中）
        try:
            # Double-check：获取锁的间隙可能队列已被消费
            if not has_cron_queue():
                continue
            print("\n  \033[35m[queue processor] delivering scheduled work\033[0m")
            run_agent_turn_locked()  # 不传 user_query → cron 触发
        finally:
            agent_lock.release()


# ═══════════════════════════════════════════════════════════
#  主入口
#
#  启动时序（严格按照依赖顺序）：
#    1. load_durable_jobs()          — 文件顶部的启动时加载
#    2. cron_scheduler_loop 线程     — 依赖 scheduled_jobs 已填充
#    3. queue_processor_loop 线程    — 依赖 cron_queue 被调度器写入
#    4. 主循环等待用户输入
#
#  三个并行运行的角色：
#    ┌─ 主线程（用户交互）───────┐
#    │  while True:              │
#    │    query = input()        │  ← 用户输入
#    │    with agent_lock:       │  ← 获取锁（与 cron 互斥）
#    │      run_agent_turn()     │  ← 执行 agent_loop
#    └───────────────────────────┘
#
#    ┌─ cron_scheduler_loop ─────┐
#    │  while True:              │
#    │    sleep(1)               │
#    │    cron_matches?           │  ← 检查时间
#    │    → cron_queue.append()  │  ← 放入队列（解耦）
#    └───────────────────────────┘
#
#    ┌─ queue_processor_loop ────┐
#    │  while True:              │
#    │    sleep(0.2)             │
#    │    has_cron_queue?         │  ← 检查队列
#    │    agent_lock 空闲?        │  ← 检查用户是否在忙
#    │    → run_agent_turn()     │  ← 自动执行 agent_loop
#    └───────────────────────────┘
#
#  锁层级：
#    agent_lock（高优先级）——保护 agent_loop 不被并发执行
#    cron_lock（低优先级）——保护 scheduled_jobs/cron_queue
#    用户输入时：持有 agent_lock → 内部不获取 cron_lock
#    cron 触发时：queue_processor 尝试获取 agent_lock → 失败则跳过
#    两个 lock 没有嵌套获取场景，不存在死锁风险。
# ═══════════════════════════════════════════════════════════
if __name__ == "__main__":
    print("s14: cron scheduler")
    print("Enter a question, press Enter to send. Type q to quit.\n")

    # 启动队列处理器守护线程
    # 注意：cron_scheduler_loop 已在文件顶部（715行）启动
    # 这里启动 queue_processor_loop，scheduler 和 processor 现在是并行运行的
    threading.Thread(target=queue_processor_loop, daemon=True).start()
    print("  \033[35m[queue processor] started\033[0m")

    # 启动顺序说明：
    #   1. load_durable_jobs() ← 文件顶部（711行）
    #   2. cron_scheduler_loop ← 文件顶部（712行）
    #   3. queue_processor_loop ← 上面刚启动
    #   4. 现在是主循环 —— 三个角色并行运行

    while True:
        try:
            query = input("\033[36ms14 >> \033[0m")
        except (EOFError, KeyboardInterrupt):
            break
        if query.strip().lower() in ("q", "exit", ""):
            break
        # 用户输入与 cron 触发通过 agent_lock 互斥
        # with agent_lock = lock.acquire() + try: ... finally: release()
        # 用户交互期间，cron 触发的 queue_processor 无法获取锁，跳过
        with agent_lock:
            run_agent_turn_locked(query)
