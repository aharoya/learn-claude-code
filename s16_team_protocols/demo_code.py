#!/usr/bin/env python3
"""
s16: 团队协议 — request-response 协议 + request_id + 消息分发 + 状态机。

Run:  python s16_team_protocols/demo_code.py
Need: pip install anthropic python-dotenv + .env with ANTHROPIC_API_KEY

Changes from s15:
  - ProtocolState dataclass (request_id, type, sender, status, created_at)
  - pending_requests dict：跟踪进行中的协议请求
  - dispatch_message：按类型将收到的消息路由到处理器
  - request_shutdown：Lead 发送关闭协议请求
  - request_plan：Lead 要求队友提交计划
  - handle_shutdown_request / handle_plan_response：队友接收并响应
  - match_response：Lead 通过 request_id + 类型验证关联响应
  - 队友 idle loop：等待收件箱消息，而非 10 轮后退出
  - consume_lead_inbox 统一函数：协议路由 + 注入 history
  - 3 个新 Lead 工具：request_shutdown, request_plan, review_plan
  - 1 个新队友工具：submit_plan

ASCII 流程:
  Lead: BUS.send("shutdown_request", {request_id}) ──────→ teammate inbox
  Teammate: dispatch → handler → BUS.send("shutdown_response", {request_id}) ─→ Lead inbox
  Lead: consume_lead_inbox → match_response(request_id) → pending_requests[req_id].status = approved
"""

# ======================================================================
# 执行流程（入口 → 结束）
# ======================================================================
#
#   1. 启动 → 初始化 MessageBus + active_teammates + pending_requests
#
#   2. 主循环：input → history.append → agent_loop → consume_lead_inbox
#
#   3. agent_loop 核心循环（与 s15 一致，无 cron）：
#      a. 调用 LLM（TOOLS 含 12+3 个工具）
#      b. 遍历 tool_use → execute_tool 分发
#         【s16 新增】request_shutdown → BUS.send + 注册 ProtocolState
#         【s16 新增】request_plan → BUS.send 要求队友提交计划
#         【s16 新增】review_plan → match_response + BUS.send 批准/拒绝
#      c. 结果回写 → 回到步骤 a
#
#   4. agent_loop 返回 → consume_lead_inbox：
#      a. BUS.read_inbox("lead") 读取收件箱
#      b. 遍历消息 → 如果 msg_type 以 "_response" 结尾：
#         → match_response(msg_type, request_id, approve)
#         → 更新 pending_requests[req_id].status = approved/rejected
#      c. 所有消息注入到 history
#
#   5. 队友线程（s16 升级为 idle loop）：
#      a. 创建 messages → while not shutdown_requested:
#      b. 读收件箱 → handle_inbox_message 按类型分发
#         shutdown_request → BUS.send("shutdown_response") → stop
#         plan_approval_response → 批准则追加 "[Plan approved]"，拒绝则追加反馈
#      c. LLM turn → 工具分发 → 结果回写
#      d. LLM 完成 → 进入 idle 等待（不是退出！）
#         → 每 1 秒检查收件箱 → 有新消息则回到 LLM turn
#      e. shutdown_request → BUS.send 最终摘要 → 退出
# ======================================================================

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
MEMORY_DIR = WORKDIR / ".memory"
MEMORY_INDEX = MEMORY_DIR / "MEMORY.md"
client = Anthropic(base_url=os.getenv("ANTHROPIC_BASE_URL"))
MODEL = os.environ["MODEL_ID"]

# ═══════════════════════════════════════════════════════════
#  任务系统（s12）
# ═══════════════════════════════════════════════════════════

TASKS_DIR = WORKDIR / ".tasks"
TASKS_DIR.mkdir(exist_ok=True)


@dataclass
class Task:
    id: str
    subject: str
    description: str
    status: str          # pending | in_progress | completed
    owner: str | None
    blockedBy: list[str]


def _task_path(task_id: str) -> Path:
    """任务 id → 磁盘文件路径（.tasks/{id}.json）。"""
    return TASKS_DIR / f"{task_id}.json"


def create_task(subject: str, description: str = "",
                blockedBy: list[str] | None = None) -> Task:
    """创建任务并持久化到 .tasks/{id}.json。

    执行流程（数字编号对应下方代码）：
      ① 生成唯一 id（时间戳 + 4 位随机数）
      ② 构造 Task 对象（blockedBy 规范化：None → []）
      ③ save_task 落盘 → 返回任务对象

    参数 blockedBy：前置依赖的任务 id 列表（可空）。
    """
    # ─── ① 生成唯一 id（时间戳秒 + 4 位随机数）───
    task = Task(
        id=f"task_{int(time.time())}_{random.randint(0, 9999):04d}",
        subject=subject, description=description,
        status="pending", owner=None,
        # blockedBy or []：把 None 规范化为空列表，避免后续遍历报错
        blockedBy=blockedBy or [],
    )
    # ─── ② 落盘 → 返回 ───
    save_task(task)
    return task


def save_task(task: Task):
    """把 Task 对象序列化为 JSON 写入磁盘。"""
    _task_path(task.id).write_text(json.dumps(asdict(task), indent=2))


def load_task(task_id: str) -> Task:
    """从磁盘读取任务 JSON，用 **dict 解包还原 Task 对象。

    ★ Python 技巧：`Task(**json.loads(...))` 把 dict 的键值对
      展开成关键字参数传给 dataclass 构造函数——dict 的键必须和
      字段名完全一致。
    """
    return Task(**json.loads(_task_path(task_id).read_text()))


def list_tasks() -> list[Task]:
    """列出 .tasks/ 下所有任务，按文件名排序。

    ★ glob("task_*.json") 只匹配任务文件，不匹配其他文件。
    """
    return [Task(**json.loads(p.read_text()))
            for p in sorted(TASKS_DIR.glob("task_*.json"))]


def get_task(task_id: str) -> str:
    """返回完整任务详情（含 description/blockedBy），供 Agent 读取。"""
    task = load_task(task_id)
    return json.dumps(asdict(task), indent=2)


def can_start(task_id: str) -> bool:
    """检查任务的所有前置依赖是否已完成（claim 的前置检查）。

    规则：blockedBy 中有任何一个任务不存在或未 completed → 返回 False。
    不存在的依赖视为 blocked（避免引用错误 id 时崩溃）。
    """
    task = load_task(task_id)
    for dep_id in task.blockedBy:
        if not _task_path(dep_id).exists():
            return False  # 依赖任务文件不存在 → 视为阻塞
        if load_task(dep_id).status != "completed":
            return False  # 依赖还没做完 → 阻塞
    return True


