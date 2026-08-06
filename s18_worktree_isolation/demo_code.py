#!/usr/bin/env python3
"""
s18: 工作树隔离 —— git worktree + 任务-目录绑定 + 事件日志。

运行：  python s18_worktree_isolation/demo_code.py
依赖：  pip install anthropic python-dotenv + .env 中配置 ANTHROPIC_API_KEY

相对 s17 的变更：
  - Task 数据类新增 worktree 字段（str | None，绑定的工作树名）
  - validate_worktree_name：校验 worktree 名，拒绝路径穿越和非法字符
  - create_worktree：校验名字 → git worktree add → 可选绑定任务
  - bind_task_to_worktree：绑定任务到 worktree，只写 worktree 字段，保持任务为 pending
  - remove_worktree：强制删除前做安全检查，不自动完成任务
  - run_git 返回 (是否成功, 输出)，只在成功后才写事件日志
  - 队友工具：+ complete_task，绑定时在 worktree 目录下执行
  - scan_unclaimed_tasks：用 can_start() 做依赖检查
  - idle_poll：检查认领结果，IDLE 阶段分发 shutdown
  - consume_lead_inbox：统一 Lead 收件箱消费（路由协议 + 注入上下文）
  - Lead 新增 3 个工具：create_worktree / remove_worktree / keep_worktree

目录拓扑：
  主仓库 (/)
    ├── .worktrees/auth/  (分支: wt/auth)   ← 任务 #1
    ├── .worktrees/ui/    (分支: wt/ui)     ← 任务 #2
    ├── .tasks/task_xxx.json (worktree: "auth")
    └── .worktrees/events.jsonl

──────────────────────────────────────────────
s18 核心（本文件重点注释区）：
  Worktree System：validate_worktree_name / run_git / log_event /
    create_worktree / bind_task_to_worktree / _count_worktree_changes /
    remove_worktree / keep_worktree（s18 新增）
  spawn_teammate_thread 的 wt_ctx：队友 bash/read/write 在 worktree cwd 执行
  其余函数为 s01-s17 继承，保留注释以维持章节一致性
──────────────────────────────────────────────
"""

import os, subprocess, json, time, random, threading, re
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

# ── Task System (from s12 + s18 worktree field) ──

TASKS_DIR = WORKDIR / ".tasks"
TASKS_DIR.mkdir(exist_ok=True)


@dataclass
class Task:
    """任务数据（s12 沿用 + s18 新增 worktree 字段）。

    字段说明：
      id / subject / description：任务标识与描述
      status：pending → in_progress → completed（s17 自治状态机）
      owner：认领人（None = 未认领，s17 可认领前提）
      blockedBy：依赖任务 ID 列表（依赖完成后才能认领）
      worktree：绑定的工作目录名（s18 新增，None = 无绑定）
    """
    id: str
    subject: str
    description: str
    status: str
    owner: str | None
    blockedBy: list[str]
    worktree: str | None = None      # s18: bound worktree name


def _task_path(task_id: str) -> Path:
    return TASKS_DIR / f"{task_id}.json"


def create_task(subject: str, description: str = "",
                blockedBy: list[str] | None = None) -> Task:
    """创建任务并落盘（s12 沿用）。

    ★ owner 初始为 None：s17 自治认领的前提——无 owner 才在候选范围内。
      worktree 字段默认 None：s18 中由 bind_task_to_worktree 写入。
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

    ★ Task(**dict)：asdict 序列化 + ** 解包反序列化，
      dataclass 来回转换模式（见学习记录 09）。
    """
    return Task(**json.loads(_task_path(task_id).read_text()))


def list_tasks() -> list[Task]:
    """列出看板上所有任务，按文件名排序（s12 沿用）。"""
    return [Task(**json.loads(p.read_text()))
            for p in sorted(TASKS_DIR.glob("task_*.json"))]


def get_task_json(task_id: str) -> str:
    """返回单个任务的完整 JSON 字符串（s12 沿用）。"""
    task = load_task(task_id)
    return json.dumps(asdict(task), indent=2)


