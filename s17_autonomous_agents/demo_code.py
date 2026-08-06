#!/usr/bin/env python3
"""
s17: Autonomous Agents — idle poll + auto-claim + WORK/IDLE lifecycle.

Run:  python s17_autonomous_agents/demo_code.py
Need: pip install anthropic python-dotenv + .env with ANTHROPIC_API_KEY

Changes from s16:
  - scan_unclaimed_tasks: find pending, unowned tasks with deps completed
  - idle_poll: 60s polling loop (inbox + task board), dispatches shutdown in IDLE
  - claim_task: owner check + return value verification
  - Teammate lifecycle: WORK → IDLE → SHUTDOWN
  - Teammate tools: + list_tasks, claim_task, complete_task (5→8)
  - consume_lead_inbox: unified inbox consumer for protocol + context injection
  - Identity re-injection after context compression

ASCII lifecycle:
  WORK: inbox → LLM → tools → (tool_use? loop) → (done? → IDLE)
  IDLE: 5s poll → inbox? → WORK / unclaimed? → claim → WORK / 60s? → SHUTDOWN

──────────────────────────────────────────────
s17 核心（本文件重点注释区）：
  scan_unclaimed_tasks / idle_poll / claim_task（s17 新增自治三件套）
  spawn_teammate_thread 的 run()：WORK → IDLE → SHUTDOWN 三阶段循环
  其余函数为 s01-s16 继承，保留注释以维持章节一致性
──────────────────────────────────────────────
"""

import os, subprocess, json, time, random, threading
from pathlib import Path
from datetime import datetime
from dataclasses import dataclass, asdict, field

try:
    import readline
    readline.parse_and_bind('set bind-tty-special-chars off')
except ImportError:
    pass

from anthropic import Anthropic
from dotenv import load_dotenv

load_dotenv(override=True)
if os.getenv("ANTHROPIC_BASE_URL"):
    os.environ.pop("ANTHROPIC_AUTH_TOKEN", None)

WORKDIR = Path.cwd()
client = Anthropic(base_url=os.getenv("ANTHROPIC_BASE_URL"))
MODEL = os.environ["MODEL_ID"]

# ── Task System (from s12) ──

TASKS_DIR = WORKDIR / ".tasks"
TASKS_DIR.mkdir(exist_ok=True)


@dataclass
class Task:
    id: str
    subject: str
    description: str
    status: str
    owner: str | None
    blockedBy: list[str]


def _task_path(task_id: str) -> Path:
    return TASKS_DIR / f"{task_id}.json"


def create_task(subject: str, description: str = "",
                blockedBy: list[str] | None = None) -> Task:
    """创建任务并落盘（s12 沿用）。

    ★ owner 初始为 None：这是 s17 自治认领的前提——
      无 owner 的任务才在 scan_unclaimed_tasks 的候选范围内。
    """
    task = Task(
        id=f"task_{int(time.time())}_{random.randint(0, 9999):04d}",
        subject=subject, description=description,
        status="pending", owner=None,
        blockedBy=blockedBy or [],
    )
    save_task(task)
    return task


def save_task(task: Task):
    """把任务序列化成 JSON 写入 .tasks/{id}.json（s12 沿用）。"""
    _task_path(task.id).write_text(json.dumps(asdict(task), indent=2))


def load_task(task_id: str) -> Task:
    """从 JSON 文件反序列化任务（s12 沿用）。

    ★ Task(**dict) 模式：asdict 序列化 + ** 解包反序列化，
      天然支持 dataclass 来回转换（见学习记录 09 的持久化模式）。
    """
    return Task(**json.loads(_task_path(task_id).read_text()))


def list_tasks() -> list[Task]:
    """列出看板上所有任务，按文件名排序（s12 沿用）。"""
    return [Task(**json.loads(p.read_text()))
            for p in sorted(TASKS_DIR.glob("task_*.json"))]


def get_task(task_id: str) -> str:
    """返回单个任务的完整 JSON 字符串（s12 沿用）。"""
    task = load_task(task_id)
    return json.dumps(asdict(task), indent=2)


def can_start(task_id: str) -> bool:
    """判断任务是否可以开始：所有 blockedBy 依赖都已 completed。

    执行流程：
      ① 加载任务，遍历所有依赖 ID
      ② 依赖任务文件不存在 → 不可开始
      ③ 依赖任务状态 != "completed" → 不可开始（被阻塞）
      ④ 全部依赖完成后 → 可开始

    ★ 依赖语义：有依赖 ≠ 不能做；只有"被未完成的依赖阻塞"才不能做。
      （README 强调：不是 blockedBy 为空才可认领，而是没有未完成依赖。）
    """
    task = load_task(task_id)
    for dep_id in task.blockedBy:
        if not _task_path(dep_id).exists():
            return False
        if load_task(dep_id).status != "completed":
            return False
    return True