def claim_task(task_id: str, owner: str = "agent") -> str:
    """认领任务：设置 owner，状态 pending → in_progress。

    执行流程（数字编号对应下方代码）：
      ① 加载任务
      ② 校验状态：必须还是 pending（in_progress/completed 不能再认领）
      ③ 校验依赖：所有 blockedBy 必须已完成（can_start）
      ④ 认领成功 → 写 owner + 状态 → 落盘

    返回值：成功消息或拒绝原因。
    """
    # ─── ① 加载任务 ───
    task = load_task(task_id)
    # ─── ② 状态校验 ───
    if task.status != "pending":
        return f"Task {task_id} is {task.status}, cannot claim"
    # ─── ③ 依赖校验 ───
    if not can_start(task_id):
        deps = [d for d in task.blockedBy
                if not _task_path(d).exists() or load_task(d).status != "completed"]
        return f"Blocked by: {deps}"
    # ─── ④ 认领：写 owner + 状态 → 落盘 ───
    task.owner = owner
    task.status = "in_progress"
    save_task(task)
    print(f"  \033[36m[claim] {task.subject} → in_progress (owner: {owner})\033[0m")
    return f"Claimed {task.id} ({task.subject})"


def complete_task(task_id: str) -> str:
    """完成任务：状态 in_progress → completed，并解锁下游。

    执行流程（数字编号对应下方代码）：
      ① 加载任务
      ② 状态校验：必须 in_progress
      ③ 改状态 + 写磁盘
      ④ 全量扫描：找出刚被解锁的 pending 任务（can_start 变 True）
      ⑤ 拼接返回消息（含解锁列表）
    """
    # ─── ①② 加载 + 状态校验 ───
    task = load_task(task_id)
    if task.status != "in_progress":
        return f"Task {task_id} is {task.status}, cannot complete"
    # ─── ③ 完成并落盘 ───
    task.status = "completed"
    save_task(task)
    # ─── ④ 扫描所有任务，找刚被解锁的下游任务 ───
    unblocked = [t.subject for t in list_tasks()
                 if t.status == "pending" and t.blockedBy and can_start(t.id)]
    print(f"  \033[32m[complete] {task.subject} ✓\033[0m")
    msg = f"Completed {task.id} ({task.subject})"
    if unblocked:
        msg += f"\nUnblocked: {', '.join(unblocked)}"
        print(f"  \033[33m[unblocked] {', '.join(unblocked)}\033[0m")
    return msg


# ── Prompt Assembly (from s10, synced) ──

PROMPT_SECTIONS = {
    "identity": "You are a coding agent. Act, don't explain.",
    "tools": "Available tools: bash, read_file, write_file, "
             "get_task, create_task, list_tasks, claim_task, complete_task, "
             "spawn_teammate, send_message, check_inbox, "
             "request_shutdown, request_plan, review_plan.",
    "workspace": f"Working directory: {WORKDIR}",
    "memory": "Relevant memories are injected below when available.",
}


def assemble_system_prompt(context: dict) -> str:
    """把固定 section + 动态记忆拼成最终 system prompt。

    拼接顺序：identity（身份）→ tools（工具清单）→ workspace（工作目录）
    → 若有记忆则追加（记忆是运行中动态变化的，其余固定）。
    """
    sections = [PROMPT_SECTIONS["identity"],
                PROMPT_SECTIONS["tools"],
                PROMPT_SECTIONS["workspace"]]
    memories = context.get("memories", "")
    if memories:
        sections.append(f"Relevant memories:\n{memories}")
    return "\n\n".join(sections)


# ── system prompt 缓存（s10 设计）──
# 每次 agent_loop 末尾会 update_context → 重新组装 system prompt。
# 如果 context 没变（key 相同），直接复用缓存的 prompt，省一次拼接。
_last_context_key, _last_prompt = None, None


def get_system_prompt(context: dict) -> str:
    """带缓存的 system prompt 获取器。

    ① 把 context 序列化成 JSON 字符串作为 key
       （sort_keys 保证键顺序无关、ensure_ascii=False 保留中文、
        default=str 兜底序列化非 JSON 类型）
    ② key 与上次相同 → 直接返回缓存
    ③ key 变了 → 重新组装并更新缓存
    """
    global _last_context_key, _last_prompt
    key = json.dumps(context, sort_keys=True, ensure_ascii=False, default=str)
    if key == _last_context_key and _last_prompt:
        return _last_prompt
    _last_context_key = key
    _last_prompt = assemble_system_prompt(context)
    return _last_prompt

# ═══════════════════════════════════════════════════════════
#  工具实现（3 基础 + 5 任务）
# ═══════════════════════════════════════════════════════════

def safe_path(p: str) -> Path:
    """把相对路径解析成绝对路径，并校验不能逃出工作目录。

    ★ 安全防线：Agent 是 LLM，可能被 prompt 引导去读写工作区外的文件。
      .resolve() 把路径规范化（去掉 ../ 等），
      is_relative_to 判断是否还在 WORKDIR 内，越界就抛异常。
    """
    path = (WORKDIR / p).resolve()
    if not path.is_relative_to(WORKDIR):
        raise ValueError(f"Path escapes workspace: {p}")
    return path


def run_bash(command: str, run_in_background: bool = False) -> str:
    """执行 shell 命令（同步路径）。

    执行流程（数字编号对应下方代码）：
      ① 调 subprocess.run 执行命令（shell + 捕获输出 + 120s 超时）
      ② 合并 stdout 和 stderr
      ③ 截断到 50000 字符返回（防止输出撑爆上下文）

    ★ run_in_background 参数不在本函数处理：
      真正的后台路径在 agent_loop 分发时判断（should_run_background），
      这里始终走同步执行。
    """
    try:
        # ─── ① 执行命令（cwd=WORKDIR 限制在项目目录内）───
        r = subprocess.run(command, shell=True, cwd=WORKDIR,
                           capture_output=True, text=True, timeout=120)
        # ─── ② 合并 stdout + stderr ───
        out = (r.stdout + r.stderr).strip()
        # ─── ③ 截断返回 ───
        return out[:50000] if out else "(no output)"
    except subprocess.TimeoutExpired:
        return "Error: Timeout (120s)"