def can_start(task_id: str) -> bool:
    """判断任务是否可以开始：所有 blockedBy 依赖都已 completed（s12 沿用）。

    执行流程：
      ① 加载任务，遍历所有依赖 ID
      ② 依赖任务文件不存在 → 不可开始
      ③ 依赖任务状态 != "completed" → 不可开始（被阻塞）
      ④ 全部依赖完成后 → 可开始

    ★ 依赖语义：有依赖 ≠ 不能做；只有"被未完成的依赖阻塞"才不能做。
    """
    task = load_task(task_id)
    for dep_id in task.blockedBy:
        if not _task_path(dep_id).exists():
            return False
        if load_task(dep_id).status != "completed":
            return False
    return True


def claim_task(task_id: str, owner: str = "agent") -> str:
    """认领一个 pending 任务，将其状态改为 in_progress（s17 沿用）。

    执行流程：
      ① 加载任务
      ② 校验：必须 pending
      ③ 校验：必须无 owner（防止两个队友抢同一个任务 → 后写覆盖）
      ④ 校验：依赖必须全部完成（can_start）
      ⑤ 通过 → 写入 owner + 状态置为 in_progress，落盘
      ⑥ 返回成功/失败信息

    ★ 并发安全：教学版 owner 检查是"读时判断"，无文件锁，
      存在 TOCTOU 窗口（详见 README 竞态分析）。
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
    """将 in_progress 任务标记为 completed，并回报"解锁了下游谁"（s17 沿用）。

    执行流程：
      ① 校验：必须 in_progress
      ② 状态置为 completed，落盘
      ③ 扫描看板：找出因本任务完成而"解锁"（依赖全完成）的 pending 任务
      ④ 返回结果，附带 Unblocked 提示

    ★ s18 中队友的 _run_complete_task 会在调用后重置 wt_ctx["path"]，
      让队友从 worktree 回到主仓库——完成即脱离隔离目录。
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


# ═══════════════════════════════════════════════════════════
#  Worktree System（s18 新增核心）
#
#  问题：s17 队友共享 WORKDIR。Alice 和 Bob 都改 config.py，
#  互相覆盖，且分不清改动是谁的、无法干净回滚。
#
#  s18 方案：git worktree 隔离目录。
#    每个任务可绑定一个独立工作目录（.worktrees/{name}/，
#    独立分支 wt/{name}），队友认领后工具在 worktree 里执行。
#
#  核心生命周期：
#    create_worktree（创建+可选绑定）→ 队友认领（cwd 切换）
#    → 工作完成 → remove_worktree（清理） 或 keep_worktree（保留审查）
#
#  三个安全机制（教学版重点）：
#    1. validate_worktree_name：只允许 [A-Za-z0-9._-]{1,64}，拒绝路径穿越
#    2. remove_worktree 默认拒绝删除有改动的 worktree（需 discard_changes=true）
#    3. run_git 返回 (ok, output)，只在 git 成功后才写事件日志
# ═══════════════════════════════════════════════════════════

WORKTREES_DIR = WORKDIR / ".worktrees"      # 所有 worktree 的根目录
WORKTREES_DIR.mkdir(exist_ok=True)

# 合法 worktree 名：1-64 位字母/数字/点/下划线/短横线
VALID_WT_NAME = re.compile(r'^[A-Za-z0-9._-]{1,64}$')


def validate_worktree_name(name: str) -> str | None:
    """校验 worktree 名是否合法。合法返回 None，非法返回错误信息。

    三道检查（对应下方代码）：
      ① 空名 → 拒绝
      ② "." / ".." → 拒绝（★ 防路径穿越：否则 name 会逃出 WORKTREES_DIR）
      ③ 不匹配正则 → 拒绝（非法字符 / 超长）

    ★ 为什么必须有这一步？
      worktree 名会被拼进路径（WORKTREES_DIR / name）和分支名（wt/{name}）。
      若允许 ".." 或含 "/" 的名字，git 命令可能操作到仓库外——
      这是安全的入口关卡。
    """
    if not name:
        return "Worktree name cannot be empty"
    if name == "." or name == "..":
        return f"'{name}' is not a valid worktree name"
    if not VALID_WT_NAME.match(name):
        return (f"Invalid worktree name '{name}': "
                "only letters, digits, dots, underscores, dashes (1-64 chars)")
    return None