def claim_task(task_id: str, owner: str = "agent") -> str:
    """认领一个 pending 任务，将其状态改为 in_progress。

    执行流程：
      ① 加载任务
      ② 校验：必须 pending（不能认领已开始/已完成的任务）
      ③ 校验：必须无 owner（防止两个队友抢同一个任务 → 后写覆盖）
      ④ 校验：依赖必须全部完成（can_start）
      ⑤ 通过 → 写入 owner + 状态置为 in_progress，落盘
      ⑥ 返回成功/失败信息

    ★ 并发安全（教学版 vs CC）：
      教学版没有文件锁，owner 检查是"读时判断"，仍存在 TOCTOU 窗口
      （两个队友同时读到 owner=None 再同时写入）。CC 用 proper-lockfile
      在锁内完成读-改-写。这里至少挡掉了最明显的"后写覆盖"。
    """
    task = load_task(task_id)
    if task.status != "pending":
        return f"Task {task_id} is {task.status}, cannot claim"
    if task.owner:
        return f"Task {task_id} already owned by {task.owner}"
    if not can_start(task_id):
        deps = [d for d in task.blockedBy
                if _task_path(d).exists() and load_task(d).status != "completed"]
        missing = [d for d in task.blockedBy if not _task_path(d).exists()]
        parts = []
        if deps: parts.append(f"blocked by: {deps}")
        if missing: parts.append(f"missing deps: {missing}")
        return "Cannot start — " + ", ".join(parts)
    task.owner = owner
    task.status = "in_progress"
    save_task(task)
    print(f"  \033[36m[claim] {task.subject} → in_progress\033[0m")
    return f"Claimed {task.id} ({task.subject})"


def complete_task(task_id: str) -> str:
    """将 in_progress 任务标记为 completed，并回报"解锁了下游谁"。

    执行流程：
      ① 校验：必须 in_progress（未开始/已结束都不能重复完成）
      ② 状态置为 completed，落盘
      ③ 扫描看板：找出因本任务完成而"解锁"（依赖全完成）的 pending 任务
      ④ 返回结果，附带 Unblocked 提示 → 队友据此认领下一批

    ★ 返回值信息量：告诉调用方"因为这一单完成，谁可以开始做了"，
      队友无需全量扫描看板也能知道下一步去哪。
    """
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
    return msg


# ── Prompt Assembly (from s10) ──

PROMPT_SECTIONS = {
    "identity": "You are a coding agent. Act, don't explain.",
    "tools": "Available tools: bash, read_file, write_file, "
             "create_task, list_tasks, get_task, claim_task, complete_task, "
             "spawn_teammate, send_message, check_inbox, "
             "request_shutdown, request_plan, review_plan.",
    "workspace": f"Working directory: {WORKDIR}",
    "memory": "Relevant memories are injected below when available.",
}


def assemble_system_prompt(context: dict) -> str:
    sections = [PROMPT_SECTIONS["identity"],
                PROMPT_SECTIONS["tools"],
                PROMPT_SECTIONS["workspace"]]
    if context.get("memories"):
        sections.append(f"Relevant memories:\n{context['memories']}")
    return "\n\n".join(sections)


_last_context_hash, _last_prompt = None, None


def get_system_prompt(context: dict) -> str:
    """带缓存的 system prompt 组装（s10 沿用）。

    执行流程：（这里可以看一下s10的readme，有说明为什么不用hash）
      ① 把 context 序列化做哈希（★ 排序后比较，忽略 key 顺序差异）
      ② 哈希未变且已有缓存 → 直接返回旧 prompt（避免重复拼接）
      ③ 哈希变了 → 重新组装并缓存
    """
    global _last_context_hash, _last_prompt
    #
    h = json.dumps(context, sort_keys=True)
    if h == _last_context_hash and _last_prompt:
        return _last_prompt
    _last_context_hash, _last_prompt = h, assemble_system_prompt(context)
    return _last_prompt


# ── Tools (from s15) ──

def safe_path(p: str) -> Path:
    path = (WORKDIR / p).resolve()
    if not path.is_relative_to(WORKDIR):
        raise ValueError(f"Path escapes workspace: {p}")
    return path


def run_bash(command: str) -> str:
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


# ── MessageBus (from s15) ──

MAILBOX_DIR = WORKDIR / ".mailboxes"
MAILBOX_DIR.mkdir(exist_ok=True)


class MessageBus:
    """基于文件的进程内消息总线（s15 沿用，教学简化版）。

    ★ 文件做邮箱：每个 agent 一个 {agent}.jsonl 追加式日志。
      发送=追加一行，读取=整文件读出后删除（消费式）。
      简化了真实 CC 的 mailbox，核心语义（发送/读取/清空）一致。
    """

    def send(self, from_agent: str, to_agent: str, content: str,
             msg_type: str = "message", metadata: dict = None):
        """发送消息：追加一行 JSON 到目标收件箱文件。"""
        msg = {"from": from_agent, "to": to_agent,
               "content": content, "type": msg_type,
               "ts": time.time(), "metadata": metadata or {}}
        inbox = MAILBOX_DIR / f"{to_agent}.jsonl"
        with open(inbox, "a") as f:
            f.write(json.dumps(msg) + "\n")
        print(f"  \033[33m[bus] {from_agent} → {to_agent}: "
              f"({msg_type}) {content[:50]}\033[0m")

    def read_inbox(self, agent: str) -> list[dict]:
        """读取收件箱全部消息，读完删除文件（消费式读取）。

        ★ 读走即清空：防止同一条消息被多个调用方重复处理。
        """
        inbox = MAILBOX_DIR / f"{agent}.jsonl"
        if not inbox.exists():
            return []
        msgs = [json.loads(line) for line in inbox.read_text().splitlines()
                if line.strip()]
        inbox.unlink()
        return msgs


