#!/usr/bin/env python3
"""
s15: Agent 团队 — MessageBus + spawn_teammate_thread + 收件箱注入。

Run:  python s15_agent_teams/demo_code.py
Need: pip install anthropic python-dotenv + .env with ANTHROPIC_API_KEY

Changes from s14:
  - MessageBus 类：基于文件的邮箱系统（.mailboxes/*.jsonl）
  - spawn_teammate_thread：在后台线程中创建队友 Agent
  - 队友运行简化的 agent_loop（bash, read, write, send_message）
  - Lead 工具：spawn_teammate, send_message, check_inbox（3 个新增）
  - Lead 收件箱：队友消息注入到对话历史（不只是打印）
  - 教学版：队友限制 10 轮（真实 CC 使用 idle loop）

ASCII 流程:
  Lead: cron_queue → messages → prompt → LLM → TOOLS ────→ loop
                ↑                     ↓                        |
                └── inbox ← MessageBus ← teammate.send_message ←┘
  Teammate: inbox → LLM → bash/read/write/send → loop (max 10 turns)
"""

# ======================================================================
# 执行流程（入口 → 结束）
# ======================================================================
#
#   1. 启动 → 加载持久化 cron → 启动调度线程 → 初始化 MessageBus
#
#   2. 启动两个后台线程：
#      input_reader：阻塞等待用户输入 → 放入 events 队列
#      inbox_poller：每 1 秒检查收件箱 + 后台任务 → 放入 events 队列
#
#   3. 主循环从 events 队列消费：
#
#      kind == "user"：
#        → 追加 user 消息到 history
#
#      kind == "wake"（队友消息或后台任务完成）：
#        → BUS.read_inbox("lead") 读取队友消息
#        → collect_background_results() 收集后台通知
#        → 合并为一条 user 消息注入 history
#
#      kind == "quit"：
#        → 程序退出
#
#   4. 无论哪种唤醒来源，都执行一轮 agent_loop(history, context)
#
#   5. agent_loop 核心循环：
#      a. 消费 cron_queue → 注入 [Scheduled] 消息
#      b. 调用 LLM（TOOLS 含 14 个工具，含 team 工具）
#      c. 遍历 tool_use：
#         后台？→ start_background_task
#         同步？→ execute_tool
#            【s15 新增】spawn_teammate → 创建队友线程
#            【s15 新增】send_message → BUS.send("lead", to, content)
#            【s15 新增】check_inbox → BUS.read_inbox("lead")
#      d. collect_background_results + 队友通知 → 注入
#      e. 结果回写 → 回到步骤 a
#
#   6. 队友线程（独立运行）：
#      a. 创建 messages = [{"role": "user", "content": prompt}]
#      b. 自己的 while 循环（最多 10 轮）
#      c. 每轮读自己的收件箱（BUS.read_inbox(name)）
#      d. 调用 LLM → sub_handlers 分发 → 结果回写
#      e. 完成后 BUS.send(name, "lead", summary) → 退出
# ======================================================================

import os, subprocess, json, time, random, threading, queue
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
MEMORY_INDEX = MEMORY_DIR / "MEMORY.md"     # 记忆索引
client = Anthropic(base_url=os.getenv("ANTHROPIC_BASE_URL"))  # Anthropic 客户端
MODEL = os.environ["MODEL_ID"]              # 模型 ID


# ═══════════════════════════════════════════════════════════
#  任务系统（s12 引入）
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
    owner: str | None    # 执行者标识（团队场景下为 teammate 名称）
    blockedBy: list[str] # 依赖任务 ID 列表

def _task_path(task_id: str) -> Path:
    # 任务 ID → 对应文件路径。所有 CRUD 都走这个函数拿路径，
    # 保证"任务存储位置"只在一处定义（单一事实来源）。
    return TASKS_DIR / f"{task_id}.json"

def create_task(subject: str, description: str = "",
                blockedBy: list[str] | None = None) -> Task:
    """创建任务。

    执行流程：
      ① 生成唯一 id（时间戳 + 4 位随机数）
      ② 构造 Task 对象（blockedBy 规范化）
      ③ save_task 落盘 → 返回
    """
    # ─── ① 生成 id ───
    # id 生成：时间戳（int(time.time())）+ 4 位随机数。
    # 时间戳保证"不同秒创建必然不同"，随机数处理"同一秒创建多个"的冲突。
    # 简单但够用——真实 CC 用顺序 ID + highwatermark 文件，更严谨。
    # ─── ② 构造 Task ───
    task = Task(
        id=f"task_{int(time.time())}_{random.randint(0, 9999):04d}",
        subject=subject, description=description,
        status="pending", owner=None,
        # blockedBy or []：LLM 可能传 None 或空列表，统一成空列表。
        # 注意 dataclass 里 blockedBy: list[str] 是可变默认值陷阱——
        # 这里没用默认参数 `blockedBy: list[str] = []`（那样所有实例共享同一个 list），
        # 而是用 `= None` + 函数内 `or []`，每实例独立列表。这是 Python 最佳实践。
        blockedBy=blockedBy or [],
    )
    # ─── ③ 落盘 ───
    save_task(task)
    return task

def save_task(task: Task):
    # asdict(task)：把 dataclass 转成普通 dict（{id, subject, ...}）
    # json.dumps(..., indent=2)：转成格式化的 JSON 字符串（缩进 2 空格，方便人看）
    _task_path(task.id).write_text(json.dumps(asdict(task), indent=2))

def load_task(task_id: str) -> Task:
    # ★ Task(**json.loads(...)) 是两个反向操作的组合：
    #   json.loads(file)       → 把 JSON 文本还原成 dict
    #   Task(**dict)           → 把 dict 按关键字解包成 dataclass 实例
    #   ** 是"关键字解包"：{"id": "x", "subject": "y"} → Task(id="x", subject="y")
    return Task(**json.loads(_task_path(task_id).read_text()))

def list_tasks() -> list[Task]:
    # glob("task_*.json")：匹配目录下所有以 task_ 开头、.json 结尾的文件
    # sorted(...)：按文件名排序（因为 id 含时间戳，文件名排序≈创建时间排序）
    return [Task(**json.loads(p.read_text()))
            for p in sorted(TASKS_DIR.glob("task_*.json"))]

def get_task(task_id: str) -> str:
    """Return full task details as JSON."""
    task = load_task(task_id)
    return json.dumps(asdict(task), indent=2)

def can_start(task_id: str) -> bool:
    """Check if all blockedBy dependencies are completed.
    Missing dependencies are treated as blocked."""
    task = load_task(task_id)
    for dep_id in task.blockedBy:
        # ★ 缺失依赖视为 blocked（不是崩溃）：
        #   如果依赖 ID 写错了（不存在的任务），返回 False 表示"不能开工"。
        #   而不是抛 FileNotFoundError——错误引用不能炸掉整个流程。
        if not _task_path(dep_id).exists():
            return False
        if load_task(dep_id).status != "completed":
            return False
    # 没有依赖（blockedBy 为空）时循环直接跳过，返回 True（可以开工）
    return True