def run_git(args: list[str]) -> tuple[bool, str]:
    """统一执行 git 命令，返回 (是否成功, 输出文本)。

    执行流程：
      ① 用 subprocess 跑 git（cwd=WORKDIR，30s 超时）
      ② 拼接 stdout+stderr 为输出文本（★ 出错信息也返回给模型看）
      ③ 截断到 5000 字符防刷屏
      ④ 返回 returncode==0 作为成功标志

    ★ 返回值 (ok, output) 设计：
      调用方（create/remove_worktree）只在 ok=True 时才写事件日志，
      保证日志反映真实 git 状态，不记录失败操作。
    """
    try:
        r = subprocess.run(["git"] + args, cwd=WORKDIR,
                           capture_output=True, text=True, timeout=30)
        out = (r.stdout + r.stderr).strip()
        out = out[:5000] if out else "(no output)"
        return r.returncode == 0, out
    except subprocess.TimeoutExpired:
        return False, "Error: git timeout"


def log_event(event_type: str, worktree_name: str, task_id: str = ""):
    """把一次 worktree 生命周期事件追加到 events.jsonl（审计日志）。

    ★ 事件类型：create（创建）/ remove（删除）/ keep（保留）。
      只做记录，不做恢复——教学版用于人工排查；
      完整恢复还需要 index 或 `git worktree list` 扫描。
    """
    event = {"type": event_type, "worktree": worktree_name,
             "task_id": task_id, "ts": time.time()}
    events_file = WORKTREES_DIR / "events.jsonl"
    with open(events_file, "a") as f:
        f.write(json.dumps(event) + "\n")


def create_worktree(name: str, task_id: str = "") -> str:
    """创建独立 git worktree + 专属分支，可选绑定到任务。

    执行流程：
      ① 校验名字（非法 → 返回错误）
      ② 检查目录是否已存在（防重复创建）
      ③ git worktree add：在 .worktrees/{name} 建目录，
         基于当前 HEAD 开新分支 wt/{name}（★ -b 创建专属分支）
      ④ 失败 → 返回 git 错误
      ⑤ 成功 → 若有 task_id 则绑定到任务（bind_task_to_worktree）
      ⑥ 写 create 事件日志 → 返回成功信息

    ★ 分支命名 wt/{name}：每个 worktree 独占一个分支，
      与主分支互不干扰——这是"隔离"的核心。
    """
    err = validate_worktree_name(name)
    if err:
        return f"Error: {err}"
    path = WORKTREES_DIR / name
    if path.exists():
        return f"Worktree '{name}' already exists at {path}"
    ok, result = run_git(["worktree", "add", str(path), "-b", f"wt/{name}", "HEAD"])
    if not ok:
        return f"Git error: {result}"
    if task_id:
        bind_task_to_worktree(task_id, name)
    log_event("create", name, task_id)
    print(f"  \033[33m[worktree] created: {name} at {path}\033[0m")
    return f"Worktree '{name}' created at {path}"


def bind_task_to_worktree(task_id: str, worktree_name: str):
    """把任务和 worktree 绑定（只写 worktree 字段，不改任务状态）。

    ★ 为什么不改 status？
      任务仍保持 pending，等队友自动认领时才推进到 in_progress——
      这样 Lead 可以提前建好任务+worktree，队友 idle 时自然认领。
      （绑定 = 预分配目录，认领 = 实际开工）
    """
    task = load_task(task_id)
    task.worktree = worktree_name
    save_task(task)
    print(f"  \033[33m[bind] {task.subject} → worktree:{worktree_name}\033[0m")