def run_read(path: str, limit: int | None = None) -> str:
    """读文件（按行拆分，支持 limit 截断）。

    执行流程（数字编号对应下方代码）：
      ① 校验路径安全 → 读文件按行拆分
      ② limit 存在且超过 → 截断 + 追加提示（还剩几行没显示）
      ③ 行列表拼回字符串返回
    """
    try:
        # ─── ① 读文件 ───
        lines = safe_path(path).read_text().splitlines()
        # ─── ② 按 limit 截断 ───
        if limit and limit < len(lines):
            lines = lines[:limit] + [f"... ({len(lines) - limit} more lines)"]
        # ─── ③ 拼回字符串 ───
        return "\n".join(lines)
    except Exception as e:
        return f"Error: {e}"


def run_write(path: str, content: str) -> str:
    """写文件（自动创建父目录，覆盖写入）。

    执行流程（数字编号对应下方代码）：
      ① 校验路径安全
      ② 自动创建不存在的父目录
      ③ write_text 覆盖写入
    """
    try:
        # ─── ① 校验路径 ───
        fp = safe_path(path)
        # ─── ② 创建父目录 ───
        fp.parent.mkdir(parents=True, exist_ok=True)
        # ─── ③ 覆盖写入 ───
        fp.write_text(content)
        return f"Wrote {len(content)} bytes to {path}"
    except Exception as e:
        return f"Error: {e}"


# Task tools

def run_create_task(subject: str, description: str = "",
                    blockedBy: list[str] | None = None) -> str:
    """工具层：创建任务 + 终端打印 + 返回可读消息（而非 Task 对象）。

    ★ run_ 前缀的函数都是"工具包装层"：把底层函数返回的结构化数据
      （Task 对象 / 列表）转成给 LLM 看的字符串，同时打印彩色日志。
    """
    task = create_task(subject, description, blockedBy)
    deps = f" (blockedBy: {', '.join(blockedBy)})" if blockedBy else ""
    print(f"  \033[34m[create] {task.subject}{deps}\033[0m")
    return f"Created {task.id}: {task.subject}{deps}"