def claim_task(task_id: str, owner: str = "agent") -> str:
    """认领任务：pending → in_progress，并记录 owner。

    执行流程（数字编号对应下方代码）：
      ① 加载任务
      ② 校验状态：必须还是 pending（in_progress/completed 不能再认领）
      ③ 校验依赖：所有 blockedBy 必须已完成（can_start）
      ④ 通过校验 → 设 owner + 改状态 → 写磁盘
    在团队场景下 owner 参数用于标记是哪个队友在执行。
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
    # ─── ④ 认领并落盘 ───
    task.owner = owner
    task.status = "in_progress"
    save_task(task)
    print(f"  \033[36m[claim] {task.subject} → in_progress (owner: {owner})\033[0m")
    return f"Claimed {task.id} ({task.subject})"

def complete_task(task_id: str) -> str:
    """完成任务：in_progress → completed，并发现被解除阻塞的任务。

    执行流程（数字编号对应下方代码）：
      ① 加载任务
      ② 状态校验：必须 in_progress
      ③ 改状态 + 写磁盘
      ④ 级联发现：找出被本次完成"解锁"的 pending 任务
      ⑤ 返回结果（含被解锁列表）

    完成一个任务后，遍历所有 pending 任务，
    找出那些 blockedBy 全部已完成的——它们现在可以开工了。
    这个"级联发现"是依赖图的核心价值。
    """
    # ─── ①② 加载 + 校验 ───
    task = load_task(task_id)
    if task.status != "in_progress":
        return f"Task {task_id} is {task.status}, cannot complete"
    # ─── ③ 完成并落盘 ───
    task.status = "completed"
    save_task(task)
    # ─── ④ 级联发现：哪些 pending 任务现在被解除了阻塞 ───
    unblocked = [t.subject for t in list_tasks()
                 if t.status == "pending" and t.blockedBy and can_start(t.id)]
    print(f"  \033[32m[complete] {task.subject} ✓\033[0m")
    # ─── ⑤ 返回结果 ───
    msg = f"Completed {task.id} ({task.subject})"
    if unblocked:
        msg += f"\nUnblocked: {', '.join(unblocked)}"
        print(f"  \033[33m[unblocked] {', '.join(unblocked)}\033[0m")
    return msg


# ═══════════════════════════════════════════════════════════
#  提示词组装系统（s10 引入）
#
#  s15 的 tools 片段包含 14 个工具（新增 3 个 team 工具）。
# ═══════════════════════════════════════════════════════════

PROMPT_SECTIONS = {
    "identity": "You are a coding agent. Act, don't explain.",
    "tools": "Available tools: bash, read_file, write_file, "
             "get_task, create_task, list_tasks, claim_task, complete_task, "
             "schedule_cron, list_crons, cancel_cron, "
             "spawn_teammate, send_message, check_inbox.",
    "workspace": f"Working directory: {WORKDIR}",
    "memory": "Relevant memories are injected below when available.",
}

def assemble_system_prompt(context: dict) -> str:
    # 按固定顺序拼 system prompt：identity → tools → workspace。
    # 顺序有意义：身份放最前（模型先知道"我是谁"），工具列表次之。
    # memories 是"条件加载"——context 里有记忆才追加，没有就跳过。
    sections = [PROMPT_SECTIONS["identity"],
                PROMPT_SECTIONS["tools"],
                PROMPT_SECTIONS["workspace"]]
    memories = context.get("memories", "")
    if memories:
        sections.append(f"Relevant memories:\n{memories}")
    # "\n\n".join(...)：片段之间用空行分隔，避免拼接后粘连
    return "\n\n".join(sections)

_last_context_key, _last_prompt = None, None

def get_system_prompt(context: dict) -> str:
    # ★ 确定性缓存：context 没变就不重新组装 system prompt。
    #   key = json.dumps(context, sort_keys=True)
    #     sort_keys=True：dict 键排序，保证 {"a":1,"b":2} 和 {"b":2,"a":1} 产生相同 key
    #     ensure_ascii=False：中文不转成 \uXXXX，否则同一个词可能因编码不同导致缓存 miss
    #     default=str：context 里可能有 Path/datetime 等非 JSON 类型，转成字符串兜底
    #   为什么缓存：agent_loop 每轮都会调 get_system_prompt，如果 context 没变
    #   （比如 memories 文件没更新），重复组装就是纯浪费。缓存命中直接返回旧值。
    global _last_context_key, _last_prompt
    key = json.dumps(context, sort_keys=True, ensure_ascii=False, default=str)
    if key == _last_context_key and _last_prompt:
        return _last_prompt
    _last_context_key = key
    _last_prompt = assemble_system_prompt(context)
    return _last_prompt


# ═══════════════════════════════════════════════════════════
#  工具实现（3 个标准 + 5 个任务 + 3 个 cron）
# ═══════════════════════════════════════════════════════════

def safe_path(p: str) -> Path:
    # resolve() 会把相对路径转绝对路径，还会解析符号链接（symlink）。
    # 这很关键：如果用户用 `read_file` 读 "link_to_etc/passwd"，
    # resolve() 后变成 /etc/passwd，is_relative_to(WORKDIR) 返回 False → 拦截。
    # 不加 resolve() 的话，symlink 可以绕过目录检查（TOCTOU 类漏洞）。
    path = (WORKDIR / p).resolve()
    if not path.is_relative_to(WORKDIR):
        raise ValueError(f"Path escapes workspace: {p}")
    return path

def run_bash(command: str, run_in_background: bool = False) -> str:
    """执行 Shell 命令。run_in_background 由 agent_loop 层处理。

    执行流程（数字编号对应下方代码）：
      ① 调 subprocess.run 执行命令（shell + 捕获输出 + 120s 超时）
      ② 合并 stdout 和 stderr
      ③ 截断到 50000 字符返回
      ④ 超时 → 返回错误信息
    """
    try:
        # ─── ① 执行命令 ───
        # subprocess.run 参数说明：
        #   shell=True    → 用系统 shell 执行（支持管道、通配符等）
        #   capture_output → 同时捕获 stdout 和 stderr
        #   text=True     → 输出按文本处理（而非 bytes）
        #   timeout=120   → 120 秒超时，防止命令挂死
        # ★ run_in_background 参数在这里"没用到"：
        #   TOOLS schema 里有它，是给 LLM 传信号用的（声明"这个命令我想后台跑"）。
        #   agent_loop 层读到这个参数后决定走 start_background_task 分支，
        #   走到 execute_tool 时已经是后台 worker 线程里，同步执行即可。
        #   所以 run_bash 本体不需要管它，函数签名里保留只为吸收 **block.input。
        r = subprocess.run(command, shell=True, cwd=WORKDIR, encoding='gbk',
                           capture_output=True, text=True, timeout=120)
        # ─── ② 合并 stdout + stderr ───
        # 避免只看到一半（报错通常进 stderr，合并后能看到完整报错）
        out = (r.stdout + r.stderr).strip()
        # ─── ③ 截断返回 ───
        # 截断到 50000 字符：防止超大输出撑爆上下文
        return out[:50000] if out else "(no output)"
    except subprocess.TimeoutExpired:
        # ─── ④ 超时兜底 ───
        return "Error: Timeout (120s)"

def run_read(path: str, limit: int | None = None) -> str:
    """读取文件内容。

    执行流程：
      ① 校验路径安全 → 读文件按行拆分
      ② limit 存在且超过 → 截断 + 追加提示
      ③ 行列表拼回字符串返回
    """
    try:
        # ─── ① 读文件 ───
        lines = safe_path(path).read_text().splitlines()
        # ─── ② 按 limit 截断 ───
        # limit 限制读取行数：读大文件时防止全部塞进上下文。
        # 超过的部分不丢弃，而是追加一行提示"还有 N 行"，告诉 LLM 文件没读完。
        if limit and limit < len(lines):
            lines = lines[:limit] + [f"... ({len(lines) - limit} more lines)"]
        # ─── ③ 拼回字符串 ───
        return "\n".join(lines)
    except Exception as e:
        return f"Error: {e}"


def run_write(path: str, content: str) -> str:
    """写入文件（覆盖写，自动创建父目录）。

    执行流程：
      ① 校验路径安全
      ② 自动创建不存在的父目录
      ③ write_text 覆盖写入
    """
    try:
        # ─── ① 校验路径 ───
        fp = safe_path(path)
        # ─── ② 创建父目录 ───
        # parents=True：自动创建不存在的父目录（如 write_file("a/b/c.txt")，
        # 且 a/、a/b/ 不存在时，这里会一次性全建出来）
        # exist_ok=True：父目录已存在也不报错
        fp.parent.mkdir(parents=True, exist_ok=True)
        # ─── ③ 覆盖写入 ───
        # write_text 是"覆盖写"：文件已有内容会被完全替换（不是追加）
        fp.write_text(content)
        return f"Wrote {len(content)} bytes to {path}"
    except Exception as e:
        return f"Error: {e}"


# 任务工具包装
# ★ run_* 是"包装函数"模式：核心逻辑（create_task 等）与工具入口分离。
#   核心函数返回对象/处理业务，run_* 负责把结果格式化成字符串给 LLM。
#   TOOLS 里的 handler 全部指向 run_*，而不是核心函数——因为 handler 必须返回
#   str（tool_result 内容），而核心函数可能返回 Task 对象。
def run_create_task(subject: str, description: str = "",
                    blockedBy: list[str] | None = None) -> str:
    task = create_task(subject, description, blockedBy)
    # 三元表达式：blockedBy 非空才拼接 " (blockedBy: xxx)" 后缀
    deps = f" (blockedBy: {', '.join(blockedBy)})" if blockedBy else ""
    print(f"  \033[34m[create] {task.subject}{deps}\033[0m")
    return f"Created {task.id}: {task.subject}{deps}"

def run_list_tasks() -> str:
    tasks = list_tasks()
    if not tasks:
        return "No tasks. Use create_task to add some."
    lines = []
    for t in tasks:
        # 状态图标映射：pending=○ in_progress=● completed=✓。
        # .get(t.status, "?")：未知状态显示 "?"（防御性，不崩溃）
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
        # 防御：查询不存在的任务不抛异常，给 LLM 返回友好错误
        return f"Error: Task {task_id} not found"


def run_claim_task(task_id: str) -> str:
    # 教学版固定 owner="agent"（团队场景下应传队友名，这里简化）
    return claim_task(task_id, owner="agent")

def run_complete_task(task_id: str) -> str:
    return complete_task(task_id)


# ═══════════════════════════════════════════════════════════
#  后台任务系统（s13 引入）
# ═══════════════════════════════════════════════════════════

_bg_counter = 0
background_tasks: dict[str, dict] = {}
background_results: dict[str, str] = {}
background_lock = threading.Lock()

def is_slow_operation(tool_name: str, tool_input: dict) -> bool:
    """启发式：install/build/test/deploy 等关键词 → 可能是慢操作。"""
    # 只对 bash 做启发式（非 bash 工具不可能是慢操作）
    if tool_name != "bash":
        return False
    # .lower()：命令转小写再匹配，避免大小写问题（"PIP INSTALL" 也能命中）
    cmd = tool_input.get("command", "").lower()
    slow_keywords = ["install", "build", "test", "deploy", "compile",
                     "docker build", "pip install", "npm install",
                     "cargo build", "pytest", "make"]
    # any(...)：任一关键词出现在命令里就判定为慢操作
    # 这是"误判率可控"的粗略启发式——宁可把慢命令当后台，也不阻塞主循环
    return any(kw in cmd for kw in slow_keywords)

def should_run_background(tool_name: str, tool_input: dict) -> bool:
    """LLM 显式声明 或 启发式检测 → 后台执行。"""
    # ★ 判定优先级：LLM 显式声明 > 启发式。
    #   LLM 主动设 run_in_background=True（比如它自己知道这命令耗时长），
    #   这个意图优先——即使命令不包含慢关键词也后台。
    if tool_input.get("run_in_background"):
        return True
    return is_slow_operation(tool_name, tool_input)

def execute_tool(block) -> str:
    """执行工具调用 block（供后台线程和 agent_loop 共用）。

    执行流程（数字编号对应下方代码）：
      ① 按 block.name 查 handler 映射表（字典分发）
      ② 命中 → handler(**block.input) 调用，返回输出字符串
      ③ 未命中 → 返回 "Unknown tool"

    包含全部 14 个工具的内联映射——Lead 的全部工具集。
    s15 新增的 3 个 team 工具也在这里：
      - spawn_teammate → run_spawn_teammate（创建队友线程，立即返回）
      - send_message  → run_send_message（写队友邮箱）
      - check_inbox   → run_check_inbox（读 Lead 邮箱）
    """
    # ─── ① 字典分发（策略模式）───
    #   .get(block.name) 返回名称对应的 handler；不存在返回 None。
    #   block.name 是 LLM 选定的工具名（"bash"/"send_message" 等），
    #   block.input 是 LLM 填的参数 dict。
    handler = {
        "bash": run_bash, "read_file": run_read, "write_file": run_write,
        "create_task": run_create_task, "list_tasks": run_list_tasks,
        "get_task": run_get_task, "claim_task": run_claim_task,
        "complete_task": run_complete_task,
        "schedule_cron": run_schedule_cron, "list_crons": run_list_crons,
        "cancel_cron": run_cancel_cron,
        "spawn_teammate": run_spawn_teammate,
        "send_message": run_send_message, "check_inbox": run_check_inbox,
    }.get(block.name)
    # ─── ② 命中 → 调用 handler ───
    if handler:
        # handler(**block.input)：** 把 dict 展开成关键字参数调用，
        #   例：{"path": "a.txt", "limit": 5} → handler(path="a.txt", limit=5)。
        # ★ 副作用：如果 TOOLS 里定义了某参数但 handler 没接收，会 TypeError。
        #   所以 run_bash 才保留没用的 run_in_background 参数——吸收多余参数。
        return handler(**block.input)
    # ─── ③ 未命中 → 报错 ───
    return f"Unknown tool: {block.name}"

def start_background_task(block) -> str:
    """在守护线程中启动工具执行，立即返回 bg_id。

    执行流程（数字编号对应下方代码）：
      ① 分配唯一 bg_id（全局计数器 + 4 位序号）
      ② 取命令文本做显示用（bash 用 command，其他工具用工具名兜底）
      ③ 定义 worker：后台线程执行 execute_tool，完成后写结果（持锁）
      ④ 先注册任务（running 状态），再启动 worker 线程
      ⑤ 立即返回 bg_id——agent_loop 不等结果，继续处理下一件事
    """
    # ─── ① 分配 bg_id ───
    global _bg_counter
    _bg_counter += 1
    bg_id = f"bg_{_bg_counter:04d}"

    # ─── ② 取显示用命令 ───
    # block.input.get("command", block.name)：
    #   优先取命令文本（bash 工具才有 command），
    #   其他工具（如 read_file）没有 command，就用工具名兜底
    cmd = block.input.get("command", block.name)

    # ─── ③ 定义 worker 线程体（稍后 start）───
    def worker():
        # ③a 执行工具——这期间 agent_loop 不阻塞（独立线程）
        result = execute_tool(block)
        # ③b 写结果：两处共享数据结构在同一把锁下更新
        with background_lock:
            #   background_tasks[bg_id]["status"] = "completed" 标记完成
            #   background_results[bg_id] = result 存入结果
            # 锁保证"状态和结果成对出现"——collect 不会读到 completed 但没结果的状态
            background_tasks[bg_id]["status"] = "completed"
            background_results[bg_id] = result

    # ─── ④ 先注册再启动 ───
    # 顺序重要：如果先 start() 后注册，worker 可能在注册前就执行完，
    # 然后 background_tasks[bg_id]["status"] 会 KeyError（字典里还没这项）。
    with background_lock:
        background_tasks[bg_id] = {
            "tool_use_id": block.id,
            "command": cmd,
            "status": "running",
        }
    threading.Thread(target=worker, daemon=True).start()

    # ─── ⑤ 立即返回 bg_id ───
    print(f"  \033[33m[background] dispatched {bg_id}: {cmd[:40]}\033[0m")
    return bg_id

def collect_background_results() -> list[str]:
    """收集已完成后台任务，返回 <task_notification> 列表。每个任务 pop 一次。

    执行流程（数字编号对应下方代码）：
      ① 持锁快照：找出所有 completed 的任务 ID 列表
      ② 逐个 pop 消费：从字典移除 + 取结果（每任务只通知一次）
      ③ 截断 summary 到 200 字符
      ④ 格式化为 <task_notification> XML 文本
    """
    # ─── ① 快照已完成任务 ID ───
    # 为什么分成"先找 ID、再逐个 pop"两段（而不是一把锁包到底）？
    #   → 缩小持锁范围。pop + 格式化字符串都在锁外做，
    #     锁只保护"遍历字典"这一瞬，避免长时间锁住影响其他线程写。
    with background_lock:
        ready_ids = [bid for bid, task in background_tasks.items()
                     if task["status"] == "completed"]
    notifications = []
    for bg_id in ready_ids:
        # ─── ② 逐个 pop 消费 ───
        with background_lock:
            # pop 即从字典移除，保证每个任务"只通知一次"（下次 collect 就找不到了）
            task = background_tasks.pop(bg_id)
            output = background_results.pop(bg_id, "")
        # ─── ③ 截断 summary ───
        # 截断到 200 字符：通知给 LLM 看的，不用全量结果
        summary = output[:200] if len(output) > 200 else output
        # ─── ④ 格式化 XML 通知 ───
        # <task_notification> 是 XML 标签格式的文本消息，
        # 注入到 messages 后 LLM 能结构化解析（<task_id>/<status>/<summary>）
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

def has_pending_background() -> bool:
    """非破坏性检查：是否有已完成待收集的后台任务。

    inbox_poller 用此条件判断是否需要唤醒 Lead——不消费数据。
    """
    with background_lock:
        # any(...)：遍历所有任务，只要有一个 completed 就返回 True
        return any(t["status"] == "completed" for t in background_tasks.values())


# ═══════════════════════════════════════════════════════════
#  Cron 调度器（s14 引入）
# ═══════════════════════════════════════════════════════════

DURABLE_PATH = WORKDIR / ".scheduled_tasks.json"

@dataclass
class CronJob:
    id: str
    cron: str        # "0 9 * * *"
    prompt: str      # message to inject when fired
    recurring: bool  # True = recurring, False = one-shot
    durable: bool    # True = persist to disk


scheduled_jobs: dict[str, CronJob] = {}
cron_queue: list[CronJob] = []
cron_lock = threading.Lock()
_last_fired: dict[str, str] = {}  # job_id → "YYYY-MM-DD HH:MM"

def _cron_field_matches(field: str, value: int) -> bool:
    """Match a single cron field against a value.

    匹配优先级（数字编号对应下方代码，判断顺序不能乱）：
      ① 通配 * → 恒匹配
      ② 步长 */N → 取模判断
      ③ 列举 A,B,C → 递归拆分，任一匹配
      ④ 范围 A-B → 闭区间判断
      ⑤ 纯数字 → 精确匹配
    """
    # ─── ① 通配 ───
    if field == "*":
        return True
    # ─── ② 步长 ───
    if field.startswith("*/"):
        step = int(field[2:])   # "*/5"[2:] = "5"
        # 取模判断：value % step == 0 表示 value 是 step 的整数倍。
        # 例：分钟字段 */5，dt.minute=20，20 % 5 == 0 → 匹配。
        return step > 0 and value % step == 0
    # ─── ③ 列举 ───
    if "," in field:
        # ★ 递归：把 "1,3,5" 按逗号拆开，逐个递归匹配。
        #   any(...) 是"任一匹配即可"——"1,3,5" 中 value=3 时 "3" 匹配 → True。
        #   用递归而不是手动循环，是为了让列举项也支持嵌套语法
        #   （比如 "1,*/2" 这种混合写法）。
        return any(_cron_field_matches(f.strip(), value)
                   for f in field.split(","))
    # ─── ④ 范围 ───
    if "-" in field:
        lo, hi = field.split("-", 1)   # "1-5" → lo="1", hi="5"
        # 闭区间判断：lo <= value <= hi（含端点）
        return int(lo) <= value <= int(hi)
    # ─── ⑤ 精确匹配 ───
    return value == int(field)

def cron_matches(cron_expr: str, dt: datetime) -> bool:
    """检查 5 字段 cron 表达式是否匹配。DOM/DOW 使用 OR 语义。"""
    fields = cron_expr.strip().split()
    if len(fields) != 5:
        return False
    minute, hour, dom, month, dow = fields
    # ★ weekday 数字体系转换：
    #   Python：   Monday=0, Tuesday=1, ..., Sunday=6
    #   cron：     Sunday=0, Monday=1, ..., Saturday=6
    #   (dt.weekday() + 1) % 7 实现转换：
    #     Monday   (0+1)%7=1 ✓
    #     Sunday   (6+1)%7=0 ✓
    #     Saturday (5+1)%7=6 ✓
    dow_val = (dt.weekday() + 1) % 7

    m = _cron_field_matches(minute, dt.minute)
    h = _cron_field_matches(hour, dt.hour)
    dom_ok = _cron_field_matches(dom, dt.day)
    month_ok = _cron_field_matches(month, dt.month)
    dow_ok = _cron_field_matches(dow, dow_val)

    # 分钟、小时、月份必须同时匹配（AND 关系）——这三者没有特殊语义
    if not (m and h and month_ok):
        return False
    # ★ DOM 和 DOW 是 OR 语义（标准 Unix cron 行为）：
    #   "0 9 15 * 1" 表示"每月15号 或 每周一"的 9:00，不是"15号且周一"。
    #   所以不能直接 `return dom_ok and dow_ok`——那是 AND（错误语义）。
    #   逻辑拆解：
    #     - 两者都没约束（都是 *）→ 无条件匹配（前面已通过 m/h/month）
    #     - 只约束了 DOM → 看 dom_ok
    #     - 只约束了 DOW → 看 dow_ok
    #     - 两者都约束 → OR（任一匹配即可）
    dom_unconstrained = dom == "*"
    dow_unconstrained = dow == "*"
    if dom_unconstrained and dow_unconstrained:
        return True
    if dom_unconstrained:
        return dow_ok
    if dow_unconstrained:
        return dom_ok
    return dom_ok or dow_ok

def _validate_cron_field(field: str, lo: int, hi: int) -> str | None:
    """Validate a single cron field value is within [lo, hi]."""
    # 与 _cron_field_matches 同构的递归验证：校验语法 + 范围。
    # 校验分支和匹配分支结构一致（* → */N → , → - → 数字），
    # 保证"能匹配的表达式必然合法"，"非法的必然拒绝"。
    # 返回 None = 合法；返回字符串 = 错误描述。
    if field == "*":
        return None
    if field.startswith("*/"):
        step_str = field[2:]
        if not step_str.isdigit():          # "*/x" 的 x 必须是纯数字
            return f"Invalid step: {field}"
        step = int(step_str)
        if step <= 0:                       # 步长必须 > 0（"*/0" 会除零）
            return f"Step must be > 0: {field}"
        return None
    if "," in field:
        # 递归校验每个列举项；任一项非法就整体非法（短路返回）
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
        if a > b:                           # "5-2" 是倒序范围，非法
            return f"Range start > end: {field}"
        return None
    if not field.isdigit():
        return f"Invalid field: {field}"
    val = int(field)
    if val < lo or val > hi:
        return f"Value {val} out of bounds [{lo}-{hi}]"
    return None

def validate_cron(cron_expr: str) -> str | None:
    """Validate a cron expression. Returns error message or None.

    执行流程：
      ① 按空白拆分 5 个字段，数量不对 → 报错
      ② zip 并行迭代 (字段, 范围, 名字)
      ③ 逐个字段调 _validate_cron_field 校验
      ④ 任一非法 → 带字段名返回错误；全部合法 → None
    """
    # ─── ① 拆分 + 数量检查 ───
    fields = cron_expr.strip().split()
    if len(fields) != 5:
        return f"Expected 5 fields, got {len(fields)}"
    # ─── ② 定义每个字段的范围和名字 ───
    # 每个字段的合法范围：minute 0-59, hour 0-23, DOM 1-31, month 1-12, DOW 0-6
    bounds = [(0, 59), (0, 23), (1, 31), (1, 12), (0, 6)]
    names = ["minute", "hour", "day-of-month", "month", "day-of-week"]
    # ★ zip(fields, bounds, names)：三个列表"拉链"式并行迭代。
    #   相当于同时遍历 (第几个字段, 它的范围, 它的名字)——
    #   不用下标索引，直接解包成 (field, (lo, hi), name) 三个变量。
    # ─── ③④ 逐个校验 ───
    for i, (field, (lo, hi), name) in enumerate(zip(fields, bounds, names)):
        err = _validate_cron_field(field, lo, hi)
        if err:
            return f"{name}: {err}"   # 返回带字段名的错误，方便 LLM 定位
    return None

def save_durable_jobs():
    """Persist durable jobs to .scheduled_tasks.json."""
    # asdict(j) for j in ... if j.durable：
    #   只把 durable=True 的任务落盘（session-only 任务不写，进程退出即丢）
    durable = [asdict(j) for j in scheduled_jobs.values() if j.durable]
    DURABLE_PATH.write_text(json.dumps(durable, indent=2))

def load_durable_jobs():
    """Load durable jobs from disk on startup.

    执行流程：
      ① 文件不存在 → 直接返回
      ② 读文件 → 逐个 CronJob(**dict) 还原
      ③ 逐个校验 cron 表达式，非法的跳过（不崩溃）
      ④ 打印加载数量；文件损坏 → 静默跳过
    """
    # ─── ① 文件不存在 ───
    if not DURABLE_PATH.exists():
        return
    try:
        # ─── ② 读文件 + 还原 ───
        jobs = json.loads(DURABLE_PATH.read_text())
        for j in jobs:
            # CronJob(**j)：dict → dataclass（同 load_task 的 ** 解包）
            job = CronJob(**j)
            # ─── ③ 逐个校验 ───
            # 加载时重新校验 cron 表达式：
            #   防止磁盘文件被手改坏，或之前版本写入的非法表达式。
            #   校验失败的 job 直接跳过（打印提示但不崩溃），保证启动不被单个坏任务拖垮。
            err = validate_cron(job.cron)
            if err:
                print(f"  \033[31m[cron] skipping invalid job {job.id}: {err}\033[0m")
                continue
            scheduled_jobs[job.id] = job
        # ─── ④ 统计 + 打印 ───
        valid = [j for j in jobs if j["id"] in scheduled_jobs]
        if valid:
            print(f"  \033[35m[cron] loaded {len(valid)} durable job(s)\033[0m")
    except Exception:
        # 整个文件损坏（JSON 解析失败）→ 静默跳过，不阻塞启动。
        # 宁可丢掉定时任务，也不能让 Agent 因为坏文件起不来。
        pass


def schedule_job(cron: str, prompt: str, recurring: bool = True,
                 durable: bool = True) -> CronJob | str:
    """Register a new cron job. Returns CronJob or error string.

    执行流程（数字编号对应下方代码）：
      ① 校验 cron 表达式（非法 → 直接返回错误字符串）
      ② 构造 CronJob（随机 6 位 id）
      ③ 写入 scheduled_jobs（持 cron_lock 防调度线程并发读）
      ④ durable → 落盘到 .scheduled_tasks.json
    """
    # ─── ① 校验 ───
    err = validate_cron(cron)
    if err:
        return err
    # ─── ② 构造任务 ───
    job = CronJob(
        id=f"cron_{random.randint(0, 999999):06d}",   # 6 位随机数，足够避免冲突
        cron=cron, prompt=prompt,
        recurring=recurring, durable=durable,
    )
    # ─── ③ 注册到内存 ───
    with cron_lock:
        scheduled_jobs[job.id] = job   # 写入共享字典（cron_lock 保护，防调度线程并发读）
    # ─── ④ durable → 落盘 ───
    if durable:
        save_durable_jobs()            # durable 任务同时落盘，保证跨会话恢复
    print(f"  \033[35m[cron register] {job.id} '{cron}' → {prompt[:40]}\033[0m")
    return job

def cancel_job(job_id: str) -> str:
    """Cancel a cron job.

    执行流程（数字编号对应下方代码）：
      ① 从 scheduled_jobs 移除（持锁）
      ② 不存在 → 返回错误
      ③ durable → 同步落盘
    """
    # ─── ① 移除 ───
    with cron_lock:
        job = scheduled_jobs.pop(job_id, None)
        # pop(job_id, None)：存在则删除并返回，不存在返回 None（不会 KeyError）
    # ─── ② 不存在兜底 ───
    if not job:
        return f"Job {job_id} not found"
    # ─── ③ durable → 同步落盘 ───
    if job.durable:
        save_durable_jobs()            # 删除 durable 任务后要同步落盘，否则重启又恢复
    print(f"  \033[31m[cron cancel] {job_id}\033[0m")
    return f"Cancelled {job_id}"

def cron_scheduler_loop():
    """独立守护线程：每 1 秒轮询，匹配的任务放入 cron_queue。

    执行流程（数字编号对应下方代码）：
      ① sleep(1) → 避免忙等
      ② datetime.now() → 获取当前时间 + 生成分钟标记
      ③ 遍历 scheduled_jobs（持有 cron_lock 保护）
      ④ 对每个 job：cron_matches 匹配？
      ⑤ 匹配成功 → 检查 _last_fired 防止重复 → 放入 cron_queue
      ⑥ 一次性任务（recurring=False）→ 触发后立即删除

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
        # ─── ① 每秒轮询 ───
        time.sleep(1)         # sleep 防止忙等（空转烧 CPU）
        # ─── ② 取当前时间 + 生成分钟标记 ───
        now = datetime.now()
        # Date-aware marker prevents daily jobs from skipping on day 2+
        # 时间戳精确到分钟："2026-07-31 09:05" 而不是 "09:05"。
        # 原因：如果只存 "HH:MM"，跨天后（第二天 09:05）会被误判为"已触发过"而跳过。
        minute_marker = now.strftime("%Y-%m-%d %H:%M")
        with cron_lock:
            # ─── ③ 遍历所有已注册任务 ───
            # list(...) 拷贝一份再遍历：防止遍历中 scheduled_jobs 被修改
            # （比如 cancel_job 在别的线程 pop 掉一个 job，会触发 RuntimeError）
            for job in list(scheduled_jobs.values()):
                try:
                    # ─── ④ 时间匹配 ───
                    if cron_matches(job.cron, now):
                        # ─── ⑤ 防重复 + 入队 ───
                        # 防止同一分钟重复触发：同一个 minute_marker 只触发一次。
                        # 例：*/5 任务在 09:05:01 触发，Agent 处理到 09:05:04
                        # 调度器又检查，分钟还是 09:05——靠 _last_fired 拦下。
                        if _last_fired.get(job.id) != minute_marker:
                            cron_queue.append(job)
                            _last_fired[job.id] = minute_marker
                            print(f"  \033[35m[cron fire] {job.id} → "
                                  f"{job.prompt[:40]}\033[0m")
                        # ─── ⑥ 一次性任务：触发后立即删除 ───
                        # ★ 删除发生在"入队"之后、消费之前——
                        #   即使 Agent 忙没消费，一次性任务也不会重复触发。
                        if not job.recurring:
                            scheduled_jobs.pop(job.id, None)
                            if job.durable:
                                save_durable_jobs()
                except Exception as e:
                    # 单任务异常保护：一个坏 job 的 bug 不杀死整个调度线程
                    print(f"  \033[31m[cron error] {job.id}: {e}\033[0m")