BUS = MessageBus()
active_teammates: dict[str, bool] = {}


# ── Protocol State (from s16) ──

@dataclass
class ProtocolState:
    request_id: str
    type: str
    sender: str
    target: str
    status: str
    payload: str
    created_at: float = field(default_factory=time.time)


pending_requests: dict[str, ProtocolState] = {}


def new_request_id() -> str:
    """生成唯一请求 ID：req_{6位随机数}（s16 沿用）。

    ★ 用纯随机数而非时间戳：同一秒可能产生多个请求，
      随机数降低碰撞概率。
    """
    return f"req_{random.randint(0, 999999):06d}"


def match_response(response_type: str, request_id: str, approve: bool):
    """将协议响应关联回原始请求（通过 request_id），更新状态机。

    执行流程：
      ① 凭 request_id 在 pending_requests 里找原始请求
      ② 找不到 → 未知请求，忽略
      ③ 类型校验：shutdown 只能由 shutdown_response 响应，
         plan_approval 只能由 plan_approval_response 响应
         （防止"关机的响应"意外批准"计划审批的请求"）
      ④ 通过 → status 置为 approved / rejected

    ★ 相比 s16 的版本：s16 还有幂等校验（非 pending 的重复响应忽略），
      s17 精简掉了这一步——教学简化，正常单次响应不影响正确性。
    """
    # ─── ① 查找原始请求状态 ───
    state = pending_requests.get(request_id)
    if not state:
        print(f"  \033[31m[protocol] unknown request_id: {request_id}\033[0m")
        return
    # ─── ③ 类型校验：响应类型必须匹配请求类型 ───
    if state.type == "shutdown" and response_type != "shutdown_response":
        print(f"  \033[31m[protocol] type mismatch: expected shutdown_response, "
              f"got {response_type}\033[0m")
        return
    if state.type == "plan_approval" and response_type != "plan_approval_response":
        print(f"  \033[31m[protocol] type mismatch: expected plan_approval_response, "
              f"got {response_type}\033[0m")
        return
    # ─── ④ 更新状态机 ───
    state.status = "approved" if approve else "rejected"
    icon = "✓" if approve else "✗"
    color = "32" if approve else "31"
    print(f"  \033[{color}m[protocol] {state.type} {icon} "
          f"({request_id}: {state.status})\033[0m")


# ═══════════════════════════════════════════════════════════
#  Autonomous Agent（s17 新增核心）
#
#  问题：s16 队友等 Lead 分配任务。看板上有 10 个未认领任务，
#  Lead 就得手动 assign 10 次——不能扩展。
#
#  s17 方案：队友自组织，自己看板、自己认领。
#    1) scan_unclaimed_tasks：扫描看板找"可认领"任务
#       （pending + 无 owner + 依赖全完成）
#    2) idle_poll：空闲时每 5s 轮询（inbox 优先，其次任务板），
#       60s 无活 → timeout 退出；收到 shutdown → 立即退出
#    3) 队友生命周期：WORK → IDLE → SHUTDOWN 三阶段循环
# ═══════════════════════════════════════════════════════════

IDLE_POLL_INTERVAL = 5   # seconds（IDLE 轮询间隔）
IDLE_TIMEOUT = 60         # seconds（IDLE 总超时）


def scan_unclaimed_tasks() -> list[dict]:
    """扫描任务看板，找出"可认领"的任务。

    可认领三条件（缺一不可）：
      ① status == "pending"（还没开始）
      ② 无 owner（没人认领）
      ③ can_start 为真（所有 blockedBy 依赖已完成，不被阻塞）

    执行流程：
      ① 遍历 .tasks/ 下所有 task_*.json
      ② 逐个判断三个条件
      ③ 满足 → 收入候选列表
      ④ 返回（教学版按文件名排序取第一个；CC 用文件锁防止抢单）

    ★ 依赖语义：有依赖 ≠ 不可认领；只有"依赖未完成"才不可认领。
    """
    unclaimed = []
    for f in sorted(TASKS_DIR.glob("task_*.json")):
        task = json.loads(f.read_text())
        if (task.get("status") == "pending"
                and not task.get("owner")
                and can_start(task["id"])):
            unclaimed.append(task)
    return unclaimed