def run_list_tasks() -> str:
    """工具层：列出所有任务，格式化成可读文本。

    每行格式：图标 + id + 标题 + [状态] + 所有者 + 依赖。
    图标映射：pending ○ / in_progress ● / completed ✓。
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
    """工具层：获取任务详情，找不到时返回友好错误。"""
    try:
        return get_task(task_id)
    except FileNotFoundError:
        return f"Error: Task {task_id} not found"


def run_claim_task(task_id: str) -> str:
    """工具层：认领任务（owner 固定为 lead agent 自己）。"""
    return claim_task(task_id, owner="agent")


def run_complete_task(task_id: str) -> str:
    """工具层：完成任务。"""
    return complete_task(task_id)


# ── Background Tasks (from s13, synced) ──

_bg_counter = 0
background_tasks: dict[str, dict] = {}
background_results: dict[str, str] = {}
background_lock = threading.Lock()


def is_slow_operation(tool_name: str, tool_input: dict) -> bool:
    """启发式判断：这条命令是否可能需要 >30s（应放后台）。

    只看 bash 工具；命中"安装/构建/测试/部署"等关键词 → True。
    关键词是硬编码的近似判断，模型显式要求优先（见 should_run_background）。
    """
    if tool_name != "bash":
        return False
    cmd = tool_input.get("command", "").lower()
    slow_keywords = ["install", "build", "test", "deploy", "compile",
                     "docker build", "pip install", "npm install",
                     "cargo build", "pytest", "make"]
    return any(kw in cmd for kw in slow_keywords)


def should_run_background(tool_name: str, tool_input: dict) -> bool:
    """决定一个工具调用是否走后台路径。

    规则：模型显式传 run_in_background=True 优先；
    否则用 is_slow_operation 启发式兜底。
    """
    if tool_input.get("run_in_background"):
        return True
    return is_slow_operation(tool_name, tool_input)


def start_background_task(block) -> str:
    """把工具调用放进 daemon 线程执行，立即返回后台任务 ID。

    执行流程（数字编号对应下方代码）：
      ① 分配递增 bg_id（bg_0001 起）
      ② 定义 worker：执行 execute_tool → 结果存进 background_results
      ③ 先注册状态为 running（带锁，防与 collect 并发读写）
      ④ 启动 daemon 线程（不阻塞 Lead）→ 返回 bg_id

    ★ 生产者-消费者模式：worker 是生产者（把结果写进 dict），
      agent_loop 每轮末尾的 collect_background_results 是消费者。
      background_lock 保护这个共享 dict 的并发读写。
    """
    global _bg_counter
    # ─── ① 分配 bg_id ───
    _bg_counter += 1
    bg_id = f"bg_{_bg_counter:04d}"
    cmd = block.input.get("command", block.name)

    # ─── ② 定义 worker（真正的执行在后台线程）───
    def worker():
        result = execute_tool(block)
        with background_lock:
            background_tasks[bg_id]["status"] = "completed"
            background_results[bg_id] = result

    # ─── ③ 先注册 running 状态 ───
    # 必须在 start 前注册，否则 worker 写完 status 时 key 可能还不存在
    with background_lock:
        background_tasks[bg_id] = {
            "tool_use_id": block.id,
            "command": cmd,
            "status": "running",
        }
    # ─── ④ 启动后台线程 → 立即返回 ───
    threading.Thread(target=worker, daemon=True).start()
    print(f"  \033[33m[background] dispatched {bg_id}: {cmd[:40]}\033[0m")
    return bg_id


def collect_background_results() -> list[str]:
    """收集已完成后台任务，转成 <task_notification> 文本列表。

    执行流程（数字编号对应下方代码）：
      ① 带锁找出所有 status == completed 的任务 id
      ② 逐个弹出（pop）任务记录和结果（消费式：取出即删）
      ③ 格式化成 XML 风格的 task_notification 文本
      ④ 追加到返回列表，供 agent_loop 注入上下文
    """
    # ─── ① 找出已完成的任务 ───
    with background_lock:
        ready_ids = [bid for bid, task in background_tasks.items()
                     if task["status"] == "completed"]
    notifications = []
    for bg_id in ready_ids:
        # ─── ② 消费式取出（pop）───
        with background_lock:
            task = background_tasks.pop(bg_id)
            output = background_results.pop(bg_id, "")
        # ─── ③ 摘要截断到 200 字符 ───
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


# ── MessageBus (from s15) ──

MAILBOX_DIR = WORKDIR / ".mailboxes"
MAILBOX_DIR.mkdir(exist_ok=True)


class MessageBus:
    """File-based message bus. Each agent has a .jsonl inbox.
    Read is destructive: read_text + unlink (consumes messages).
    Teaching version: no file locking; real CC uses proper-lockfile."""

    def send(self, from_agent: str, to_agent: str, content: str,
             msg_type: str = "message", metadata: dict = None):
        """发一条消息：追加写入收件人的 .jsonl 邮箱文件。

        参数：
          msg_type：消息类型。默认 "message"（普通消息）；
                    s16 协议消息用 "shutdown_request" / "shutdown_response"
                    等类型标记。
          metadata：附加结构化信息。协议消息在这里带 request_id / approve。

        消息字段结构：
          {from, to, content, type, ts, metadata}
          → to_agent 的邮箱文件每行一条 JSON。
        """
        msg = {"from": from_agent, "to": to_agent,
               "content": content, "type": msg_type,
               "ts": time.time(), "metadata": metadata or {}}
        inbox = MAILBOX_DIR / f"{to_agent}.jsonl"
        # append 模式打开：消息一条接一条追加，不覆盖历史
        with open(inbox, "a") as f:
            f.write(json.dumps(msg) + "\n")
        print(f"  \033[33m[bus] {from_agent} → {to_agent}: "
              f"({msg_type}) {content[:50]}\033[0m")

    def read_inbox(self, agent: str) -> list[dict]:
        """读收件人的邮箱：读出所有消息 + 删除文件。

        ★ 消费式读取（destructive）：read + unlink。
          消息被读走就从磁盘消失——保证每条消息只被处理一次。
          s15 的 peek 是非破坏性"只看有没有"，这里是真读。

        返回：消息字典列表（空文件 / 无文件返回 []）。
        """
        inbox = MAILBOX_DIR / f"{agent}.jsonl"
        if not inbox.exists():
            return []
        # 逐行解析 JSON（过滤空行）
        msgs = [json.loads(line) for line in inbox.read_text().splitlines()
                if line.strip()]
        inbox.unlink()  # consume: read + delete
        return msgs


BUS = MessageBus()
active_teammates: dict[str, bool] = {}

# ═══════════════════════════════════════════════════════════
#  NEW in s16: 协议状态机
#
#  设计思想：
#    s15 的团队通信是"自由格式"——Lead 和 Teammate 通过
#    send_message + check_inbox 自由 chat。这在简单场景下够用，
#    但需要"结构化交互"时会失控（比如"你怎么还没关？""我关了！"
#    "没关！"）。
#
#    s16 引入协议层：每种结构化交互（关闭、计划审批）定义一个
#    request-response 模式，通过 request_id 关联，通过状态机追踪。
#
#  协议交互模型：
#    Lead 发送 request → 注册 ProtocolState (status=pending)
#    Teammate 收到 → dispatch → handler → 发送 response
#    Lead 收到 response → match_response → 更新 ProtocolState.status
# ═══════════════════════════════════════════════════════════

@dataclass
class ProtocolState:
    """协议请求的状态追踪。

    字段：
      request_id：唯一请求标识（req_{6位随机数}）
      type：协议类型（"shutdown" | "plan_approval"）
      sender：请求发起者
      target：请求目标
      status：请求状态（pending → approved/rejected）
      payload：请求/响应的正文（plan 文本或 shutdown 原因）
      created_at：创建时间戳
    """
    request_id: str
    type: str       # "shutdown" | "plan_approval"
    sender: str
    target: str
    status: str     # pending | approved | rejected
    payload: str    # plan text or shutdown reason
    created_at: float = field(default_factory=time.time)

# 进行中的协议请求（Lead 视角：等待 teammate 响应的请求）
pending_requests: dict[str, ProtocolState] = {}


def new_request_id() -> str:
    """生成唯一请求 ID：req_{6位随机数}。

    ★ 用纯随机数而非时间戳：协议请求可能在同一秒内产生多个，
      随机数降低碰撞概率。6 位随机数有 10^6 种组合，教学场景够用。
    """
    return f"req_{random.randint(0, 999999):06d}"


def match_response(response_type: str, request_id: str, approve: bool):
    """将响应关联到原始请求（通过 request_id），并更新状态机。

    执行流程（数字编号对应下方代码）：
      ① 凭 request_id 在 pending_requests 里找原始请求
      ② 找不到 → 未知请求，忽略（可能已过期/伪造）
      ③ 类型校验：shutdown 请求只能被 shutdown_response 响应，
         plan_approval 只能被 plan_approval_response 响应
         （防止"关机的响应"意外批准"计划审批的请求"）
      ④ 幂等校验：状态不是 pending（已 approved/rejected）→ 忽略重复响应
      ⑤ 通过校验 → status 置为 approved / rejected

    参数 response_type：响应消息的类型（如 "shutdown_response"）。
    参数 request_id：原始请求的 ID（贯穿全链路的关联键）。
    参数 approve：True=批准/确认，False=拒绝。
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
    # ─── ④ 幂等校验：已处理的状态不重复处理 ───
    if state.status != "pending":
        print(f"  \033[33m[protocol] {request_id} already {state.status}, "
              f"ignoring duplicate\033[0m")
        return
    # ─── ⑤ 更新状态机 ───
    state.status = "approved" if approve else "rejected"
    icon = "✓" if approve else "✗"
    color = "32" if approve else "31"
    print(f"  \033[{color}m[protocol] {state.type} {icon} "
          f"({request_id}: {state.status})\033[0m")


# ═══════════════════════════════════════════════════════════
#  Unified Lead Inbox Consumer（s16 修复）
#
#  问题：s15 中 check_inbox 工具和主循环都直接读收件箱，
#  如果 check_inbox 读了协议响应消息，主循环就看不到它们，
#  match_response 就不会被调用，协议请求永远 pending。
#
#  修复：consume_lead_inbox 统一读收件箱 + 协议路由，
#  check_inbox 和主循环都调用它。route_protocol=True
#  时需要匹配响应并更新状态机。
# ═══════════════════════════════════════════════════════════