def consume_cron_queue() -> list[CronJob]:
    """Consume fired jobs from cron_queue (called by agent_loop).

    执行流程：
      ① 持锁拷贝队列（快照）
      ② 清空原队列
      ③ 返回快照
    """
    with cron_lock:
        # ─── ①② 拷贝 + 清空 ───
        # 先拷贝出来，再清空原队列。
        # ★ 不能直接 `return cron_queue`——那样外部拿到的是队列本身的引用，
        #   之后调度线程 append 也会被"外面"看到；先 list() 拷贝 + clear()
        #   保证"这次消费的就是这一批"，且下次调度器写的是新的一批。
        fired = list(cron_queue)
        cron_queue.clear()
    # ─── ③ 返回快照 ───
    return fired


# Load durable jobs on startup, then start scheduler thread
load_durable_jobs()
threading.Thread(target=cron_scheduler_loop, daemon=True).start()
print("  \033[35m[cron] scheduler thread started\033[0m")


# Cron tool handlers

def run_schedule_cron(cron: str, prompt: str,
                      recurring: bool = True, durable: bool = True) -> str:
    """工具包装：调度 cron 任务。

    执行流程：
      ① 调 schedule_job（成功 → CronJob 对象；失败 → 错误字符串）
      ② isinstance 判断：失败 → 包装 Error 返回
      ③ 成功 → 格式化 cron id + 表达式返回
    """
    # ─── ① 调用核心函数 ───
    result = schedule_job(cron, prompt, recurring, durable)
    # ─── ② 失败分支 ───
    # schedule_job 返回类型是 CronJob | str：
    #   成功 → CronJob 对象；失败（cron 非法）→ 错误字符串。
    # ★ isinstance(result, str) 是 Python 的类型判断：
    #   用它是为了区分"返回的是对象还是错误文本"——两种返回共用一个通道。
    if isinstance(result, str):
        return f"Error: {result}"
    # ─── ③ 成功分支 ───
    return f"Scheduled {result.id}: '{cron}' → {prompt}"