def idle_poll(name: str, messages: list, role: str) -> str:
    """队友的 IDLE 阶段：每 5s 轮询一次，找活干。

    返回值（三选一）：
      "work"     → 收到普通消息 或 成功认领任务 → 回到 WORK 阶段
      "shutdown" → 收到 shutdown_request → 回复后立即退出
      "timeout"  → 60s 无新任务 → 退出（SHUTDOWN）

    每轮轮询流程：
      ① sleep 5s（★ 释放 GIL，Lead/其他队友可并行跑）
      ② 读 inbox —— 收件箱优先！
         ├ 含 shutdown_request → 回 shutdown_response → 返回 "shutdown"
         └ 其他消息 → 注入上下文 → 返回 "work"
      ③ 无 inbox 消息 → 扫描任务看板
         ├ 有可认领任务 → claim_task → 成功则注入 auto-claimed → 返回 "work"
         └ claim 失败（被抢/依赖未完成）→ 打印，继续下一轮
      ④ 12 次循环（60s）全部空转 → 返回 "timeout"

    ★ 为什么 inbox 优先？shutdown_request 等协议消息必须立即处理，
      不能被普通消息挤到下一轮。s16 教训：协议响应不能丢。
    """
    for _ in range(IDLE_TIMEOUT // IDLE_POLL_INTERVAL):
        time.sleep(IDLE_POLL_INTERVAL)

        # ─── ① 读收件箱（优先）───
        inbox = BUS.read_inbox(name)
        if inbox:
            # ② shutdown_request → 立即回复并退出（不等下一轮 WORK）
            for msg in inbox:
                if msg.get("type") == "shutdown_request":
                    req_id = msg.get("metadata", {}).get("request_id", "")
                    BUS.send(name, "lead", "Shutting down gracefully.",
                             "shutdown_response",
                             {"request_id": req_id, "approve": True})
                    print(f"  \033[35m[protocol] {name} approved shutdown "
                          f"in idle ({req_id})\033[0m")
                    return "shutdown"

            # ③ 非协议消息 → 注入上下文 → 回到 WORK
            messages.append({"role": "user",
                "content": "<inbox>" + json.dumps(inbox) + "</inbox>"})
            print(f"  \033[36m[idle] {name} found inbox messages\033[0m")
            return "work"

        # ─── ④ 无消息 → 扫描任务看板，尝试自动认领 ───
        unclaimed = scan_unclaimed_tasks()
        if unclaimed:
            task = unclaimed[0]  # 教学版只认第一个
            result = claim_task(task["id"], name)
            if "Claimed" in result:
                # 认领成功 → 注入 <auto-claimed> 到上下文 → 回到 WORK
                messages.append({"role": "user",
                    "content": f"<auto-claimed>Task {task['id']}: "
                               f"{task['subject']}</auto-claimed>"})
                print(f"  \033[32m[idle] {name} auto-claimed: "
                      f"{task['subject']}\033[0m")
                return "work"
            # 认领失败（已被抢/依赖未完成）→ 打印原因，继续下一轮
            print(f"  \033[33m[idle] {name} claim failed: "
                  f"{result}\033[0m")

    # ─── ⑤ 全部轮询空转 → 超时退出 ───
    print(f"  \033[31m[idle] {name} timeout ({IDLE_TIMEOUT}s)\033[0m")
    return "timeout"


# ── Teammate Thread (from s15 + s16 + s17) ──

def spawn_teammate_thread(name: str, role: str, prompt: str) -> str:
    """启动一个自治队友线程（s17：可自己认领任务）。

    执行流程：
      ① 防重名：已存在同名队友 → 拒绝
      ② 组装队友 system prompt
      ③ 定义 8 个队友工具（比 s16 多 list_tasks/claim_task/complete_task）
      ④ 定义 run() 线程主体（WORK → IDLE → SHUTDOWN）
      ⑤ 注册 active_teammates + 启动线程，返回确认

    ★ 线程以 daemon=True 启动：主进程退出时队友线程随之结束，
      不会阻塞程序退出。
    """
    if name in active_teammates:
        return f"Teammate '{name}' already exists"

    system = (f"You are '{name}', a {role}. "
              f"Use tools to complete tasks. "
              f"You can list and claim tasks from the board. "
              f"Check inbox for protocol messages.")

    def handle_inbox_message(name: str, msg: dict, messages: list):
        """分发队友收件箱里的协议消息（WORK 阶段用）。

        执行流程：
          ① 取消息类型 + metadata
          ② shutdown_request → 回 shutdown_response + 返回 True（要求退出）
          ③ plan_approval_response → 注入 [Plan approved]/[Plan rejected]
             到上下文 + 返回 False（继续工作）
          ④ 其他类型 → 返回 False（不处理）

        ★ 返回值语义：True = "应当退出"（收到关机），
          False = "继续干活"（消息已注入上下文 / 无需处理）。
        """
        msg_type = msg.get("type", "message")
        meta = msg.get("metadata", {})
        req_id = meta.get("request_id", "")

        if msg_type == "shutdown_request":
            BUS.send(name, "lead", "Shutting down gracefully.",
                     "shutdown_response",
                     {"request_id": req_id, "approve": True})
            print(f"  \033[35m[protocol] {name} approved shutdown "
                  f"({req_id})\033[0m")
            return True

        if msg_type == "plan_approval_response":
            approve = meta.get("approve", False)
            if approve:
                messages.append({"role": "user",
                    "content": "[Plan approved] Proceed with the task."})
            else:
                messages.append({"role": "user",
                    "content": f"[Plan rejected] Feedback: {msg['content']}"})
        return False

    def run():
        """队友线程主体：WORK → IDLE → SHUTDOWN 三阶段循环。"""
        # 队友的独立上下文（与 Lead 隔离，从 spawn 时的 prompt 开始）
        messages = [{"role": "user", "content": prompt}]
        sub_tools = [
            {"name": "bash", "description": "Run a shell command.",
             "input_schema": {"type": "object",
                              "properties": {"command": {"type": "string"}},
                              "required": ["command"]}},
            {"name": "read_file", "description": "Read file.",
             "input_schema": {"type": "object",
                              "properties": {"path": {"type": "string"}},
                              "required": ["path"]}},
            {"name": "write_file", "description": "Write file.",
             "input_schema": {"type": "object",
                              "properties": {"path": {"type": "string"},
                                             "content": {"type": "string"}},
                              "required": ["path", "content"]}},
            {"name": "send_message",
             "description": "Send message to another agent.",
             "input_schema": {"type": "object",
                              "properties": {"to": {"type": "string"},
                                             "content": {"type": "string"}},
                              "required": ["to", "content"]}},
            {"name": "submit_plan",
             "description": "Submit a plan for Lead approval.",
             "input_schema": {"type": "object",
                              "properties": {"plan": {"type": "string"}},
                              "required": ["plan"]}},
            # s17 new: teammates can list, claim, and complete tasks
            {"name": "list_tasks",
             "description": "List all tasks on the board.",
             "input_schema": {"type": "object", "properties": {},
                              "required": []}},
            {"name": "claim_task",
             "description": "Claim a pending task.",
             "input_schema": {"type": "object",
                              "properties": {"task_id": {"type": "string"}},
                              "required": ["task_id"]}},
            {"name": "complete_task",
             "description": "Mark an in-progress task as completed.",
             "input_schema": {"type": "object",
                              "properties": {"task_id": {"type": "string"}},
                              "required": ["task_id"]}},
        ]

        def _run_list_tasks():
            tasks = list_tasks()
            if not tasks:
                return "No tasks."
            return "\n".join(
                f"  {t.id}: {t.subject} [{t.status}]"
                for t in tasks)

        def _run_claim_task(task_id: str):
            return claim_task(task_id, owner=name)

        def _run_complete_task(task_id: str):
            return complete_task(task_id)

        sub_handlers = {
            "bash": run_bash, "read_file": run_read, "write_file": run_write,
            "send_message": lambda to, content: (BUS.send(name, to, content),
                                                  "Sent")[1],
            "submit_plan": lambda plan: _teammate_submit_plan(name, plan),
            "list_tasks": _run_list_tasks,
            "claim_task": _run_claim_task,
            "complete_task": _run_complete_task,
        }

        # ═══════════════════════════════════════════════════
        #  队友生命周期：WORK → IDLE → SHUTDOWN（s17 三阶段）
        #    WORK:  最多 10 轮 LLM 循环（inbox → LLM → 工具 → 循环）
        #    IDLE:  idle_poll 每 5s 轮询 inbox + 任务板，60s 超时
        #    SHUTDOWN: 收到 shutdown 或超时 → 发 summary → 退出
        # ═══════════════════════════════════════════════════
        while True:
            # ─── 身份重注入（s17 新增）───
            # 消息过短说明被 autoCompact 压缩过 → 重新注入身份，
            # 否则队友忘了自己是谁/在干什么。
            if len(messages) <= 3:
                messages.insert(0, {"role": "user",
                    "content": f"<identity>You are '{name}', role: {role}. "
                               f"Continue your work.</identity>"})

            # ─── WORK 阶段：内层循环（最多 10 轮 LLM 调用）───
            should_shutdown = False
            for _ in range(10):
                # ① 读收件箱 + 分发协议消息（含 shutdown_request）
                inbox = BUS.read_inbox(name)
                for msg in inbox:
                    stopped = handle_inbox_message(name, msg, messages)
                    if stopped:
                        should_shutdown = True
                        break
                if should_shutdown:
                    break
                # ② 普通消息注入上下文
                if inbox and not should_shutdown:
                    non_protocol = [m for m in inbox
                                    if m.get("type") == "message"]
                    if non_protocol:
                        messages.append({"role": "user",
                            "content": f"<inbox>{json.dumps(non_protocol)}</inbox>"})

                # ③ 调 LLM（★ 网络 I/O，期间释放 GIL，其他线程可并行）
                try:
                    response = client.messages.create(
                        model=MODEL, system=system, messages=messages[-20:],
                        tools=sub_tools, max_tokens=8000)
                except Exception:
                    break  # LLM 调用失败 → 跳过本轮
                messages.append({"role": "assistant", "content": response.content})
                if response.stop_reason != "tool_use":
                    break  # 模型结束本轮 → WORK 阶段结束
                # ④ 执行模型请求的工具
                results = []
                for block in response.content:
                    if block.type == "tool_use":
                        handler = sub_handlers.get(block.name)
                        output = handler(**block.input) if handler else "Unknown"
                        results.append({"type": "tool_result",
                                        "tool_use_id": block.id,
                                        "content": str(output)})
                messages.append({"role": "user", "content": results})

            if should_shutdown:
                break  # WORK 阶段收到关机 → 直接退出

            # ─── IDLE 阶段（s17 新增）───
            idle_result = idle_poll(name, messages, role)
            if idle_result == "shutdown":
                break  # IDLE 收到关机 → 退出
            if idle_result == "timeout":
                break  # 60s 空转 → SHUTDOWN

        # ─── SHUTDOWN 阶段：发 summary 给 Lead，退出 ───
        # 从 assistant 消息里反向找最后一条文本，作为最终汇报
        summary = "Done."
        for msg in reversed(messages):
            if msg["role"] == "assistant" and isinstance(msg["content"], list):
                for b in msg["content"]:
                    if getattr(b, "type", None) == "text":
                        summary = b.text
                        break
                else:
                    continue
                break
        BUS.send(name, "lead", summary, "result")   # 结果发给 Lead
        active_teammates.pop(name, None)            # 注销在册状态
        print(f"  \033[32m[teammate] {name} finished\033[0m")

    active_teammates[name] = True
    threading.Thread(target=run, daemon=True).start()
    print(f"  \033[36m[teammate] {name} spawned as {role}\033[0m")
    return f"Teammate '{name}' spawned as {role} (autonomous)"


def _teammate_submit_plan(from_name: str, plan: str) -> str:
    """队友提交计划给 Lead 审批（plan_approval 协议，s16 沿用）。

    执行流程：
      ① 生成 request_id
      ② 注册 ProtocolState（status=pending）→ 之后 Lead 的响应靠它匹配
      ③ 发 plan_approval_request 给 Lead
      ④ 返回等待提示（模型能看到，等 Lead 审批后注入结果）

    ★ 与 shutdown 协议同一套机制（request_id + 状态机），方向相反：
      shutdown 是 Lead 发起、队友响应；plan 是队友发起、Lead 响应。
    """
    req_id = new_request_id()
    pending_requests[req_id] = ProtocolState(
        request_id=req_id, type="plan_approval",
        sender=from_name, target="lead",
        status="pending", payload=plan)
    BUS.send(from_name, "lead", plan,
             "plan_approval_request",
             {"request_id": req_id})
    return f"Plan submitted ({req_id}). Waiting for approval..."


# ── Lead Protocol Tools (from s16) ──

def run_request_shutdown(teammate: str) -> str:
    """Lead 发起 shutdown 协议：请求指定队友优雅退出（s16 沿用）。

    执行流程：
      ① 生成 request_id + 注册 ProtocolState（status=pending）
      ② 发 shutdown_request 给目标队友
      ③ 返回确认（含 req_id）

    ★ Lead 后续经 consume_lead_inbox → match_response 拿到
      shutdown_response 时，靠这个 req_id 关联回本请求。
    """
    req_id = new_request_id()
    pending_requests[req_id] = ProtocolState(
        request_id=req_id, type="shutdown",
        sender="lead", target=teammate,
        status="pending", payload="")
    BUS.send("lead", teammate, "Please shut down gracefully.",
             "shutdown_request",
             {"request_id": req_id})
    print(f"  \033[35m[protocol] shutdown_request → {teammate} "
          f"({req_id})\033[0m")
    return f"Shutdown request sent to {teammate} (req: {req_id})"


def run_request_plan(teammate: str, task: str) -> str:
    """Lead 通过普通消息请队友提交计划（s16 沿用）。

    ★ 注意：这里发的是普通 "message" 类型，不是协议消息——
      没有 request_id、没有 ProtocolState。审批流程由队友发起
      submit_plan 后才进入 plan_approval 协议。
    """
    BUS.send("lead", teammate, f"Please submit a plan for: {task}",
             "message")
    return f"Asked {teammate} to submit a plan"


def run_review_plan(request_id: str, approve: bool,
                    feedback: str = "") -> str:
    """Lead 审批队友提交的计划（plan_approval 协议响应端，s16 沿用）。

    执行流程：
      ① 凭 request_id 找到待审批请求
      ② 找不到 → 返回错误
      ③ 幂等：状态不是 pending（已审批过）→ 拒绝重复审批
      ④ 更新状态机 status
      ⑤ 发 plan_approval_response 给队友
      ⑥ 返回审批结果

    ★ 与 _teammate_submit_plan 配对：一个发起、一个响应，
      中间靠 request_id 关联。
    """
    state = pending_requests.get(request_id)
    if not state:
        return f"Request {request_id} not found"
    if state.status != "pending":
        return f"Request {request_id} already {state.status}"
    state.status = "approved" if approve else "rejected"
    BUS.send("lead", state.sender,
             feedback or ("Approved" if approve else "Rejected"),
             "plan_approval_response",
             {"request_id": request_id, "approve": approve})
    icon = "✓" if approve else "✗"
    print(f"  \033[32m[protocol] plan {icon} ({request_id})\033[0m")
    return f"Plan {'approved' if approve else 'rejected'} ({request_id})"


# ── Basic tool handlers ──

def run_create_task(subject: str, description: str = "",
                    blockedBy: list[str] | None = None) -> str:
    task = create_task(subject, description, blockedBy)
    deps = f" (blockedBy: {', '.join(blockedBy)})" if blockedBy else ""
    print(f"  \033[34m[create] {task.subject}{deps}\033[0m")
    return f"Created {task.id}: {task.subject}{deps}"


def run_list_tasks() -> str:
    tasks = list_tasks()
    if not tasks:
        return "No tasks."
    return "\n".join(
        f"  {t.id}: {t.subject} [{t.status}]"
        for t in tasks)


def run_get_task(task_id: str) -> str:
    return get_task(task_id)


def run_claim_task(task_id: str) -> str:
    return claim_task(task_id, owner="agent")


def run_complete_task(task_id: str) -> str:
    return complete_task(task_id)


def run_spawn_teammate(name: str, role: str, prompt: str) -> str:
    return spawn_teammate_thread(name, role, prompt)


def run_send_message(to: str, content: str) -> str:
    BUS.send("lead", to, content)
    return f"Sent to {to}"


def consume_lead_inbox(route_protocol=True) -> list[dict]:
    """统一读取 Lead 收件箱 + 路由协议响应（s16 沿用）。

    执行流程：
      ① BUS.read_inbox 读走所有消息（消费式，读后清空文件）
      ② route_protocol=True → 逐条检查：
         "_response" 结尾 + 带 request_id → 协议响应
         → match_response 更新状态机
      ③ 返回所有消息（check_inbox 工具与主循环末尾共用）

    ★ 统一入口避免"两个调用方各读各的导致协议消息丢失"。
    """
    msgs = BUS.read_inbox("lead")
    if route_protocol:
        for msg in msgs:
            meta = msg.get("metadata", {})
            req_id = meta.get("request_id", "")
            msg_type = msg.get("type", "")
            if req_id and msg_type.endswith("_response"):
                match_response(msg_type, req_id, meta.get("approve", False))
    return msgs


def run_check_inbox() -> str:
    """Lead 的 check_inbox 工具：读收件箱并格式化展示（s16 沿用）。

    执行流程：
      ① 调 consume_lead_inbox（统一入口：路由协议 + 读取消息）
      ② 为空 → 返回 "(inbox empty)"
      ③ 逐条格式化：来源 + 类型 + req_id（若有）+ 内容前 200 字
    """
    msgs = consume_lead_inbox(route_protocol=True)
    if not msgs:
        return "(inbox empty)"
    lines = []
    for m in msgs:
        meta = m.get("metadata", {})
        req_id = meta.get("request_id", "")
        tag = f" [{m['type']} req:{req_id}]" if req_id else f" [{m['type']}]"
        lines.append(f"  [{m['from']}]{tag} {m['content'][:200]}")
    return "\n".join(lines)


# ── Tool Definitions ──

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
    {"name": "create_task",
     "description": "Create a task.",
     "input_schema": {"type": "object",
                      "properties": {"subject": {"type": "string"},
                                     "description": {"type": "string"},
                                     "blockedBy": {"type": "array",
                                                   "items": {"type": "string"}}},
                      "required": ["subject"]}},
    {"name": "list_tasks",
     "description": "List all tasks.",
     "input_schema": {"type": "object", "properties": {}, "required": []}},
    {"name": "get_task",
     "description": "Get full details of a specific task.",
     "input_schema": {"type": "object",
                      "properties": {"task_id": {"type": "string"}},
                      "required": ["task_id"]}},
    {"name": "claim_task",
     "description": "Claim a pending task.",
     "input_schema": {"type": "object",
                      "properties": {"task_id": {"type": "string"}},
                      "required": ["task_id"]}},
    {"name": "complete_task",
     "description": "Complete an in-progress task.",
     "input_schema": {"type": "object",
                      "properties": {"task_id": {"type": "string"}},
                      "required": ["task_id"]}},
    {"name": "spawn_teammate",
     "description": "Spawn an autonomous teammate agent.",
     "input_schema": {"type": "object",
                      "properties": {"name": {"type": "string"},
                                     "role": {"type": "string"},
                                     "prompt": {"type": "string"}},
                      "required": ["name", "role", "prompt"]}},
    {"name": "send_message",
     "description": "Send message to a teammate.",
     "input_schema": {"type": "object",
                      "properties": {"to": {"type": "string"},
                                     "content": {"type": "string"}},
                      "required": ["to", "content"]}},
    {"name": "check_inbox",
     "description": "Check inbox for messages and protocol responses.",
     "input_schema": {"type": "object", "properties": {}, "required": []}},
    {"name": "request_shutdown",
     "description": "Request a teammate to shut down gracefully.",
     "input_schema": {"type": "object",
                      "properties": {"teammate": {"type": "string"}},
                      "required": ["teammate"]}},
    {"name": "request_plan",
     "description": "Ask a teammate to submit a plan for review.",
     "input_schema": {"type": "object",
                      "properties": {"teammate": {"type": "string"},
                                     "task": {"type": "string"}},
                      "required": ["teammate", "task"]}},
    {"name": "review_plan",
     "description": "Approve or reject a submitted plan.",
     "input_schema": {"type": "object",
                      "properties": {
                          "request_id": {"type": "string"},
                          "approve": {"type": "boolean"},
                          "feedback": {"type": "string"}},
                      "required": ["request_id", "approve"]}},
]