def consume_lead_inbox(route_protocol: bool = True) -> list[dict]:
    """统一读取 Lead 收件箱 + 路由协议响应。

    执行流程（数字编号对应下方代码）：
      ① BUS.read_inbox 读走所有消息（消费式）
      ② 为空 → 直接返回
      ③ route_protocol=True 时，逐条检查：
         以 "_response" 结尾 + 带 request_id → 是协议响应
         → 调用 match_response 更新状态机
      ④ 返回所有消息（调用方再决定注入 history）

    ★ 为什么必须统一入口？（s16 修复）
      s15 中 check_inbox 工具和主循环各自直接读收件箱——
      如果 check_inbox 先读走了协议响应消息，主循环就看不到它们，
      match_response 不被调用 → 协议请求永远 pending。
      s16 让两个调用方都走这里，保证协议路由不丢。

    参数 route_protocol：True 时自动匹配 _response 消息。
    返回：所有消息的列表。
    """
    # ─── ① 消费式读取 ───
    msgs = BUS.read_inbox("lead")
    if not msgs:
        return []
    # ─── ③ 路由协议响应 ───
    if route_protocol:
        for msg in msgs:
            meta = msg.get("metadata", {})
            req_id = meta.get("request_id", "")
            msg_type = msg.get("type", "")
            # 以 "_response" 结尾的消息 = 协议响应 → 匹配原请求
            if req_id and msg_type.endswith("_response"):
                approve = meta.get("approve", False)
                match_response(msg_type, req_id, approve)
    return msgs


# ═══════════════════════════════════════════════════════════
#  NEW in s16: 队友线程（idle loop + 消息分发）
#
#  与 s15 最大的区别：队友不再 10 轮后退出，而是进入 idle loop。
#  idle loop 等待收件箱中的协议消息（shutdown_request, plan_approval_response），
#  有新消息时重新进入 LLM 轮次。
#
#  消息分发架构（handle_inbox_message）：
#    shutdown_request → BUS.send("shutdown_response") → 返回 True（停止）
#    plan_approval_response → 追加到 messages（批准/拒绝反馈）→ 返回 False（继续）
#    其他消息 → 作为普通收件箱消息注入
# ═══════════════════════════════════════════════════════════

def spawn_teammate_thread(name: str, role: str, prompt: str) -> str:
    """在后台线程创建队友（s16 升级版：idle loop）。

    执行流程（数字编号对应下方代码）：
      ① 防重名检查（active_teammates 里已有同名 → 拒绝）
      ② 组装队友 system prompt（提示存在协议消息）
      ③ 定义消息分发器 handle_inbox_message（按 type 路由）
      ④ 定义线程体 run()：while 循环
         （读收件箱 → 分发协议消息 → LLM 轮次 → 工具分发 → 回写）
      ⑤ 注册 active_teammates → 启动 daemon 线程 → 立即返回

    ★ 与 s15 的关键区别（核心变化）：
      1. idle loop：LLM 完成不再退出，而是等待收件箱消息
         ——队友变成"随时可被 Lead 唤醒"的常驻线程
      2. 消息分发：handle_inbox_message 按 type 路由（协议 vs 普通消息）
      3. shutdown_request → 优雅关闭（回响应 → 退出循环）
      4. submit_plan 工具 → _teammate_submit_plan（协议层面提交计划）
    """
    # ─── ① 防重名检查 ───
    if name in active_teammates:
        return f"Teammate '{name}' already exists"
    # ─── ② 组装队友 system prompt ───
    # 明确告知队友：要检查收件箱里的协议消息（shutdown_request 等）
    system = (f"You are '{name}', a {role}. "
              f"Use tools to complete tasks. "
              f"Check inbox for protocol messages (shutdown_request, etc).")

    def handle_inbox_message(name: str, msg: dict, messages: list) -> bool:
        """队友侧的消息分发器：按 type 路由到不同处理器。

        执行流程（数字编号对应下方代码）：
          ① 提取 type + metadata.request_id
          ② shutdown_request → 回 shutdown_response（带同一 request_id）
             → 返回 True（通知外层停止循环）
          ③ plan_approval_response → 把审批结果注入 messages
             （批准 → "[Plan approved]"，拒绝 → 附带 Lead 的 feedback）
             → 返回 False（继续循环）
          ④ 其他消息 → 返回 False（由外层按普通收件箱处理）

        返回：True = 队友应停止循环，False = 继续。

        ★ 这是"协议在队友侧的执行端"：
          协议的定义在 Lead 侧（match_response 负责收尾），
          这里负责"收到协议请求后的响应动作"。
        """
        # ─── ① 提取消息类型 + 关联键 ───
        msg_type = msg.get("type", "message")
        meta = msg.get("metadata", {})
        req_id = meta.get("request_id", "")

        # ─── ② shutdown_request：握手响应 → 停止 ───
        if msg_type == "shutdown_request":
            # 优雅关闭：回响应（approve=True）→ 返回 True（外层中断循环）
            # ★ request_id 原样带回——Lead 凭它匹配到原始请求
            BUS.send(name, "lead", "Shutting down gracefully.",
                     "shutdown_response",
                     {"request_id": req_id, "approve": True})
            print(f"  \033[35m[protocol] {name} approved shutdown "
                  f"({req_id})\033[0m")
            return True  # stop the loop

        # ─── ③ plan_approval_response：审批结果注入上下文 ───
        if msg_type == "plan_approval_response":
            approve = meta.get("approve", False)
            if approve:
                messages.append({"role": "user",
                    "content": f"[Plan approved] Proceed with the task."})
            else:
                # 拒绝时 Lead 的 feedback 在 content 字段里
                messages.append({"role": "user",
                    "content": f"[Plan rejected] Feedback: {msg['content']}"})

        # ─── ④ 其他消息：交给外层按普通收件箱处理 ───
        return False  # continue

    def run():
        # ── ④a 队友的独立上下文：只有初始 prompt（与 Lead 完全隔离）──
        messages = [{"role": "user", "content": prompt}]

        # 队友的工具集：比 Lead 少很多（只有 5 个）。
        # ★ s16 新增 submit_plan：队友提交计划给 Lead 审批的协议工具。
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
        ]
        sub_handlers = {
            "bash": run_bash, "read_file": run_read, "write_file": run_write,
            # ★ lambda 技巧：BUS.send 返回 None，execute_tool 需要返回值
            #   (BUS.send(...), "Sent")[1] → 调 send 但返回 "Sent" 给 LLM
            "send_message": lambda to, content: (BUS.send(name, to, content),
                                                  "Sent")[1],
            # submit_plan 走协议层（_teammate_submit_plan 会注册 ProtocolState）
            "submit_plan": lambda plan: _teammate_submit_plan(name, plan),
        }

        # ── ④b 队友主循环（s16：不再限 10 轮，改为"一直跑，直到 shutdown"）──
        shutdown_requested = False
        while not shutdown_requested:
            # ④b-1 先读收件箱，区分协议消息和普通消息
            #   协议消息（shutdown_request / plan_approval_response）→ 分发器处理
            #   普通消息 → 收集到 non_protocol，注入上下文让 LLM 看到
            inbox = BUS.read_inbox(name)
            should_stop = False
            non_protocol = []
            for msg in inbox:
                if msg.get("type") in ("shutdown_request", "plan_approval_response"):
                    # 协议消息 → 走分发器（内部处理并返回是否停止）
                    should_stop = handle_inbox_message(name, msg, messages)
                    if should_stop:
                        break     # 跳出消息遍历
                else:
                    non_protocol.append(msg)
            # 协议要求停止 → 退出整个循环
            if should_stop:
                shutdown_requested = True
                break
            # 有普通消息 → 包成 <inbox> 标签注入上下文
            if non_protocol:
                inbox_json = json.dumps(non_protocol)
                messages.append({"role": "user",
                    "content": "<inbox>" + inbox_json + "</inbox>"})

            # ④b-2 LLM 轮次（只保留最近 20 条，控制上下文长度）
            try:
                response = client.messages.create(
                    model=MODEL, system=system, messages=messages[-20:],
                    tools=sub_tools, max_tokens=8000)
            except Exception:
                break  # API 错误 → 退出线程

            messages.append({"role": "assistant", "content": response.content})
            if response.stop_reason != "tool_use":
                # ── ④b-3 IDLE LOOP（s16 核心变化）──
                # LLM 认为任务做完了。s15 在这里 break 退出线程；
                # s16 改为"挂起等待"：每秒轮询收件箱，
                #   收到 shutdown_request → 握手 → 退出
                #   收到普通消息 → 注入上下文 → 回到外层重新 LLM 轮次
                # ★ 这就是"队友常驻"的机制：不退出，等 Lead 唤醒。
                #   （真实 CC 在这里发 idle_notification 通知 Lead"我空了"）
                while not shutdown_requested:
                    time.sleep(1)
                    inbox = BUS.read_inbox(name)
                    if not inbox:
                        continue
                    for msg in inbox:
                        if msg.get("type") in ("shutdown_request", "plan_approval_response"):
                            should_stop = handle_inbox_message(name, msg, messages)
                            if should_stop:
                                shutdown_requested = True
                                break
                        else:
                            non_protocol.append(msg)
                    if shutdown_requested:
                        break  # 跳出消息遍历
                    if non_protocol:
                        inbox_json = json.dumps(non_protocol)
                        messages.append({"role": "user",
                            "content": "<inbox>" + inbox_json + "</inbox>"})
                        break  # 有新消息 → 回到外层继续 LLM 轮次

            # ④b-4 工具分发（同步执行，结果回写）
            results = []
            for block in response.content:
                if block.type == "tool_use":
                    handler = sub_handlers.get(block.name)
                    output = handler(**block.input) if handler else "Unknown"
                    results.append({"type": "tool_result",
                                    "tool_use_id": block.id,
                                    "content": str(output)})
            messages.append({"role": "user", "content": results})

        # ── ④c 收尾：提取最终摘要 → 发回 Lead → 移除注册 ──
        # 摘要提取：从最后一条 assistant 消息往回找第一条 text block。
        #   默认 "Done."，找到文本就用文本覆盖。
        #   for...else 语法：内层 for 正常跑完（没 break）才执行 else，
        #   这里内层找不到 text 就 continue 外层继续往前翻。
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
        BUS.send(name, "lead", summary, "result")
        active_teammates.pop(name, None)  # 从注册表移除（线程已结束）
        print(f"  \033[32m[teammate] {name} finished\033[0m")

    # ─── ⑤ 注册 + 启动线程 → 立即返回（不阻塞 Lead）───
    active_teammates[name] = True
    threading.Thread(target=run, daemon=True).start()
    print(f"  \033[36m[teammate] {name} spawned as {role}\033[0m")
    return f"Teammate '{name}' spawned as {role}"