def _count_worktree_changes(path: Path) -> tuple[int, int]:
    """统计 worktree 的未提交文件数 + 未推送 commit 数。

    两个 git 查询：
      ① git status --porcelain → 未提交改动文件数
      ② git log @{push}..HEAD → 已提交但未推送的 commit 数

    返回 (-1, -1) 表示查询失败（可能不是 git 仓库）。
    """
    try:
        r1 = subprocess.run(["git", "status", "--porcelain"],
                            cwd=path, capture_output=True, text=True, timeout=10)
        files = len([l for l in r1.stdout.strip().splitlines() if l.strip()])
        r2 = subprocess.run(["git", "log", "@{push}..HEAD", "--oneline"],
                            cwd=path, capture_output=True, text=True, timeout=10)
        commits = len([l for l in r2.stdout.strip().splitlines() if l.strip()])
        return files, commits
    except Exception:
        return -1, -1


def remove_worktree(name: str, discard_changes: bool = False) -> str:
    """移除 worktree。默认拒绝删除有改动的目录，需 discard_changes=true 强制。

    执行流程：
      ① 校验名字 + 检查目录存在
      ② 默认安全检查（discard_changes=False）：
         统计未提交文件/未推送 commit
         - 查询失败 → 拒绝（让调用方显式确认强制删除）
         - 有改动 → 拒绝，提示用 discard_changes 或 keep_worktree
      ③ 通过 → git worktree remove --force 删目录
      ④ 再删专属分支 wt/{name}（★ 分支残留会占仓库）
      ⑤ 写 remove 事件日志

    ★ 安全设计：删除前必须确认没有未保存的工作——
      相当于"回收站前先问一句"，防止误删队友的劳动成果。
    """
    err = validate_worktree_name(name)
    if err:
        return err
    path = WORKTREES_DIR / name
    if not path.exists():
        return f"Worktree '{name}' not found"
    if not discard_changes:
        files, commits = _count_worktree_changes(path)
        if files < 0:
            return (f"Cannot verify worktree '{name}' status. "
                    "Use discard_changes=true to force removal.")
        if files > 0 or commits > 0:
            return (f"Worktree '{name}' has {files} uncommitted file(s) "
                    f"and {commits} unpushed commit(s). "
                    "Use discard_changes=true to force removal, "
                    "or keep_worktree to preserve for review.")
    ok1, _ = run_git(["worktree", "remove", str(path), "--force"])
    if not ok1:
        return f"Failed to remove worktree directory for '{name}'"
    run_git(["branch", "-D", f"wt/{name}"])
    log_event("remove", name)
    print(f"  \033[33m[worktree] removed: {name}\033[0m")
    return f"Worktree '{name}' removed"


def keep_worktree(name: str) -> str:
    """保留 worktree 供人工审查（不删分支、不删目录）。

    ★ 与 remove 互补：remove = 清理，keep = 留档。
      典型流程：队友完成后，Lead 先 keep 供人工 review，
      确认无误后再 remove 清理。
    """
    err = validate_worktree_name(name)
    if err:
        return err
    log_event("keep", name)
    print(f"  \033[36m[worktree] kept: {name}\033[0m")
    return f"Worktree '{name}' kept for review (branch: wt/{name})"


# ── Prompt Assembly (from s10) ──