TOOL_HANDLERS = {
    "bash": run_bash, "read_file": run_read, "write_file": run_write,
    "create_task": run_create_task, "list_tasks": run_list_tasks,
    "get_task": run_get_task,
    "claim_task": run_claim_task, "complete_task": run_complete_task,
    "spawn_teammate": run_spawn_teammate,
    "send_message": run_send_message, "check_inbox": run_check_inbox,
    "request_shutdown": run_request_shutdown,
    "request_plan": run_request_plan, "review_plan": run_review_plan,
}


# ── Context ──

MEMORY_DIR = WORKDIR / ".memory"
MEMORY_INDEX = MEMORY_DIR / "MEMORY.md"


def update_context(context: dict, messages: list) -> dict:
    """从持久化记忆文件刷新 context（s09/s10 沿用）。

    执行流程：
      ① 检查 .memory/MEMORY.md 是否存在
      ② 存在 → 读取前 2000 字符作为相关记忆
      ③ 返回新 context（get_system_prompt 据此重组 system prompt）
    """
    memories = ""
    if MEMORY_INDEX.exists():
        memories = MEMORY_INDEX.read_text()[:2000]
    return {"memories": memories}


# ── Agent Loop ──

def agent_loop(messages: list, context: dict):
    """Lead 的核心循环：LLM ↔ 工具往返，直到模型结束本轮。

    执行流程：
      ① 组装 system prompt
      ② 调 LLM（★ 网络 I/O，期间释放 GIL，队友线程可并行）
      ③ 追加 assistant 消息
      ④ stop_reason != "tool_use" → 返回（本轮结束）
      ⑤ 逐条执行工具，收集 tool_result
      ⑥ 追加工具结果，刷新 context 后回到 ②
    """
    system = get_system_prompt(context)
    while True:
        try:
            response = client.messages.create(
                model=MODEL, system=system, messages=messages,
                tools=TOOLS, max_tokens=8000)
        except Exception as e:
            # LLM 调用异常 → 记录错误消息并结束本轮
            messages.append({"role": "assistant", "content": [
                {"type": "text", "text": f"[Error] {type(e).__name__}: {e}"}]})
            return

        messages.append({"role": "assistant", "content": response.content})
        if response.stop_reason != "tool_use":
            return

        results = []
        for block in response.content:
            if block.type != "tool_use":
                continue
            print(f"\033[36m> {block.name}\033[0m")
            handler = TOOL_HANDLERS.get(block.name)
            output = handler(**block.input) if handler else "Unknown"
            print(str(output)[:300])
            results.append({"type": "tool_result",
                            "tool_use_id": block.id, "content": output})
        messages.append({"role": "user", "content": results})
        context = update_context(context, messages)
        system = get_system_prompt(context)