def _teammate_submit_plan(from_name: str, plan: str) -> str:
    """队友提交计划给 Lead 审批（协议请求的发起端）。

    执行流程（数字编号对应下方代码）：
      ① 生成 request_id
      ② 注册 ProtocolState（type="plan_approval", status="pending"）
      ③ BUS.send 发送 "plan_approval_request"（带 request_id）
      ④ 返回提示文本

    ★ 重要设计：这是协议级请求——不是代码级的门控。
      提交后队友线程继续运行，仍可调用 bash/write 等工具。
      真正的执行控制依赖"模型在看到审批前主动等待"，
      而非代码层锁住工具分发。教学版省略了生产级的工具门控。

    参数 from_name：提交计划的队友名。
    参数 plan：计划文本（存进 payload）。
    """
    # ─── ① 生成 request_id ───
    req_id = new_request_id()
    # ─── ② 注册状态（pending → 等待 Lead 审批）───
    pending_requests[req_id] = ProtocolState(
        request_id=req_id, type="plan_approval",
        sender=from_name, target="lead",
        status="pending", payload=plan)
    # ─── ③ 发送协议请求 ───
    BUS.send(from_name, "lead", plan,
             "plan_approval_request",
             {"request_id": req_id})
    return f"Plan submitted ({req_id}). Waiting for approval..."


# ═══════════════════════════════════════════════════════════
#  Lead 协议工具（s16 新增）
#
#  三个新工具让 Lead 可以通过协议与队友交互：
#    request_shutdown → 发送关闭请求 → 等待 shutdown_response
#    request_plan → 要求队友提交计划
#    review_plan → 批准或拒绝队友提交的计划
# ═══════════════════════════════════════════════════════════