PROMPT_SECTIONS = {
    "identity": "You are a coding agent. Act, don't explain.",
    "tools": "Available tools: bash, read_file, write_file, "
             "create_task, list_tasks, get_task, claim_task, complete_task, "
             "spawn_teammate, send_message, check_inbox, "
             "request_shutdown, request_plan, review_plan, "
             "create_worktree, remove_worktree, keep_worktree.",
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

    执行流程：
      ① 把 context 序列化做缓存 key（★ sort_keys 保证 key 顺序无关）
      ② key 未变且已有缓存 → 直接返回旧 prompt
      ③ key 变了 → 重新组装并缓存

    ★ 变量名 _last_context_hash 是"指纹"泛称——存的其实是
      json.dumps 后的字符串，不是算法哈希（详见学习记录 10/15）。
    """
    global _last_context_hash, _last_prompt
    h = json.dumps(context, sort_keys=True)
    if h == _last_context_hash and _last_prompt:
        return _last_prompt
    _last_context_hash, _last_prompt = h, assemble_system_prompt(context)
    return _last_prompt


# ── Basic Tools ──

def safe_path(p: str, cwd: Path = None) -> Path:
    """把相对路径安全地解析到工作区，拒绝路径逃逸（s02 沿用 + s18 cwd）。

    ★ s18 新增 cwd 参数：允许以 worktree 目录为基准解析——
      队友工具把 cwd 指向 worktree，路径就落在隔离目录内，
      同时仍能防逃逸（is_relative_to 检查）。
    """
    base = cwd or WORKDIR
    path = (base / p).resolve()
    if not path.is_relative_to(base):
        raise ValueError(f"Path escapes workspace: {p}")
    return path


def run_bash(command: str, cwd: Path = None) -> str:
    """执行 shell 命令（s02 沿用 + s18 cwd）。

    ★ s18 关键改造：队友的 _run_bash 传入 wt_ctx 路径作为 cwd，
      git 操作、文件操作都发生在该 worktree 目录。
    """
    try:
        r = subprocess.run(command, shell=True, cwd=cwd or WORKDIR,
                           capture_output=True, text=True, timeout=120)
        out = (r.stdout + r.stderr).strip()
        return out[:50000] if out else "(no output)"
    except subprocess.TimeoutExpired:
        return "Error: Timeout (120s)"


def run_read(path: str, limit: int | None = None, cwd: Path = None) -> str:
    """读取文件（s02 沿用 + s18 cwd）。"""
    try:
        lines = safe_path(path, cwd).read_text().splitlines()
        if limit and limit < len(lines):
            lines = lines[:limit] + [f"... ({len(lines) - limit} more lines)"]
        return "\n".join(lines)
    except Exception as e:
        return f"Error: {e}"


def run_write(path: str, content: str, cwd: Path = None) -> str:
    """写入文件（s02 沿用 + s18 cwd）。"""
    try:
        fp = safe_path(path, cwd)
        fp.parent.mkdir(parents=True, exist_ok=True)
        fp.write_text(content)
        return f"Wrote {len(content)} bytes to {path}"
    except Exception as e:
        return f"Error: {e}"


# ── MessageBus (from s15) ──

MAILBOX_DIR = WORKDIR / ".mailboxes"
MAILBOX_DIR.mkdir(exist_ok=True)


class MessageBus:
    """基于文件的进程内消息总线（s15 沿用）。

    ★ 文件做邮箱：每个 agent 一个 {agent}.jsonl 追加式日志。
      发送=追加一行，读取=整文件读出后删除（消费式）。
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
    return f"req_{random.randint(0, 999999):06d}"


def match_response(response_type: str, request_id: str, approve: bool):
    """将协议响应关联回原始请求（request_id），更新状态机（s16 沿用）。

    执行流程：
      ① 凭 request_id 在 pending_requests 里找原始请求
      ② 找不到 → 未知请求，忽略
      ③ 类型校验：shutdown 只能被 shutdown_response 响应，
         plan_approval 只能被 plan_approval_response 响应
      ④ 通过 → status 置为 approved / rejected

    ★ 与 s16 的差异：s16 还有幂等校验（非 pending 的重复响应忽略），
      s18 精简掉了——教学简化，正常单次响应不影响正确性。
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


# ── Autonomous Agent (from s17, + worktree cwd) ──

IDLE_POLL_INTERVAL = 5
IDLE_TIMEOUT = 60


def scan_unclaimed_tasks() -> list[dict]:
    """扫描任务看板，找出"可认领"的任务（s17 沿用）。

    可认领三条件：
      ① status == "pending"
      ② 无 owner
      ③ can_start 为真（所有 blockedBy 依赖已完成）

    ★ 返回 dict 而非 Task 对象：调用方 idle_poll 只读 worktree 字段，
      用字典访问（task_data["worktree"]）即可。
    """
    unclaimed = []
    for f in sorted(TASKS_DIR.glob("task_*.json")):
        task = json.loads(f.read_text())
        if (task.get("status") == "pending"
                and not task.get("owner")
                and can_start(task["id"])):
            unclaimed.append(task)
    return unclaimed


def idle_poll(agent_name: str, messages: list,
              name: str, role: str) -> tuple[str, str | None]:
    """队友的 IDLE 阶段：每 5s 轮询，找活干（s17 沿用 + s18 返回认领任务）。

    返回值（二元组）：
      ("work",     task_id) → 认领了任务 → 回 WORK（task_id 供切 worktree cwd）
      ("work",     None)    → 收到普通消息 → 回 WORK
      ("shutdown", None)    → 收到 shutdown_request → 退出
      ("timeout",  None)    → 60s 无活 → 退出

    ★ s18 相比 s17 的变化：
      认领成功后把 task_id 一并返回（第二个返回值），
      外层 run() 据此把队友的 wt_ctx["path"] 切到该任务的 worktree。
      认领时还把 worktree 路径写进 <auto-claimed> 注入上下文，
      让模型知道"去哪个目录干活"。
    """
    for _ in range(IDLE_TIMEOUT // IDLE_POLL_INTERVAL):
        time.sleep(IDLE_POLL_INTERVAL)

        # ─── ① 读收件箱（优先）───
        inbox = BUS.read_inbox(agent_name)
        if inbox:
            # ② shutdown_request → 立即回复并退出
            for msg in inbox:
                if msg.get("type") == "shutdown_request":
                    req_id = msg.get("metadata", {}).get("request_id", "")
                    BUS.send(name, "lead", "Shutting down gracefully.",
                             "shutdown_response",
                             {"request_id": req_id, "approve": True})
                    print(f"  \033[35m[protocol] {name} approved shutdown "
                          f"in idle ({req_id})\033[0m")
                    return "shutdown", None

            # ③ 非协议消息 → 注入上下文 → 回 WORK
            messages.append({"role": "user",
                "content": "<inbox>" + json.dumps(inbox) + "</inbox>"})
            print(f"  \033[36m[idle] {name} found inbox messages\033[0m")
            return "work", None

        # ─── ④ 无消息 → 扫描任务看板，尝试自动认领 ───
        unclaimed = scan_unclaimed_tasks()
        if unclaimed:
            task_data = unclaimed[0]
            result = claim_task(task_data["id"], agent_name)
            if "Claimed" in result:
                # ④-1 认领成功：若任务绑定了 worktree，把目录告诉模型
                wt_info = ""
                if task_data.get("worktree"):
                    wt_path = WORKTREES_DIR / task_data["worktree"]
                    wt_info = f"\nWork directory: {wt_path}"
                messages.append({"role": "user",
                    "content": f"<auto-claimed>Task {task_data['id']}: "
                               f"{task_data['subject']}{wt_info}</auto-claimed>"})
                print(f"  \033[32m[idle] {name} auto-claimed: "
                      f"{task_data['subject']}\033[0m")
                return "work", task_data["id"]
            # ④-2 认领失败（被抢/依赖未完成）→ 继续下一轮
            print(f"  \033[33m[idle] {name} claim failed: "
                  f"{result}\033[0m")

    # ─── ⑤ 全部轮询空转 → 超时退出 ───
    print(f"  \033[31m[idle] {name} timeout ({IDLE_TIMEOUT}s)\033[0m")
    return "timeout", None


# ── Teammate Thread (from s15 + s16 + s17 + s18) ──

def spawn_teammate_thread(name: str, role: str, prompt: str) -> str:
    """启动一个自治队友线程（s17 沿用 + s18 worktree cwd）。

    执行流程：
      ① 防重名：已存在同名队友 → 拒绝
      ② 组装队友 system prompt（s18 提示：有 worktree 就在那工作）
      ③ 定义 8 个队友工具
      ④ 定义 run() 线程主体（WORK → IDLE → SHUTDOWN）
      ⑤ 注册 active_teammates + 启动线程，返回确认

    ★ s18 新增：队友的 bash/read/write 通过 wt_ctx 在 worktree 里执行，
      system prompt 也提示模型"有 worktree 就在该目录工作"——
      双管齐下确保隔离生效。
    """
    if name in active_teammates:
        return f"Teammate '{name}' already exists"

    system = (f"You are '{name}', a {role}. "
              f"Use tools to complete tasks. "
              f"You can list and claim tasks from the board. "
              f"If a task has a worktree, work in that directory.")

    def handle_inbox_message(name: str, msg: dict, messages: list):
        """分发队友收件箱里的协议消息（WORK 阶段用，s16 沿用）。

        执行流程：
          ① 取消息类型 + metadata
          ② shutdown_request → 回 shutdown_response + 返回 True（要求退出）
          ③ plan_approval_response → 注入 [Plan approved]/[Plan rejected]
             到上下文 + 返回 False（继续工作）
          ④ 其他类型 → 返回 False

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
        """队友线程主体：WORK → IDLE → SHUTDOWN（s17 沿用 + s18 cwd 切换）。

        ★ s18 核心：wt_ctx 记录"当前该队友在哪个 worktree 工作"，
          bash/read_file/write_file 全部以 wt_ctx["path"] 为 cwd 执行——
          这样队友的工作天然落在自己的隔离目录，不会互相覆盖。
        """
        # 当前 worktree 路径（None = 在主仓库 WORKDIR 工作）
        # ★ 用可变 dict 而非局部变量：嵌套闭包要修改它（见 _run_claim_task）
        wt_ctx = {"path": None}

        def _wt_cwd() -> Path | None:
            """返回当前 worktree 路径（None 则用默认 WORKDIR）。"""
            p = wt_ctx["path"]
            return Path(p) if p else None

        def _run_bash(command: str) -> str:
            """在 worktree cwd 下执行 bash（s18 关键改造）。"""
            return run_bash(command, cwd=_wt_cwd())

        def _run_read(path: str) -> str:
            return run_read(path, cwd=_wt_cwd())

        def _run_write(path: str, content: str) -> str:
            return run_write(path, content, cwd=_wt_cwd())

        def _run_list_tasks():
            tasks = list_tasks()
            if not tasks:
                return "No tasks."
            return "\n".join(
                f"  {t.id}: {t.subject} [{t.status}]"
                + (f" (wt:{t.worktree})" if t.worktree else "")
                for t in tasks)

        def _run_claim_task(task_id: str):
            """认领任务，成功后把 wt_ctx 切到该任务的 worktree。

            ★ s18 关键点：
              认领成功 → 若任务绑定 worktree，队友后续工具都在那个目录跑；
              无绑定 → wt_ctx 置 None（回主仓库）。
            """
            result = claim_task(task_id, owner=name)
            if "Claimed" in result:
                task = load_task(task_id)
                if task.worktree:
                    wt_ctx["path"] = str(WORKTREES_DIR / task.worktree)
                else:
                    wt_ctx["path"] = None
            return result

        def _run_complete_task(task_id: str):
            """完成任务，并把 wt_ctx 重置为 None（活干完了，回主仓库）。

            ★ 防止下个任务还在上一个 worktree 里干活——状态必须清干净。
            """
            result = complete_task(task_id)
            wt_ctx["path"] = None
            return result

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

        sub_handlers = {
            "bash": _run_bash, "read_file": _run_read,
            "write_file": _run_write,
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
            # ─── 身份重注入（s17）───
            # 消息过短说明被 autoCompact 压缩过 → 重新注入身份
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
                            "content": "<inbox>" + json.dumps(non_protocol) + "</inbox>"})

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

            # ─── IDLE 阶段（s17）───
            idle_result, claimed_task_id = idle_poll(name, messages, name, role)
            if idle_result == "shutdown":
                break  # IDLE 收到关机 → 退出
            if idle_result == "timeout":
                break  # 60s 空转 → SHUTDOWN
            # ─── s18 新增：IDLE 认领了带 worktree 的任务 → 切 cwd ───
            # idle_poll 返回的 claimed_task_id 告诉外层"我认领了哪个任务"，
            # 据此把 wt_ctx 切到该任务的 worktree（与 _run_claim_task 同理，
            # 但那是工具认领，这是 idle 自动认领，两条路径都要处理）。
            if idle_result == "work" and claimed_task_id:
                task = load_task(claimed_task_id)
                if task.get("worktree"):
                    wt_ctx["path"] = str(WORKTREES_DIR / task["worktree"])
                else:
                    wt_ctx["path"] = None

        # Summary
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

    active_teammates[name] = True
    threading.Thread(target=run, daemon=True).start()
    print(f"  \033[36m[teammate] {name} spawned as {role}\033[0m")
    return f"Teammate '{name}' spawned as {role} (autonomous)"