def run_list_crons() -> str:
    with cron_lock:
        jobs = list(scheduled_jobs.values())   # 持锁快照，防止调度线程并发改
    if not jobs:
        return "No cron jobs. Use schedule_cron to add one."
    lines = []
    for j in jobs:
        # 给 LLM 展示两个布尔字段的可读标签（recurring/durable）
        tag = "recurring" if j.recurring else "one-shot"
        dur = "durable" if j.durable else "session"
        lines.append(f"  {j.id}: '{j.cron}' → {j.prompt[:40]} "
                     f"[{tag}, {dur}]")
    return "\n".join(lines)

def run_cancel_cron(job_id: str) -> str:
    return cancel_job(job_id)


# ═══════════════════════════════════════════════════════════
#  NEW in s15: MessageBus —— 基于文件的 Agent 间通信
#
#  设计思想：
#    多个 Agent 需要互相发送消息（Lead ↔ Teammate）。
#    MessageBus 提供简单的文件邮箱机制：
#      - send(from, to, content) → 在 .mailboxes/{to}.jsonl 追加一行 JSON
#      - read_inbox(agent) → 读取 + 删除（破坏性读取，每条消息只消费一次）
#      - peek(agent) → 非破坏性检查（是否为空），用于 inbox_poller 判断唤醒
#
#  教学版简化：无文件锁（append 是原子操作靠 OS 保证）。
#  真实 CC 使用 proper-lockfile 保证并发写入安全。
# ═══════════════════════════════════════════════════════════