def run_request_shutdown(teammate: str) -> str:
    """Lead 发起关机协议请求（协议请求的 Lead 侧发起端）。

    执行流程（数字编号对应下方代码）：
      ① 生成 request_id
      ② 注册 ProtocolState（type="shutdown", status="pending"）
      ③ BUS.send 发送 "shutdown_request"（带 request_id）
      ④ 返回提示（含 req_id，Lead 可据此追踪）

    ★ 与 _teammate_submit_plan 结构完全对称：
      都是"注册 pending 状态 + 发 request"，区别只在 type 和消息名。
    """
    # ─── ① 生成 request_id ───
    req_id = new_request_id()
    # ─── ② 注册状态 ───
    pending_requests[req_id] = ProtocolState(
        request_id=req_id, type="shutdown",
        sender="lead", target=teammate,
        status="pending", payload="")
    # ─── ③ 发送协议请求 ───
    BUS.send("lead", teammate, "Please shut down gracefully.",
             "shutdown_request",
             {"request_id": req_id})
    print(f"  \033[35m[protocol] shutdown_request → {teammate} "
          f"({req_id})\033[0m")
    return f"Shutdown request sent to {teammate} (req: {req_id})"


def run_request_plan(teammate: str, task: str) -> str:
    """Lead 要求队友提交计划。

    ★ 注意：这条用的是普通 message 类型，不是协议消息——
      它只是"口头要求队友用 submit_plan 工具"，
      真正的协议请求由队友的 submit_plan 工具发起（_teammate_submit_plan）。
    """
    BUS.send("lead", teammate, f"Please submit a plan for: {task}",
             "message")
    return f"Asked {teammate} to submit a plan"


def run_review_plan(request_id: str, approve: bool, feedback: str = "") -> str:
    """Lead 审批队友提交的计划（协议请求的 Lead 侧响应端）。

    执行流程（数字编号对应下方代码）：
      ① 凭 request_id 查状态 → 不存在或已处理 → 返回错误
      ② 更新状态为 approved / rejected
      ③ BUS.send 回 "plan_approval_response"（带 request_id + approve）
      ④ 返回结果消息

    ★ 闭环：队友 submit_plan（注册 pending）→ Lead review_plan（改状态 + 回响应）
      → 队友 handle_inbox_message 收到响应注入上下文。
      整个审批链路由 request_id 串联。

    参数 request_id：_teammate_submit_plan 生成的请求 ID。
    参数 approve：True=批准，False=拒绝。
    参数 feedback：拒绝时的反馈信息（队友会看到）。
    """
    # ─── ① 查状态 ───
    state = pending_requests.get(request_id)
    if not state:
        return f"Request {request_id} not found"
    if state.status != "pending":
        return f"Request {request_id} already {state.status}"
    # ─── ② 更新状态机 ───
    state.status = "approved" if approve else "rejected"
    # ─── ③ 回响应给提交方 ───
    BUS.send("lead", state.sender, feedback or ("Approved" if approve else "Rejected"),
             "plan_approval_response",
             {"request_id": request_id, "approve": approve})
    icon = "✓" if approve else "✗"
    print(f"  \033[32m[protocol] plan {icon} ({request_id})\033[0m")
    return f"Plan {'approved' if approve else 'rejected'} ({request_id})"


# ── Other Lead Tool Handlers ──

def run_spawn_teammate(name: str, role: str, prompt: str) -> str:
    """工具层：spawn_teammate 直接委托给 spawn_teammate_thread。"""
    return spawn_teammate_thread(name, role, prompt)


def run_send_message(to: str, content: str) -> str:
    """工具层：Lead 发普通消息给队友。"""
    BUS.send("lead", to, content)
    return f"Sent to {to}"


def run_check_inbox() -> str:
    """工具层：检查 Lead 收件箱（s16 统一走 consume_lead_inbox）。

    ★ s16 关键点：这里调用 consume_lead_inbox 而非直接 BUS.read_inbox。
      因为收件箱里可能混有协议响应消息（shutdown_response 等）——
      统一入口会先路由协议（match_response 更新状态机），再返回全部消息。
      这样 LLM 主动 check_inbox 时，协议状态也不会丢。
    """
    msgs = consume_lead_inbox(route_protocol=True)
    if not msgs:
        return "(inbox empty)"
    lines = []
    for m in msgs:
        meta = m.get("metadata", {})
        req_id = meta.get("request_id", "")
        # 协议消息带上类型 + req_id 标签，便于 LLM 区分
        tag = f" [{m['type']} req:{req_id}]" if req_id else f" [{m['type']}]"
        lines.append(f"  [{m['from']}]{tag} {m['content'][:200]}")
    return "\n".join(lines)

# ═══════════════════════════════════════════════════════════
#  工具分发（execute_tool）—— 供后台线程和 agent_loop 共用
# ═══════════════════════════════════════════════════════════

# ── Tool Dispatch ──

def execute_tool(block) -> str:
    """执行一个工具调用 block，返回输出字符串。

    执行流程（数字编号对应下方代码）：
      ① 按 block.name 查 handler 映射表（字典分发）
      ② 命中 → handler(**block.input) 调用，返回输出字符串
      ③ 未命中 → 返回 "Unknown tool"

    ★ **block.input 解包：把 LLM 返回的工具参数 dict 展开成
      关键字参数传给 handler（如 run_bash(command="ls")）。

    ★ 分发表 = 工具注册中心：s16 在 s15 基础上新增了
      request_shutdown / request_plan / review_plan 三个协议工具。
    """
    # ─── ① 字典分发（策略模式）───
    handler = {
        "bash": run_bash, "read_file": run_read, "write_file": run_write,
        "create_task": run_create_task, "list_tasks": run_list_tasks,
        "get_task": run_get_task, "claim_task": run_claim_task,
        "complete_task": run_complete_task,
        "spawn_teammate": run_spawn_teammate,
        "send_message": run_send_message, "check_inbox": run_check_inbox,
        # ── s16 新增：3 个协议工具 ──
        "request_shutdown": run_request_shutdown,
        "request_plan": run_request_plan, "review_plan": run_review_plan,
    }.get(block.name)
    # ─── ② 命中 → 调用 handler ───
    if handler:
        return handler(**block.input)
    # ─── ③ 未命中 ───
    return f"Unknown tool: {block.name}"