if __name__ == "__main__":
    print("s17: autonomous agents")
    print("Enter a question, press Enter to send. Type q to quit.\n")
    history = []                    # Lead 的对话历史
    context = {"memories": ""}      # 记忆上下文（s09/s10）
    while True:
        try:
            query = input("\033[36ms17 >> \033[0m")
        except (EOFError, KeyboardInterrupt):
            break
        if query.strip().lower() in ("q", "exit", ""):
            break
        # ① 用户输入入队 → 跑 Lead 的 agent_loop
        history.append({"role": "user", "content": query})
        agent_loop(history, context)
        context = update_context(context, history)
        # ② 打印 Lead 的最后一条文本回复
        for block in history[-1]["content"]:
            if getattr(block, "type", None) == "text":
                print(block.text)
            elif isinstance(block, dict) and block.get("type") == "text":
                print(block.get("text", ""))

        # ③ 消费 Lead 收件箱：路由协议响应 + 注入队友消息到历史
        #    （队友的 summary/result 不能只打印在终端，LLM 也要能看到）
        inbox = consume_lead_inbox(route_protocol=True)
        if inbox:
            inbox_text = "\n".join(
                f"From {m['from']} [{m.get('type', 'message')}]: "
                f"{m['content'][:200]}" for m in inbox)
            history.append({"role": "user",
                            "content": f"[Inbox]\n{inbox_text}"})
        print()