MAILBOX_DIR = WORKDIR / ".mailboxes"
MAILBOX_DIR.mkdir(exist_ok=True)

class MessageBus:
    """基于文件的 Agent 间消息总线。

    存储格式：.mailboxes/{agent_name}.jsonl
    JSONL = JSON Lines：每行一个独立的 JSON 对象（换行分隔）。

    每个 Agent 一个邮箱文件（Lead 的邮箱是 lead.jsonl，队友的邮箱是
    {name}.jsonl）。通信方式完全依赖文件系统：
      - 发消息 = 往对方的邮箱文件"追加"一行
      - 读消息 = 读整个文件 + 删除文件（消费式）

    read_inbox 是破坏性的（读取 + 删除文件），确保每条消息只被消费一次。
    这是"消息队列"语义——每条消息有一个消费者，读完即消失。
    如果只是"看一眼有没有消息"而不想消费，用 peek。

    ★ 为什么用文件而不是内存队列？
      教学版选文件是因为：
      1. 直观——.mailboxes/ 目录下的文件可以直接用编辑器查看
      2. 跨线程可观察——无论哪个线程写的，文件系统是共享的
      3. 与真实 CC 一致——CC 也用文件收件箱（~/.claude/teams/.../inboxes/）
      缺点是：没有文件锁，多线程并发写同一邮箱可能损坏。
      教学版靠 OS 的 append 原子性保证基本安全（真实 CC 用 proper-lockfile）。
    """

    def send(self, from_agent: str, to_agent: str, content: str,
             msg_type: str = "message"):
        """发送消息到指定 Agent 的邮箱。

        实现原理（数字编号对应下方代码）：
          ① 组装消息 dict（发送者/接收者/正文/类型/时间戳）
          ② 拼接目标邮箱路径 .mailboxes/{to_agent}.jsonl
          ③ 以 append 追加模式打开文件，写一行 JSON
        核心："a" 模式每次在文件末尾追加，不覆盖已有内容
        （对比 "w" 模式会清空文件重写）。

        参数 from_agent：发送者名称。
        参数 to_agent：接收者名称（= 邮箱文件名）。
        参数 content：消息正文。
        参数 msg_type：消息类型（"message" 普通消息 / "result" 完成报告）。
        """
        # ─── ① 组装消息 ───
        msg = {"from": from_agent, "to": to_agent,
               "content": content, "type": msg_type,
               "ts": time.time()}
        # ─── ② 目标邮箱路径 ───
        inbox = MAILBOX_DIR / f"{to_agent}.jsonl"
        # ─── ③ 追加写入（append 模式）───
        with open(inbox, "a") as f:
            f.write(json.dumps(msg) + "\n")   # json.dumps + 换行 → JSONL 格式
        print(f"  \033[33m[bus] {from_agent} → {to_agent}: "
              f"{content[:50]}\033[0m")

    def read_inbox(self, agent: str) -> list[dict]:
        """读取并销毁指定 Agent 的邮箱（破坏性操作）。

        实现原理（数字编号对应下方代码）：
          ① 拼接邮箱路径；文件不存在 → 返回空列表
          ② 读整个文件，每行 json.loads 还原成 dict
          ③ inbox.unlink() 删除整个文件（消费式）
        所以每条消息只能被读取一次——下次读取时文件已不存在，返回 []。

        返回：消息列表（每条为一个 dict）。
        空邮箱返回 []。读取后文件被删除。
        """
        # ─── ① 邮箱不存在 → 空收件箱 ───
        inbox = MAILBOX_DIR / f"{agent}.jsonl"
        if not inbox.exists():
            return []
        # ─── ② 读文件：每行 JSON → dict ───
        msgs = [json.loads(line) for line in inbox.read_text().splitlines()
                if line.strip()]   # 跳过空行
        # ─── ③ 消费式删除 ───
        inbox.unlink()  # 读完全部内容后删掉文件 → 下次读取返回 []
        return msgs

    def peek(self, agent: str) -> bool:
        """非破坏性检查：Agent 是否有未读消息。

        被 inbox_poller 使用——不消费数据，只判断是否需要唤醒。
        只检查文件是否存在 + 文件大小 > 0（有一行就算有消息）。

        ★ 为什么需要 peek？
          read_inbox 是破坏性的，如果 inbox_poller 用 read_inbox 检查
          就会把消息"提前消费掉"，然后主循环想读时已经空了。
          peek 让 poller 只做判断，消息留给主循环真正消费。
        """
        # ① 文件存在？
        # ② 且大小 > 0？（有一行内容就算有消息）
        # ★ and 短路：文件不存在时不会调 stat()（避免 FileNotFoundError）
        inbox = MAILBOX_DIR / f"{agent}.jsonl"
        return inbox.exists() and inbox.stat().st_size > 0