def _teammate_submit_plan(from_name: str, plan: str) -> str:
    """队友提交计划给 Lead 审批（plan_approval 协议，s16 沿用）。

    执行流程：
      ① 生成 request_id
      ② 注册 ProtocolState（status=pending）→ Lead 的响应靠它匹配
      ③ 发 plan_approval_request 给 Lead
      ④ 返回等待提示（模型能看到）
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

    ★ 注意：发的是普通 "message" 类型，不是协议消息——
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


# ── Lead Worktree Tools (s18 new) ──
# 这三个是 Lead 工具层薄包装，直接转发到 Worktree System 的核心函数。
# 目的是把"工具调用"与"核心逻辑"分开：TOOL_HANDLERS 只做转发，
# 核心逻辑（create/remove/keep）可独立测试。

def run_create_worktree(name: str, task_id: str = "") -> str:
    """Lead 工具：创建 worktree（可绑定任务）。"""
    return create_worktree(name, task_id)


def run_remove_worktree(name: str, discard_changes: bool = False) -> str:
    """Lead 工具：移除 worktree（默认拒绝有改动的）。"""
    return remove_worktree(name, discard_changes)


def run_keep_worktree(name: str) -> str:
    """Lead 工具：保留 worktree 供人工审查。"""
    return keep_worktree(name)


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
        + (f" (wt:{t.worktree})" if t.worktree else "")
        for t in tasks)


def run_get_task(task_id: str) -> str:
    return get_task_json(task_id)


def run_claim_task(task_id: str) -> str:
    return claim_task(task_id, owner="agent")


def run_complete_task(task_id: str) -> str:
    return complete_task(task_id)


def run_spawn_teammate(name: str, role: str, prompt: str) -> str:
    return spawn_teammate_thread(name, role, prompt)


def run_send_message(to: str, content: str) -> str:
    BUS.send("lead", to, content)
    return f"Sent to {to}"


def run_check_inbox() -> str:
    """Lead 的 check_inbox 工具：读收件箱并格式化展示（s16 沿用）。

    ★ 走统一入口 consume_lead_inbox（路由协议 + 消费式读取）。
      注意副作用：读取会清空收件箱文件——详见 README 的
      "inbox 消费式读取"分析。
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
    # s18 new: worktree tools
    {"name": "create_worktree",
     "description": "Create an isolated git worktree with its own branch.",
     "input_schema": {"type": "object",
                      "properties": {"name": {"type": "string"},
                                     "task_id": {"type": "string"}},
                      "required": ["name"]}},
    {"name": "remove_worktree",
     "description": "Remove a worktree. Refuses if uncommitted changes unless discard_changes=true.",
     "input_schema": {"type": "object",
                      "properties": {"name": {"type": "string"},
                                     "discard_changes": {"type": "boolean"}},
                      "required": ["name"]}},
    {"name": "keep_worktree",
     "description": "Keep a worktree for manual review.",
     "input_schema": {"type": "object",
                      "properties": {"name": {"type": "string"}},
                      "required": ["name"]}},
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
    "create_worktree": run_create_worktree,
    "remove_worktree": run_remove_worktree,
    "keep_worktree": run_keep_worktree,
}


# ── Context ──

MEMORY_DIR = WORKDIR / ".memory"
MEMORY_INDEX = MEMORY_DIR / "MEMORY.md"


def update_context(context: dict, messages: list) -> dict:
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
    print("s18: worktree isolation")
    print("Enter a question, press Enter to send. Type q to quit.\n")
    history = []                    # Lead 的对话历史
    context = {"memories": ""}      # 记忆上下文（s09/s10）
    while True:
        try:
            query = input("\033[36ms18 >> \033[0m")
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