# ═══════════════════════════════════════════════════════════
#  工具定义（TOOLS）—— 发送给 LLM 的 JSON Schema
#  15 个工具：3 基础 + 5 任务 + 4 团队 + 3 协议
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
    {"name": "spawn_teammate",
     "description": "Spawn a teammate agent in a background thread.",
     "input_schema": {"type": "object",
                      "properties": {
                          "name": {"type": "string"},
                          "role": {"type": "string"},
                          "prompt": {"type": "string"}},
                      "required": ["name", "role", "prompt"]}},
    {"name": "send_message",
     "description": "Send message to a teammate via MessageBus.",
     "input_schema": {"type": "object",
                      "properties": {"to": {"type": "string"},
                                     "content": {"type": "string"}},
                      "required": ["to", "content"]}},
    {"name": "check_inbox",
     "description": "Check Lead's inbox. Routes protocol responses automatically.",
     "input_schema": {"type": "object", "properties": {},
                      "required": []}},
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
     "description": "Approve or reject a submitted plan by request_id.",
     "input_schema": {"type": "object",
                      "properties": {
                          "request_id": {"type": "string"},
                          "approve": {"type": "boolean"},
                          "feedback": {"type": "string"}},
                      "required": ["request_id", "approve"]}},
]

# ═══════════════════════════════════════════════════════════
#  上下文 + agent_loop
# ═══════════════════════════════════════════════════════════

# ── Context ──

def update_context(context: dict, messages: list) -> dict:
    """从真实状态派生 context（供 system prompt 组装用）。

    从 .memory/MEMORY.md 读记忆文本（s09 的持久化记忆在此注入）。
    返回包含三块的 context：
      enabled_tools：当前启用的工具名列表（让 LLM 知道有什么工具）
      workspace：工作目录
      memories：记忆内容（非空才追加到 system prompt）
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


# ── Agent Loop ──

def agent_loop(messages: list, context: dict):
    """Agent 核心循环（s16 版本）。

    执行流程（数字编号对应下方代码）：
      ① 首次进入：用当前 context 组装 system prompt
      ② 调用 LLM（带 tools + system prompt）
      ③ stop_reason != "tool_use"？→ 是则结束本轮 agent_loop
      ④ 遍历 tool_use blocks：后台 → start_background_task；同步 → execute_tool
      ⑤ collect_background_results → 注入 <task_notification>
      ⑥ 结果回写 messages → 更新 context/system → 回到 ②

    ★ 与 s15 的区别：
      1. 没有 cron 消费（s16 去掉了 s15 的 s14 cron 机制，聚焦团队协议）
      2. 协议响应处理不在 agent_loop 里——在主循环的
         consume_lead_inbox()（agent_loop 返回后统一处理）
      3. 其余骨架与 s01 核心循环完全一致（循环不可变原则）
    """
    # ─── ① 组装 system prompt ───
    system = get_system_prompt(context)
    while True:
        # ─── ② 调用 LLM ───
        try:
            response = client.messages.create(
                model=MODEL, system=system, messages=messages,
                tools=TOOLS, max_tokens=8000)
        except Exception as e:
            # API 错误：记录错误消息后退出（不重试、不降级）
            messages.append({"role": "assistant", "content": [
                {"type": "text",
                 "text": f"[Error] {type(e).__name__}: {e}"}]})
            return

        # ─── ③ 检查停止条件 ───
        messages.append({"role": "assistant", "content": response.content})
        if response.stop_reason != "tool_use":
            return   # LLM 没有请求工具 → 本轮对话结束

        # ─── ④ 工具分发 ───
        results = []
        for block in response.content:
            if block.type != "tool_use":
                continue
            print(f"\033[36m> {block.name}\033[0m")

            # ④a 后台路径：慢操作 → 异步执行，立即返回 bg_id
            if should_run_background(block.name, block.input):
                bg_id = start_background_task(block)
                results.append({"type": "tool_result",
                                "tool_use_id": block.id,
                                "content": f"[Background task {bg_id} started] "
                                           f"Result will be available when complete."})
            # ④b 同步路径：直接执行（含协议工具 request_shutdown/review_plan 等）
            else:
                output = execute_tool(block)
                print(str(output)[:300])
                results.append({"type": "tool_result",
                                "tool_use_id": block.id,
                                "content": output})

        # ─── ⑤ 收集已完成后台任务 → 追加通知 ───
        user_content = list(results)
        bg_notifications = collect_background_results()
        if bg_notifications:
            for notif in bg_notifications:
                user_content.append({"type": "text", "text": notif})

        # ─── ⑥ 结果回写 → 更新 context → 回到 ② ───
        messages.append({"role": "user", "content": user_content})
        context = update_context(context, messages)
        system = get_system_prompt(context)

# ═══════════════════════════════════════════════════════════
#  主入口
#
#  s16 主循环在 agent_loop 返回后调用 consume_lead_inbox，
#  确保协议响应被 match_response 处理 + 注入到 history。
# ═══════════════════════════════════════════════════════════

if __name__ == "__main__":
    print("s16: team protocols")
    print("Enter a question, press Enter to send. Type q to quit.\n")
    history = []
    context = update_context({}, [])

    # ★ s16 主循环 vs s15：从事件驱动回到简单同步循环。
    #   s15 需要 input_reader/inbox_poller 两个线程 + queue.Queue，
    #   因为队友和后台任务要"主动唤醒"Lead。
    #   s16 简化的原因：协议响应都是"队友发完就放收件箱"，
    #   Lead 每轮 agent_loop 返回后统一 consume_lead_inbox 处理即可，
    #   不需要专门的事件线程去轮询唤醒。（队友线程仍是 daemon 自己跑）
    while True:
        # ─── ① 读取用户输入 ───
        try:
            query = input("\033[36ms16 >> \033[0m")
        except (EOFError, KeyboardInterrupt):
            break
        if query.strip().lower() in ("q", "exit", ""):
            break
        history.append({"role": "user", "content": query})

        # ─── ② 一轮 agent_loop（内部跑完整个"LLM ↔ 工具"循环）───
        agent_loop(history, context)
        context = update_context(context, history)
        # 打印 agent_loop 最后一条 assistant 消息的文本
        for block in history[-1]["content"]:
            if getattr(block, "type", None) == "text":
                print(block.text)
            elif isinstance(block, dict) and block.get("type") == "text":
                print(block.get("text", ""))

        # ─── ③ 统一处理收件箱（s16 关键）───
        # 先路由协议响应（match_response 更新状态机），再注入 history，
        # 让 LLM 下一轮能看到队友的回复（含关机确认/计划审批结果）。
        inbox_msgs = consume_lead_inbox(route_protocol=True)
        if inbox_msgs:
            inbox_text = "\n".join(
                f"From {m['from']}: {m['content'][:200]}" for m in inbox_msgs)
            history.append({"role": "user",
                            "content": f"[Inbox]\n{inbox_text}"})
            print(f"\n\033[33m[Inbox: {len(inbox_msgs)} messages injected]\033[0m")
        print()