BUS = MessageBus()

# 追踪活跃的队友（防止重复创建同名 teammate）
active_teammates: dict[str, bool] = {}


# ═══════════════════════════════════════════════════════════
#  NEW in s15: 队友线程
#
#  设计思想：
#    Lead Agent 遇到需要独立处理的子任务时，创建 Teammate。
#    每个 Teammate 是独立线程 + 独立 messages[] + 自己的 agent_loop。
#    通过 MessageBus 与 Lead 通信。
#
#  队友与 Lead 的关键区别：
#    1. 队友的工具集更小（bash/read/write/send_message，没有 task/cron/team 工具）
#    2. 队友最多 10 轮（教学限制，真实 CC 使用 idle loop）
#    3. 队友完成后通过 BUS.send(name, "lead", summary) 发送最终摘要
#    4. 队友的上下文与 Lead 隔离——只有 prompt + 收件箱消息
# ═══════════════════════════════════════════════════════════

def spawn_teammate_thread(name: str, role: str, prompt: str) -> str:
    """在后台线程中创建队友 Agent。

    执行流程（数字编号对应下方代码）：
      ① 检查同名 teammate 是否已存在（防止重复创建）
      ② 组装队友的 system prompt（身份 + 角色 + 汇报方式）
      ③ 启动后台线程 run()——**立即返回**，不阻塞 Lead
      ④ run() 里：创建独立 messages → 最多 10 轮循环
         （读收件箱 → 调 LLM → 分发工具 → 回写）
      ⑤ 循环结束 → 提取最终摘要 → BUS.send 回 Lead → 移除注册

    参数 name：队友名称（如 "researcher"）。
    参数 role：角色描述（如 "code reviewer"）。
    参数 prompt：初始任务描述（同 s06 subagent 的 description）。

    ★ 与 s06 subagent 的关键区别：
      - s06 子 Agent 是同步的：父 Agent 阻塞等待，只拿回一个摘要
      - s15 队友是异步的：创建后立即返回，队友在后台线程自己跑，
        Lead 通过收件箱异步接收结果，期间可继续处理其他事

    返回：成功消息 或 错误（同名 teammate 已存在）。
    """
    # ─── ① 防重名检查 ───
    if name in active_teammates:
        return f"Teammate '{name}' already exists"

    # ─── ② 组装队友 system prompt ───
    # 明确告知：你是谁 + 用什么工具 + 结果发给谁（lead）
    system = (f"You are '{name}', a {role}. "
              f"Use tools to complete tasks. "
              f"Send results via send_message to 'lead'.")

    # ─── ③④⑤ 队友线程体（定义 run，稍后 start）───
    def run():
        # ④a 全新上下文：只有初始 prompt（与 Lead 完全隔离）
        messages = [{"role": "user", "content": prompt}]
        # 队友的工具集（简化版——没有 task/cron/team 工具）
        # ★ 注意：队友不能 spawn 队友（防止无限递归），所以没有 spawn_teammate
        # 队友也不能看 Lead 的收件箱（上下文隔离），所以没有 check_inbox
        sub_tools = [
            {"name": "bash", "description": "Run a shell command.",
             "input_schema": {"type": "object",
                              "properties": {"command": {"type": "string"}},
                              "required": ["command"]}},
            {"name": "read_file", "description": "Read file contents.",
             "input_schema": {"type": "object",
                              "properties": {"path": {"type": "string"}},
                              "required": ["path"]}},
            {"name": "write_file", "description": "Write content to a file.",
             "input_schema": {"type": "object",
                              "properties": {"path": {"type": "string"},
                                             "content": {"type": "string"}},
                              "required": ["path", "content"]}},
            {"name": "send_message",
             "description": "Send a message to another agent.",
             "input_schema": {"type": "object",
                              "properties": {"to": {"type": "string"},
                                             "content": {"type": "string"}},
                              "required": ["to", "content"]}},
        ]
        sub_handlers = {
            "bash": run_bash, "read_file": run_read, "write_file": run_write,
            # ★ 这个 lambda 是 Python 的一个技巧，拆开看：
            #   (BUS.send(name, to, content), "Sent") 先构造一个元组：
            #     元组第一个元素 = BUS.send(...) 的返回值（None，因为 send 不 return）
            #     元组第二个元素 = 字符串 "Sent"
            #   然后 [1] 取出元组的第二个元素。
            #   最终效果：调用 BUS.send 发消息，但返回值是 "Sent"。
            #   因为 execute_tool 的 `handler(**block.input)` 要求 handler 有返回值，
            #   而 BUS.send 本身返回 None——所以包装一层，让工具调用有明确的返回值给 LLM。
            # 另一种写法（更易读但更长）：
            #   def send_handler(to, content):
            #       BUS.send(name, to, content)
            #       return "Sent"
            "send_message": lambda to, content: (BUS.send(name, to, content), "Sent")[1],
        }

        # ─── ④b 队友循环（最多 10 轮，教学版限制）───
        # ★ 关键：这是一个"裸"的循环，不是完整的 agent_loop——
        #   没有 cron 消费、没有后台任务收集、没有 context 更新。
        #   队友只需要：读收件箱 → 调 LLM → 执行工具 → 回写，就够了。
        for _ in range(10):
            # ④b-1 先读收件箱（lead 可能发了消息）
            # ★ 这里使用 JSONL 的 json.dumps 序列化：
            #   把 inbox（list[dict]）转成 JSON 字符串包在 <inbox> 标签里
            #   作为一条 user 消息注入。这是队友"看到" Lead 消息的方式。
            inbox = BUS.read_inbox(name)
            if inbox:
                messages.append({"role": "user",
                                 "content": f"<inbox>{json.dumps(inbox)}</inbox>"})

            # ④b-2 调用 LLM（只保留最近 20 条消息，控制上下文长度）
            try:
                response = client.messages.create(
                    model=MODEL, system=system, messages=messages[-20:],
                    tools=sub_tools, max_tokens=8000)
            except Exception:
                break  # API 错误 → 退出

            # ④b-3 检查停止条件
            messages.append({"role": "assistant", "content": response.content})
            if response.stop_reason != "tool_use":
                break  # 队友认为任务完成

            # ④b-4 工具分发（send_message 走 BUS.send，其余同步执行）
            results = []
            for block in response.content:
                if block.type == "tool_use":
                    handler = sub_handlers.get(block.name)
                    output = handler(**block.input) if handler else "Unknown"
                    results.append({"type": "tool_result",
                                    "tool_use_id": block.id,
                                    "content": str(output)})

            # ④b-5 回写 → 进入下一轮
            messages.append({"role": "user", "content": results})

        # ─── ⑤ 队友完成 → 提取最终摘要 → 发回 Lead ───
        # ★ 摘要提取逻辑：从最后一条 assistant 消息往回找第一条 text block。
        #   默认 "Done."，找到文本就用文本覆盖。
        #   Python 的 for...else 语法：
        #     内层 for 循环如果"正常跑完"（没 break）就执行 else。
        #     这里内层 for 找到 text 就 break，找不到就跑 else 里的 continue
        #     （继续外层循环，往前翻更早的 assistant 消息）。
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
        active_teammates.pop(name, None)
        print(f"  \033[32m[teammate] {name} finished\033[0m")

    # ─── ③ 注册 + 启动队友线程 ───
    # 先写入 active_teammates（防重名检查靠它），再 start()。
    # daemon=True：主线程退出时队友线程被强制终止（不做优雅收尾）。
    active_teammates[name] = True
    threading.Thread(target=run, daemon=True).start()
    print(f"  \033[36m[teammate] {name} spawned as {role}\033[0m")
    return f"Teammate '{name}' spawned as {role}"


# ═══════════════════════════════════════════════════════════
#  Team 工具包装函数
#
#  Lead 的三个团队工具：spawn_teammate/send_message/check_inbox。
#  它们通过 execute_tool 的内联映射注册。
# ═══════════════════════════════════════════════════════════

def run_spawn_teammate(name: str, role: str, prompt: str) -> str:
    """工具包装：创建队友（线程立即返回，队友异步执行）。"""
    return spawn_teammate_thread(name, role, prompt)

def run_send_message(to: str, content: str) -> str:
    """工具包装：Lead 向指定 teammate 发送消息。

    发消息 → 写入 {to}.jsonl 邮箱。队友下一次循环读收件箱时就能看到。
    """
    BUS.send("lead", to, content)
    return f"Sent to {to}"

def run_check_inbox() -> str:
    """工具包装：Lead 查看自己的收件箱（破坏性读取）。

    执行流程：
      ① 读 Lead 邮箱（破坏性——读完即删）
      ② 空邮箱 → 返回 "(inbox empty)"
      ③ 非空 → 逐条格式化 [from] content 返回

    ★ 这个工具和主循环的 inbox 注入是两套机制，注意区分：
      - check_inbox（工具）：LLM 主动调用，读到消息后自己处理
      - inbox_poller（后台线程）：队友消息到达时自动唤醒 Lead 的主循环
    两者都调用 BUS.read_inbox("lead")——如果 LLM 先用工具读了，
    消息就被消费了，主循环的注入就 read 不到（返回 []）。
    这是设计上允许的——消息队列语义：谁先消费谁拿到。
    """
    # ─── ① 读邮箱（破坏性消费）───
    msgs = BUS.read_inbox("lead")
    # ─── ② 空邮箱分支 ───
    if not msgs:
        return "(inbox empty)"
    # ─── ③ 格式化消息 ───
    lines = []
    for m in msgs:
        lines.append(f"  [{m['from']}] {m['content'][:200]}")
    return "\n".join(lines)


# ═══════════════════════════════════════════════════════════
#  工具定义（TOOLS）—— 发送给 LLM 的 JSON Schema
#
#  s15：14 个工具（3 基础 + 5 任务 + 3 cron + 3 team）。
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
    {"name": "spawn_teammate",
     "description": "Spawn a teammate agent in a background thread.",
     "input_schema": {"type": "object",
                      "properties": {
                          "name": {"type": "string"},
                          "role": {"type": "string"},
                          "prompt": {"type": "string"}},
                      "required": ["name", "role", "prompt"]}},
    {"name": "send_message",
     "description": "Send a message to a teammate via MessageBus.",
     "input_schema": {"type": "object",
                      "properties": {"to": {"type": "string"},
                                     "content": {"type": "string"}},
                      "required": ["to", "content"]}},
    {"name": "check_inbox",
     "description": "Check Lead's inbox for teammate messages.",
     "input_schema": {"type": "object", "properties": {},
                      "required": []}},
]


# ═══════════════════════════════════════════════════════════
#  上下文评估
# ═══════════════════════════════════════════════════════════

def update_context(context: dict, messages: list) -> dict:
    """Derive context from real state."""
    # 从真实状态重建 context（而不是继承旧的）：
    #   memories 直接读 .memory/MEMORY.md 文件——文件变了就反映出来，
    #   不依赖上一轮的缓存。这样 system prompt 始终反映最新状态。
    memories = ""
    if MEMORY_INDEX.exists():
        content = MEMORY_INDEX.read_text().strip()
        if content:
            memories = content
    return {
        # enabled_tools：当前可用工具名列表（PROMPT_SECTIONS 的 tools 片段用它）
        "enabled_tools": [t["name"] for t in TOOLS],
        "workspace": str(WORKDIR),
        "memories": memories,
    }


# ═══════════════════════════════════════════════════════════
#  agent_loop — 消费 cron + 标准 LLM 交互（返回 void）
# ═══════════════════════════════════════════════════════════

def agent_loop(messages: list, context: dict):
    """Agent 核心循环（s15 版本）。

    执行流程（数字编号对应下方代码）：
      ① 消费 cron_queue 已触发的任务 → 注入 [Scheduled] 消息
      ② 调用 LLM（带 tools + system prompt）
      ③ stop_reason != "tool_use"？→ 是则结束本轮 agent_loop
      ④ 遍历 tool_use blocks：后台 → start_background_task；同步 → execute_tool
      ⑤ collect_background_results → 注入 <task_notification>
      ⑥ 结果回写 messages → 更新 context/system → 回到 ①

    ★ 与 s14 的区别：
      1. s14 的 agent_loop 返回 context（供 queue_processor_loop 复用）；
         s15 返回 void——主循环自己管理 context（每轮后 update_context）。
         因为 s15 的事件循环不再需要 queue_processor 那种"返回新 context"的接力。
      2. s15 的唤醒来源更丰富：用户输入、队友消息、后台任务完成，
         全部经由主循环的事件分发后调用 agent_loop，所以 agent_loop 内部
         不需要自己区分来源。

    注意：队友消息注入（[Inbox]）发生在主循环，不在这里。
    agent_loop 只处理：cron 消费 + LLM 交互 + 工具分发。
    """
    # 首次进入：用当前 context 组装 system prompt（每次循环末尾会更新）
    system = get_system_prompt(context)
    while True:
        # ─── ① 消费 cron 队列，注入定时任务消息 ───
        fired = consume_cron_queue()
        for job in fired:
            messages.append({"role": "user",
                             "content": f"[Scheduled] {job.prompt}"})
            print(f"  \033[35m[inject cron] {job.prompt[:50]}\033[0m")

        # ─── ② 调用 LLM ───
        try:
            response = client.messages.create(
                model=MODEL, system=system, messages=messages,
                tools=TOOLS, max_tokens=8000)
        except Exception as e:
            # API 错误：把错误以 assistant 消息形式记录后直接退出循环
            # （不重试、不降级——s15 省略了 s11 的错误恢复机制）
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
            # ④b 同步路径：直接执行，返回完整输出
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

        # ─── ⑥ 结果回写 → 更新 context → 回到 ① ───
        messages.append({"role": "user", "content": user_content})
        context = update_context(context, messages)
        system = get_system_prompt(context)


# ═══════════════════════════════════════════════════════════
#  主入口 —— 事件驱动循环
#
#  s15 主入口与之前章节完全不同：不是简单的 input → agent_loop 循环。
#  而是事件驱动的多线程架构，合并三种事件来源：
#    1. 用户输入（input_reader 线程）
#    2. 队友收件箱（inbox_poller 线程，每 1 秒检查 BUS.peek + has_pending_background）
#    3. 退出信号
#
#  所有事件通过 Python queue.Queue 统一传递，
#  主循环从 events.get() 消费后执行一轮 agent_loop。
# ═══════════════════════════════════════════════════════════

if __name__ == "__main__":
    print("s15: agent teams")
    print("Enter a question, press Enter to send. Type q to quit.\n")
    history = []
    context = update_context({}, [])

    # ── 事件驱动架构（s15 最重要的结构变化）──
    # 之前章节：主循环 = input() → agent_loop → 结束。
    #          用户不输入，Agent 就完全空闲（cron 是特例）。
    # s15：主循环 = events.get() 等待三类事件。
    #          队友消息/后台任务随时可能到达，不能只等用户输入。
    #
    # 事件队列（queue.Queue）是线程安全的：
    #   多个线程（input_reader / inbox_poller）可以同时 put，
    #   主循环 get() 时按 FIFO 顺序一个一个取出。
    #   事件格式统一为 (kind, payload) 二元组：
    #     ("user", 用户输入字符串)
    #     ("wake", None)  队友消息/后台任务就绪
    #     ("quit", None)  退出
    events = queue.Queue()

    def input_reader():
        """后台线程：阻塞等待用户输入 → 放入 events 队列。

        类型：
          "quit" → 主循环退出
          "user" → 正常的用户输入

        ★ 为什么 input() 要放进线程？
           input() 是阻塞的——用户不输入它就永远停在那。
           如果把 input() 放主循环，inbox_poller 检测到队友消息时
           主循环正阻塞在 input() 上，队友消息就没人处理了。
           放进独立线程后，input() 阻塞不影响主循环消费其他事件。
        """
        while True:
            try:
                line = input("\033[36ms15 >> \033[0m")
            except (EOFError, KeyboardInterrupt):
                events.put(("quit", None))   # 异常退出 → 通知主循环结束
                return
            except (OSError, ValueError):
                return   # stdin 被主线程关闭（exit_cleanly 触发）→ 线程自然退出
            # ★ 识别退出词：直接 os._exit(0) 立即终止进程。
            #   为什么不能"放 quit 事件等主循环处理"？
            #   主循环可能正卡在一轮很长的 agent_loop 里（调 LLM 几十秒），
            #   quit 事件排在队列里但主线程拿不到——表现为"输入 quit 没反应"。
            #   os._exit 从独立线程直接杀进程，绕开主循环，立即生效。
            #   同时它也不做解释器 shutdown，不会触发 daemon 线程的 stdin 死锁。
            if line.strip().lower() in ("q", "quit", "exit", ""):
                os._exit(0)
            events.put(("user", line))       # 正常输入 → 放入事件队列

    def inbox_poller():
        """后台线程：每 1 秒检查是否有异步结果就绪。

        唤醒条件（满足任一）：
          - BUS.peek("lead")：队友向 Lead 发送了消息
          - has_pending_background()：后台任务完成，结果待收集

        判断用 peek（非破坏性）而不是 read_inbox（破坏性）：
          peek 只检查"有没有"，不消费消息。
          真正的读取留到主循环的 "wake" 分支里做。

        注意：不依赖 active_teammates 判断——队友发送最终消息后
        会从 active_teammates 移除自己，所以消息可能先于注册移除到达。
        """
        while True:
            time.sleep(1)                    # 每秒检查一次
            if BUS.peek("lead") or has_pending_background():
                events.put(("wake", None))   # 有货 → 通知主循环

    # ─── ① 启动两个事件源线程（input_reader + inbox_poller）───
    # 保存 input_reader 线程引用，供 exit_cleanly 里 join 等它收尾
    input_thread = threading.Thread(target=input_reader, daemon=True)
    input_thread.start()
    threading.Thread(target=inbox_poller, daemon=True).start()

    def exit_cleanly():
        """优雅退出：先唤醒阻塞的 input_reader，再走正常 shutdown。

        ★ 这个函数现在只是兜底（Ctrl+C 或主循环收到 quit 事件时用）。
          正常输入 q/quit 退出已经由 input_reader 直接 os._exit(0) 处理了。

        ★ 为什么不能直接 break 走正常退出？
        input_reader 是 daemon 线程，正阻塞在 input() 上等 stdin。
        主线程 break 后，解释器 shutdown 会尝试关闭 stdin，
        但 daemon 线程还占着 stdin 的缓冲锁 → Fatal Python error
        （_enter_buffered_busy）。

        ★ 优雅解法：
          1. os.close(0) 关闭 stdin 文件描述符 → 阻塞的 read() 返回错误
             → input() 抛 OSError → input_reader 的 except 捕获后 return
          2. input_thread.join() 等线程真正结束（不再占着 stdin 锁）
          3. 此时才正常 return 走解释器 shutdown——没有线程抢锁了
          相比 os._exit(0) 硬终止，这样保留了正常的清理路径
          （队友线程收尾、stdout flush 等）。os._exit 只留作兜底。
        """
        try:
            os.close(0)              # 关闭 stdin fd → 唤醒阻塞的 input()
        except OSError:
            pass                     # stdin 可能已关闭，忽略
        input_thread.join(timeout=2) # 等 input_reader 退出（最多 2 秒）
        if input_thread.is_alive():
            # 极端兜底：join 超时线程仍占着锁 → 只能硬终止
            os._exit(0)

    had_teammates = False
    # ─── ② 主事件循环：消费三类事件 ───
    while True:
        kind, payload = events.get()   # 阻塞等待下一个事件

        # ②a quit 事件（Ctrl+C 产生）→ 退出程序
        if kind == "quit":
            exit_cleanly()

        # ②b user 事件 → 追加用户消息到 history
        if kind == "user":
            # ★ 退出判断：q / quit / exit / 空输入 都退出。
            #   这是双保险——input_reader 已过滤退出词并清空队列放 quit，
            #   正常流程不会走到这里；万一有其他路径塞入 user 退出词也兜住。
            if payload.strip().lower() in ("q", "quit", "exit", ""):
                exit_cleanly()
                break   # 退出后不能继续 append，直接离开主循环
            history.append({"role": "user", "content": payload})

        # ②c wake 事件 → 队友消息/后台任务就绪，构造注入消息
        else:
            parts = []
            # 读取队友发来的收件箱消息（破坏性——读完即删）
            inbox = BUS.read_inbox("lead")
            if inbox:
                parts.append("[Inbox]\n" + "\n".join(
                    f"From {m['from']}: {m['content'][:200]}" for m in inbox))
            # 收集后台任务完成通知
            bg = collect_background_results()
            parts.extend(bg)
            # ★ 等幂检查：如果 inbox 和 bg 都为空，说明这次 wake 对应的
            #   消息已被前面的唤醒消费掉了（比如 inbox_poller 每 1 秒检查，
            #   连续两次 wake 但第一次已把消息消费完）——跳过，不触发空循环
            if not parts:
                continue  # 已被前一次唤醒消费（等幂检查）
            history.append({"role": "user", "content": "\n".join(parts)})
            print(f"\n\033[33m[wake: {len(inbox)} inbox + {len(bg)} background "
                  f"-> new turn]\033[0m")

        # ─── ③ 无论哪种事件来源，都执行一轮 agent_loop ───
        agent_loop(history, context)
        context = update_context(context, history)
        # 打印 agent_loop 结束后最后一条 assistant 消息的文本
        for block in history[-1]["content"]:
            if getattr(block, "type", None) == "text":
                print(block.text)
            elif isinstance(block, dict) and block.get("type") == "text":
                print(block.get("text", ""))

        # 所有队友完成时打印提示
        # 状态机：active_teammates 非空 → 记录 had_teammates=True
        #          清空且无待处理消息 → 打印 "all teammates done" 并复位
        if active_teammates:
            had_teammates = True
        elif had_teammates and not BUS.peek("lead") and not has_pending_background():
            print("\033[32m[all teammates done]\033[0m")
            had_teammates = False
        print()
