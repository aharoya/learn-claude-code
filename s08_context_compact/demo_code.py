#!/usr/bin/env python3
"""
s08_context_compact.py - 上下文压缩

在每次 LLM 调用前插入四层压缩管道：

    L1: snip_compact      — 消息超 50 条时裁剪中间消息
    L2: micro_compact     — 将旧的 tool_result 替换为占位符
    L3: tool_result_budget — 超大结果持久化到磁盘
    L4: compact_history   — LLM 全文摘要（消耗 1 次 API 调用）

    应急：reactive_compact — 当 API 仍返回 prompt_too_long 时

    ┌─────────────────────────────────────────────────────────────┐
    │  messages[]                                                 │
    │    ↓                                                        │
    │  L3 budget ─→ L1 snip ─→ L2 micro ─→ [token > threshold?]  │
    │                                      ├─ No  → LLM          │
    │                                      └─ Yes → L4 summary   │
    │                                              ↓              │
    │                                          LLM call           │
    │                                    [prompt_too_long?]        │
    │                                      └─ Yes → reactive      │
    └─────────────────────────────────────────────────────────────┘

核心原则：先便宜，后昂贵（cheap first, expensive last）。
执行顺序：budget → snip → micro → auto。

基于 s07 构建（技能加载）。

    python s08_context_compact/demo_code.py
    Needs: pip install anthropic python-dotenv + ANTHROPIC_API_KEY in .env
"""

# ======================================================================
# 执行流程（入口 → 结束）
# ======================================================================
#
#   1. 启动程序 → if __name__ == "__main__" 入口
#
#   2. 加载环境变量 + 扫描技能 + 构建 SYSTEM
#
#   3. 主循环等待用户输入
#
#   4. 用户输入 → 追加到 history → agent_loop(history)
#
#   5. agent_loop 核心循环【s08 重大改动——LLM 调用前先压缩】：
#
#      a. 三层预处理（0 次 API 调用，先便宜）：
#         L3 tool_result_budget   — 超大结果写入磁盘，返回路径引用
#         L1 snip_compact         — 消息 > 50 条时裁剪中间
#         L2 micro_compact        — 旧 tool_result 替换为占位符
#
#      b. 仍然超过阈值 → L4 compact_history（1 次 API 调用）：
#         保存完整转录到磁盘 → LLM 摘要 → 替换整个 messages
#
#      c. try: 调用 LLM
#         except prompt_too_long → reactive_compact（应急摘要）→ retry
#
#      d. stop_reason != "tool_use"？→ 返回
#
#      e. 遍历 tool_use block：
#         i.   【s08 新增】block.name == "compact"？
#              → compact_history + break（结束当前轮，从头开始）
#         ii.  PreToolUse Hook
#         iii. TOOL_HANDLERS 分发
#         iv.  PostToolUse Hook
#         v.   结果收集
#
#      f. 紧凑路径：break 后直接回到步骤 a（messages 已被摘要替换）
#      g. 正常路径：结果追加 → 回到步骤 a
#
#   6. agent_loop 返回 → 打印 LLM 最终文本 → 回到步骤 3
#
#   7. 用户输入 q/exit/空行 → 程序退出
# ======================================================================

import ast, json, os, subprocess, time
from pathlib import Path

# ---- readline：让终端输入支持 UTF-8 和特殊字符（仅 Unix） ----
try:
    import readline
    readline.parse_and_bind('set bind-tty-special-chars off')
except ImportError:
    pass

from anthropic import Anthropic
from dotenv import load_dotenv

# ---- 环境变量 ----
load_dotenv(override=True)
if os.getenv("ANTHROPIC_BASE_URL"): os.environ.pop("ANTHROPIC_AUTH_TOKEN", None)

# ---- 全局常量 ----
WORKDIR = Path.cwd()                                          # 工作目录
SKILLS_DIR = WORKDIR / "skills"                               # 技能目录
TRANSCRIPT_DIR = WORKDIR / ".transcripts"                     # 压缩前完整转录存档目录
TOOL_RESULTS_DIR = WORKDIR / ".task_outputs" / "tool-results" # 超大工具输出持久化目录
client = Anthropic(base_url=os.getenv("ANTHROPIC_BASE_URL"))  # Anthropic 客户端
MODEL = os.environ["MODEL_ID"]                                # 模型 ID
CURRENT_TODOS: list[dict] = []                                # 全局待办列表

# ═══════════════════════════════════════════════════════════
#  技能系统（s07 引入）
#
#  启动时扫描 skills/ 目录，将技能目录注入 SYSTEM。
#  运行时通过 load_skill 按需加载完整内容。
# ═══════════════════════════════════════════════════════════
def _parse_frontmatter(text: str) -> tuple[dict, str]:
    """解析 SKILL.md 的 YAML 前置元数据（简化版，手动解析冒号行）。

    返回：(meta_dict, body_text)。"""
    if not text.startswith("---"):
        return {}, text
    parts = text.split("---", 2)
    if len(parts) < 3:
        return {}, text
    meta = {}
    for line in parts[1].strip().splitlines():
        if ":" in line:
            k, v = line.split(":", 1)
            meta[k.strip()] = v.strip().strip('"').strip("'")
    return meta, parts[2].strip()

# ---- 技能注册表 ----
SKILL_REGISTRY: dict[str, dict] = {}

def _scan_skills():
    """启动时扫描 skills/ 目录，填充 SKILL_REGISTRY。"""
    if not SKILLS_DIR.exists():
        return
    for d in sorted(SKILLS_DIR.iterdir()):
        if not d.is_dir():
            continue
        manifest = d / "SKILL.md"
        if manifest.exists():
            raw = manifest.read_text()
            meta, body = _parse_frontmatter(raw)
            name = meta.get("name", d.name)
            desc = meta.get("description", raw.split("\n")[0].lstrip("#").strip())
            SKILL_REGISTRY[name] = {"name": name, "description": desc, "content": raw}

_scan_skills()

def list_skills() -> str:
    """列出所有可用技能（名称 + 一行描述）。Layer 1 数据源。"""
    if not SKILL_REGISTRY:
        return "(no skills found)"
    return "\n".join(f"- **{s['name']}**: {s['description']}" for s in SKILL_REGISTRY.values())

def load_skill(name: str) -> str:
    """从注册表加载完整技能内容（Layer 2）。通过注册表查询防止路径遍历。"""
    skill = SKILL_REGISTRY.get(name)
    if not skill:
        return f"Skill not found: {name}"
    return skill["content"]

# s08: SYSTEM includes skill catalog (inherited from s07 build_system)
def build_system() -> str:
    """构建包含技能目录的 SYSTEM 提示词。"""
    catalog = list_skills()
    return (
        f"You are a coding agent at {WORKDIR}. "
        f"Skills available:\n{catalog}\n"
        "Use load_skill to get full details when needed."
    )

SYSTEM = build_system()

# s08: subagent gets its own system prompt — no compact, no skill loading
SUB_SYSTEM = (
    f"You are a coding agent at {WORKDIR}. "
    "Complete the task you were given, then return a concise summary. "
    "Do not delegate further."
)


# ═══════════════════════════════════════════════════════════
#  工具实现（6 个标准工具）
#
#  bash/read/write/edit/glob/todo_write 与之前章节一致。
# ═══════════════════════════════════════════════════════════

def safe_path(p: str) -> Path:
    """将用户输入的相对路径解析为绝对路径，确保不逃逸工作目录。"""
    path = (WORKDIR / p).resolve()
    if not path.is_relative_to(WORKDIR): raise ValueError(f"Path escapes workspace: {p}")
    return path

def run_bash(command: str) -> str:
    """执行 Shell 命令并返回 stdout/stderr。输出最长 50000 字符，超时 120 秒。"""
    try:
        r = subprocess.run(command, shell=True, cwd=WORKDIR, capture_output=True, text=True, timeout=120)
        out = (r.stdout + r.stderr).strip()
        return out[:50000] if out else "(no output)"
    except subprocess.TimeoutExpired: return "Error: Timeout (120s)"

def run_read(path: str, limit: int | None = None) -> str:
    """读取文件内容。limit 可选，限制行数。"""
    try:
        lines = safe_path(path).read_text().splitlines()
        if limit and limit < len(lines): lines = lines[:limit] + [f"... ({len(lines) - limit} more lines)"]
        return "\n".join(lines)
    except Exception as e: return f"Error: {e}"

def run_write(path: str, content: str) -> str:
    """将内容写入文件（覆盖写入，自动创建父目录）。"""
    try:
        file_path = safe_path(path); file_path.parent.mkdir(parents=True, exist_ok=True)
        file_path.write_text(content); return f"Wrote {len(content)} bytes to {path}"
    except Exception as e: return f"Error: {e}"

def run_edit(path: str, old_text: str, new_text: str) -> str:
    """精确文本替换（只替换第一次出现）。"""
    try:
        file_path = safe_path(path)
        text = file_path.read_text()
        if old_text not in text: return f"Error: text not found in {path}"
        file_path.write_text(text.replace(old_text, new_text, 1))
        return f"Edited {path}"
    except Exception as e: return f"Error: {e}"

def run_glob(pattern: str) -> str:
    """按 glob 模式匹配文件列表。"""
    import glob as g
    try:
        results = []
        for match in g.glob(pattern, root_dir=WORKDIR):
            if (WORKDIR / match).resolve().is_relative_to(WORKDIR):
                results.append(match)
        return "\n".join(results) if results else "(no matches)"
    except Exception as e: return f"Error: {e}"

def _normalize_todos(todos):
    """验证并标准化待办列表输入（s05 引入）。

    LLM 可能以多种格式传入：Python list[dict]、JSON 字符串、AST 字面量。
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

    每次调用全量替换 CURRENT_TODOS，打印带色彩图标的任务列表。
    """
    global CURRENT_TODOS
    todos, error = _normalize_todos(todos)
    if error:
        return error
    CURRENT_TODOS = todos
    lines = ["\n\033[33m## Current Tasks\033[0m"]
    for t in CURRENT_TODOS:
        icon = {"pending": " ", "in_progress": "\033[36m▸\033[0m", "completed": "\033[32m✓\033[0m"}[t["status"]]
        lines.append(f"  [{icon}] {t['content']}")
    print("\n".join(lines))
    return f"Updated {len(CURRENT_TODOS)} tasks"

def extract_text(content) -> str:
    """从 message content 中提取纯文本（s06 引入）。"""
    if not isinstance(content, list): return str(content)
    return "\n".join(getattr(b, "text", "") for b in content if getattr(b, "type", None) == "text")


# ═══════════════════════════════════════════════════════════
#  子 Agent 系统（s06 引入）
#
#  子 Agent 用 SUB_TOOLS（更少的工具集）和 SUB_SYSTEM。
#=================================================================

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
SUB_HANDLERS = {"bash": run_bash, "read_file": run_read, "write_file": run_write,
                "edit_file": run_edit, "glob": run_glob}

def spawn_subagent(description: str) -> str:
    """创建子 Agent，用全新上下文执行子任务（s06 引入）。

    上下文隔离：messages 从零开始，最多 30 轮，只返回摘要。
    """
    print(f"\n\033[35m[Subagent spawned]\033[0m")
    messages = [{"role": "user", "content": description}]
    for _ in range(30):
        response = client.messages.create(model=MODEL, system=SUB_SYSTEM,
            messages=messages, tools=SUB_TOOLS, max_tokens=8000)
        messages.append({"role": "assistant", "content": response.content})
        if response.stop_reason != "tool_use":
            break
        results = []
        for block in response.content:
            if block.type == "tool_use":
                blocked = trigger_hooks("PreToolUse", block)
                if blocked:
                    results.append({"type": "tool_result", "tool_use_id": block.id,
                                    "content": str(blocked)})
                    continue
                handler = SUB_HANDLERS.get(block.name)
                output = handler(**block.input) if handler else f"Unknown: {block.name}"
                trigger_hooks("PostToolUse", block, output)
                print(f"  \033[90m[sub] {block.name}: {str(output)[:100]}\033[0m")
                results.append({"type": "tool_result", "tool_use_id": block.id, "content": output})
        messages.append({"role": "user", "content": results})
    result = extract_text(messages[-1]["content"])
    if not result:
        for msg in reversed(messages):
            if msg["role"] == "assistant":
                result = extract_text(msg["content"])
                if result:
                    break
        if not result:
            result = "Subagent stopped after 30 turns without final answer."
    print(f"\033[35m[Subagent done]\033[0m")
    return result


# ═══════════════════════════════════════════════════════════
#  NEW in s08: 四层上下文压缩管道
#
#  为什么需要压缩？
#    每轮对话都会往 messages 追加 assistant + user(tool_results)，
#    上下文线性增长。当超过模型上下文窗口时 API 会报错。
#    压缩的目标是在不丢失"关键信息"的前提下削减体积。
#
#  设计原则：先便宜，后昂贵
#    L1-L3 是纯 Python 操作（0 次 API 调用），每次都执行。
#    L4 需要 1 次 LLM 调用，只在 L1-L3 不够时触发。
#    reactive 是应急方案，只在 API 报 prompt_too_long 时触发。
#
#  常量说明：
#    CONTEXT_LIMIT = 50000    触发 L4 的 messages 字符数阈值
#    KEEP_RECENT = 3          L2 保留最近 N 个 tool_result 不被替换
#    PERSIST_THRESHOLD = 30000 L3 单个输出超此值则写入磁盘
# ═══════════════════════════════════════════════════════════

CONTEXT_LIMIT = 50000       # messages 字符串长度阈值，超过触发 L4 摘要
KEEP_RECENT = 3             # micro_compact 保留最近多少个 tool_result
PERSIST_THRESHOLD = 30000   # 单个工具输出超过此字符数则持久化到磁盘

def estimate_size(msgs):
    """估算 messages 列表的字符串长度。

    用于判断是否超过 CONTEXT_LIMIT 阈值。
    简单粗暴的 len(str(messages))，不是精确 token 计数。
    """
    return len(str(msgs))

def _block_type(block):
    """获取 block 的 type 属性（兼容 dict 和 object 两种表示）。

    LLM 的 response.content 返回的是对象（block.type），
    但压缩后的 messages 中可能变成 dict（block["type"]）。
    此函数兼容两种格式。
    """
    return block.get("type") if isinstance(block, dict) else getattr(block, "type", None)


def _message_has_tool_use(msg):
    """判断一条 assistant 消息是否包含 tool_use block。

    用于 snip_compact 的边界保护：不裁剪一个不完整的
    tool_use→tool_result 对（防止 tool_use_id 悬空）。
    """
    if msg.get("role") != "assistant":
        return False
    content = msg.get("content")
    if not isinstance(content, list):
        return False
    return any(_block_type(block) == "tool_use" for block in content)


def _is_tool_result_message(msg):
    """判断一条 user 消息是否包含 tool_result block。

    与 _message_has_tool_use 配对使用，确保裁剪边界不会
    拆散 tool_use ↔ tool_result 的配对关系。
    """
    if msg.get("role") != "user":
        return False
    content = msg.get("content")
    if not isinstance(content, list):
        return False
    return any(isinstance(block, dict) and block.get("type") == "tool_result"
               for block in content)


# ── L1: snipCompact —— 裁剪中间消息 ───────────────────────
#
# 策略：消息超 max_messages 条时，保留头部（最近上下文）
# 和尾部（最早上下文），中间用占位符替换。
#
# 边界保护：裁剪点前后如果有一条不完整的
# tool_use→tool_result 对，调整裁剪范围以确保配对完整。
# 否则 LLM 会收到没有对应 tool_use 的 tool_result（或反过来），
# 导致混乱。
def snip_compact(messages, max_messages=50):
    """裁剪消息列表中间部分，保留头尾（L1 压缩）。

    参数 messages：消息列表。
    参数 max_messages：消息数量阈值，超过则裁剪。
    返回：裁剪后的消息列表（可能包含占位符消息）。

    示例：100 条消息 → 保留前 3 + 后 47，中间 50 条替换为
          "[snipped 50 messages]"。
    """
    if len(messages) <= max_messages: return messages  # 没超过阈值，不裁剪
    # 计算头尾保留数
    keep_head, keep_tail = 3, max_messages - 3
    head_end, tail_start = keep_head, len(messages) - keep_tail
    # 边界保护：确保不在 tool_use→tool_result 之间切割
    # 如果头部结束位置的前一条是 tool_use，
    # 则跳过后续的 tool_result 消息直到遇到非 tool_result
    if head_end > 0 and _message_has_tool_use(messages[head_end - 1]):
        while head_end < len(messages) and _is_tool_result_message(messages[head_end]):
            head_end += 1
    # 如果尾部开始位置是 tool_result 且前一条是 tool_use，
    # 则往前吞并 tool_use（保证配对完整）
    if (tail_start > 0 and tail_start < len(messages)
            and _is_tool_result_message(messages[tail_start])
            and _message_has_tool_use(messages[tail_start - 1])):
        tail_start -= 1
    # 如果头尾已经重叠或交错，不做裁剪
    if head_end >= tail_start:
        return messages
    snipped = tail_start - head_end
    return messages[:head_end] + [{"role": "user", "content": f"[snipped {snipped} messages]"}] + messages[tail_start:]


# ── L2: microCompact —— 旧结果占位符 ──────────────────────
#
# 策略：保留最近 KEEP_RECENT 个 tool_result 的完整内容，
# 更早的 tool_result 替换为短占位符 "[Earlier tool result compacted]"
# 节省大量 token，同时提示 LLM 可以重新执行工具获取结果。
def collect_tool_results(messages):
    """收集所有 tool_result block 的位置信息。

    返回：[(message_index, block_index, block), ...]
    每个元素是一个三元组，记录了 block 在 messages 中的精确位置。
    """
    blocks = []
    for mi, msg in enumerate(messages):
        if msg.get("role") != "user" or not isinstance(msg.get("content"), list): continue
        for bi, block in enumerate(msg["content"]):
            if isinstance(block, dict) and block.get("type") == "tool_result":
                blocks.append((mi, bi, block))
    return blocks

def micro_compact(messages):
    """替换旧 tool_result 为占位符（L2 压缩）。

    保留最后 KEEP_RECENT 个 tool_result 不动，
    更早的（如果内容超过 120 字符）替换为占位符字符串。

    参数 messages：消息列表。
    返回：原地修改后的 messages（同时返回引用）。

    为什么是 120 字符阈值？短输出（如 "Wrote 42 bytes to file"）
    不值得替换，占位符可能比原文还长。
    """
    tool_results = collect_tool_results(messages)
    if len(tool_results) <= KEEP_RECENT: return messages  # 不够多，不压缩
    # 除了最后 KEEP_RECENT 个，其余全部替换
    for _, _, block in tool_results[:-KEEP_RECENT]:
        if len(block.get("content", "")) > 120:
            block["content"] = "[Earlier tool result compacted. Re-run if needed.]"
    return messages


# ── L3: toolResultBudget —— 超大结果持久化 ────────────────
#
# 策略：单次工具调用可能产生超大输出（如读取大文件），
# 这些输出放在 messages 中会直接耗尽 token 预算。
# L3 将超过 PERSIST_THRESHOLD 的输出写入磁盘文件，
# 在 messages 中只保留路径引用 + 前 2000 字符预览。

def persist_large_output(tool_use_id, output):
    """将超大工具输出写入磁盘文件。

    参数 tool_use_id：工具调用的唯一 ID（用作文件名）。
    参数 output：工具输出的字符串。
    返回：如果输出不超过阈值，原样返回；
          否则返回 <persisted-output> XML 块（含文件路径 + 预览）。

    LLM 看到 <persisted-output> 块后可以用 read_file 读取完整内容。
    """
    if len(output) <= PERSIST_THRESHOLD: return output  # 不大，不需要持久化
    TOOL_RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    path = TOOL_RESULTS_DIR / f"{tool_use_id}.txt"
    if not path.exists(): path.write_text(output)
    return f"<persisted-output>\nFull output: {path}\nPreview:\n{output[:2000]}\n</persisted-output>"

def tool_result_budget(messages, max_bytes=200_000):
    """检查最新一轮 tool_result 的总大小，超大则持久化（L3 压缩）。

    只检查最新一条 user 消息中的 tool_result（不检查历史）。
    按内容大小从大到小排序，优先持久化最大的结果，
    直到总量降到 max_bytes 以下。

    参数 messages：消息列表。
    参数 max_bytes：tool_result 总字节数阈值。
    返回：修改后的 messages。
    """
    last = messages[-1] if messages else None
    # 只处理最新的 tool_result 消息
    if not last or last.get("role") != "user" or not isinstance(last.get("content"), list):
        return messages

    # 收集最新消息中的所有 tool_result
    blocks = [(i, b) for i, b in enumerate(last["content"])
              if isinstance(b, dict) and b.get("type") == "tool_result"]
    total = sum(len(str(b.get("content", ""))) for _, b in blocks)

    if total <= max_bytes:
        return messages  # 不超预算，不需要持久化

    # 从大到小排序，优先持久化最大的
    ranked = sorted(blocks, key=lambda p: len(str(p[1].get("content", ""))), reverse=True)
    for _, block in ranked:
        if total <= max_bytes:
            break
        content = str(block.get("content", ""))
        if len(content) <= PERSIST_THRESHOLD:
            continue  # 太小，持久化没意义
        tid = block.get("tool_use_id", "unknown")
        block["content"] = persist_large_output(tid, content)
        total = sum(len(str(b.get("content", ""))) for _, b in blocks)
    return messages


# ── L4: autoCompact —— LLM 全文摘要 ────────────────────────
#
# 当 L1-L3 之后 messages 仍然超过 CONTEXT_LIMIT，
# 触发 L4：先将完整对话保存到磁盘（以防摘要丢失信息），
# 然后调用 LLM 生成摘要，用摘要替换整个 messages。
# 代价：消耗 1 次 API 调用。

def write_transcript(messages):
    """将完整 messages 保存为 JSONL 文件。

    这是"安全网"——即使 LLM 摘要丢失了细节，
    原始转录仍可从磁盘恢复。

    参数 messages：消息列表。
    返回：保存的文件路径。
    """
    TRANSCRIPT_DIR.mkdir(parents=True, exist_ok=True)
    path = TRANSCRIPT_DIR / f"transcript_{int(time.time())}.jsonl"
    with path.open("w") as f:
        for msg in messages: f.write(json.dumps(msg, default=str) + "\n")
    return path

def summarize_history(messages):
    """调用 LLM 生成对话摘要。

    将 messages 截断到 80000 字符后发送给 LLM，
    要求保留：当前目标、关键发现、已读/改的文件、
    剩余工作和用户约束。

    参数 messages：要摘要的消息列表。
    返回：LLM 生成的摘要文本。
    """
    conversation = json.dumps(messages, default=str)[:80000]
    prompt = ("Summarize this coding-agent conversation so work can continue.\n"
              "Preserve: 1. current goal, 2. key findings/decisions, 3. files read/changed, "
              "4. remaining work, 5. user constraints.\nBe compact but concrete.\n\n" + conversation)
    response = client.messages.create(model=MODEL, messages=[{"role": "user", "content": prompt}], max_tokens=2000)
    return "\n".join(
        getattr(block, "text", "")
        for block in response.content
        if getattr(block, "type", None) == "text").strip() or "(empty summary)"

def compact_history(messages):
    """执行完整压缩流程：保存转录 → LLM 摘要 → 返回新 messages。

    这是 L4 的入口函数，被 auto（阈值触发）和 compact 工具两种方式调用。

    参数 messages：要压缩的消息列表。
    返回：新的 messages 列表，只含一条 user 消息 [Compacted]。
    """
    transcript_path = write_transcript(messages)
    print(f"[transcript saved: {transcript_path}]")
    summary = summarize_history(messages)
    # 将整个历史替换为一条摘要消息——"重新开始"但保留关键上下文
    return [{"role": "user", "content": f"[Compacted]\n\n{summary}"}]


# ── Emergency: reactiveCompact —— API 报错时应急 ───────────
#
# 当 L1-L4 之后 API 仍然返回 prompt_too_long，
# 说明消息仍然太大（比如摘要本身都太长）。
# 应急策略：只保留最后 5 条消息的原样 + 前面部分的 LLM 摘要。

def reactive_compact(messages):
    """应急压缩：API 报 prompt_too_long 时触发。

    保留最后 ~5 条消息不动（维持当前交互连续性），
    对更早的部分做 LLM 摘要。

    参数 messages：导致 API 报错的消息列表。
    返回：压缩后的新消息列表。
    """
    transcript = write_transcript(messages)
    # 尾部保留最后 5 条（确保配对完整）
    tail_start = max(0, len(messages) - 5)
    if (tail_start > 0 and tail_start < len(messages)
            and _is_tool_result_message(messages[tail_start])
            and _message_has_tool_use(messages[tail_start - 1])):
        tail_start -= 1  # tool_use → tool_result 配对保护
    summary = summarize_history(messages[:tail_start])
    return [{"role": "user", "content": f"[Reactive compact]\n\n{summary}"}, *messages[tail_start:]]


# ═══════════════════════════════════════════════════════════
#  工具定义（TOOLS）—— 发送给 LLM 的 JSON Schema
#
#  s08 新增 compact 工具（共 9 个工具）。
#  compact 让 LLM 主动触发压缩——与 threshold 自动触发的
#  autoCompact 互补。LLM 可以感知到"上下文快满了"并主动压缩。
#
#  每个工具定义包含三个关键字段：
#    - name：工具名称，LLM 返回的 tool_use block.name
#    - description：工具用途，帮助 LLM 判断何时调用
#    - input_schema：参数 JSON Schema，定义类型和必填项
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
    {"name": "task", "description": "Launch a subagent to handle a complex subtask. Returns only the final conclusion.",
     "input_schema": {"type": "object", "properties": {"description": {"type": "string"}}, "required": ["description"]}},
    {"name": "load_skill", "description": "Load the full content of a skill by name.",
     "input_schema": {"type": "object", "properties": {"name": {"type": "string"}}, "required": ["name"]}},
    # ── s08 新增：compact ──
    # LLM 主动调用此工具来压缩对话历史。
    # focus 参数可选，告诉摘要"请重点关注 XX 方面"。
    # 注意：compact 不走 TOOL_HANDLERS——agent_loop 中特殊处理。
    {"name": "compact", "description": "Summarize earlier conversation to free context space.",
     "input_schema": {"type": "object", "properties": {"focus": {"type": "string"}}}},
]

# ═══════════════════════════════════════════════════════════
#  工具分发映射（TOOL_HANDLERS）—— 工具名 → 执行函数
#
#  通过 LLM 返回的 block.name 查表找到对应的 Python 函数。
#  注意：compact 不在映射中——它在 agent_loop 中特殊处理。
# ═══════════════════════════════════════════════════════════

TOOL_HANDLERS = {
    "bash": run_bash, "read_file": run_read, "write_file": run_write,
    "edit_file": run_edit, "glob": run_glob, "todo_write": run_todo_write,
    "task": spawn_subagent, "load_skill": load_skill,
}

# ═══════════════════════════════════════════════════════════
#  钩子系统
#
#  PreToolUse：工具执行前（权限检查 + 日志）
#  PostToolUse：工具执行后
# ═══════════════════════════════════════════════════════════

HOOKS = {"PreToolUse": [], "PostToolUse": []}
def trigger_hooks(event, *args):
    """触发指定事件上所有已注册的回调。

    返回：第一个非 None 的返回值（拦截信号）；全 None 则放行。
    """
    for cb in HOOKS[event]:
        r = cb(*args)
        if r is not None: return r
    return None

# ---- 危险命令黑名单 ----
DENY_LIST = ["rm -rf /", "sudo", "shutdown"]
def permission_hook(block):
    """PreToolUse：黑名单权限检查。bash 命令命中 DENY_LIST 则拒绝。
    父 Agent 和子 Agent 的工具调用都经过此钩子。
    """
    if block.name == "bash":
        for p in DENY_LIST:
            if p in block.input.get("command", ""): return "Permission denied"
    return None
def log_hook(block):
    """PreToolUse：记录工具调用日志。始终返回 None。"""
    print(f"\033[90m[HOOK] {block.name}\033[0m")
    return None

# ---- 注册 Hook ----
HOOKS["PreToolUse"].append(permission_hook)
HOOKS["PreToolUse"].append(log_hook)


# ═══════════════════════════════════════════════════════════
#  agent_loop — s08 核心：LLM 调用前执行压缩管道
#
#  与 s01-s07 最大的不同：
#    之前：直接 messages → LLM
#    s08：messages → L3 budget → L1 snip → L2 micro →
#         [超阈值?] → L4 summary → LLM
#
#  另一个关键变化：compact 工具的特殊处理。
#  compact 不走 TOOL_HANDLERS，而是在 agent_loop 中直接
#  调用 compact_history + break 结束当前轮次，
#  让新轮次从摘要后的 messages 重新开始。
# ═══════════════════════════════════════════════════════════

MAX_REACTIVE_RETRIES = 1  # reactive_compact 最多重试 1 次

def agent_loop(messages: list):
    """Agent 核心循环：带上下文压缩的 LLM 交互。

    流程：
      1. 三层预处理（0 API 调用）：budget → snip → micro
      2. 超阈值 → L4 摘要（1 API 调用）
      3. try LLM，except prompt_too_long → reactive + retry
      4. 遍历 tool_use：compact 特殊处理，其他走 TOOL_HANDLERS
      5. 结果回写 → 回到步骤 1

    参数 messages：消息历史列表（对话上下文）。
    """
    reactive_retries = 0
    while True:
        # ─── 步骤 1：三层预处理（L3→L1→L2，0 API 调用）───
        # 执行顺序经过考量：budget 先做（减少后续操作的体积），
        # 然后 snip（裁剪条数），最后 micro（替换旧结果占位符）
        messages[:] = tool_result_budget(messages)    # L3：超大结果磁盘持久化
        messages[:] = snip_compact(messages)          # L1：裁剪中间消息
        messages[:] = micro_compact(messages)         # L2：旧结果占位符

        # ─── 步骤 2：L4 阈值检查（1 API 调用）───
        if estimate_size(messages) > CONTEXT_LIMIT:
            print("[auto compact]")
            messages[:] = compact_history(messages)   # 保存转录 → LLM 摘要

        # ─── 步骤 3：调用 LLM ───
        try:
            response = client.messages.create(
                model=MODEL, system=SYSTEM,
                messages=messages, tools=TOOLS, max_tokens=8000,
            )
            reactive_retries = 0  # 成功则重置计数
        except Exception as e:
            # 应急压缩：L1-L4 后仍然超出限制
            if ("prompt_too_long" in str(e).lower()
                    or "too many tokens" in str(e).lower()) \
                    and reactive_retries < MAX_REACTIVE_RETRIES:
                print("[reactive compact]")
                messages[:] = reactive_compact(messages)
                reactive_retries += 1
                continue  # 用压缩后的 messages 重试
            raise  # 非 prompt_too_long 错误，直接抛出

        # ─── 步骤 4：LLM 认为任务完成？ ───
        messages.append({"role": "assistant", "content": response.content})
        if response.stop_reason != "tool_use":
            return  # 任务完成，退出循环

        # ─── 步骤 5：处理工具调用 ───
        results = []
        for block in response.content:
            if block.type != "tool_use": continue
            print(f"\033[36m> {block.name}\033[0m")

            # === s08 特殊处理：compact 工具 ===
            # compact 不走 TOOL_HANDLERS——它需要影响 agent_loop 的控制流。
            # 执行完毕后 break 出当前轮次，让下一轮迭代使用摘要后的 messages。
            if block.name == "compact":
                messages[:] = compact_history(messages)
                results.append({"type": "tool_result", "tool_use_id": block.id,
                                "content": "[Compacted. Conversation history has been summarized.]"})
                messages.append({"role": "user", "content": results})
                break  # 结束当前轮，下一轮用紧凑后的 messages 重新开始

            # 5a：PreToolUse Hook
            blocked = trigger_hooks("PreToolUse", block)
            if blocked:
                results.append({"type": "tool_result", "tool_use_id": block.id, "content": str(blocked)})
                continue
            # 5b：工具分发执行
            handler = TOOL_HANDLERS.get(block.name)
            output = handler(**block.input) if handler else f"Unknown: {block.name}"
            # 5c：PostToolUse Hook
            trigger_hooks("PostToolUse", block, output)
            print(str(output)[:200])
            results.append({"type": "tool_result", "tool_use_id": block.id, "content": str(output)})
        else:
            # ─── 正常路径：没有 compact 被调用 ───
            # for/else 语法：for 循环没有被 break 中断时执行 else
            messages.append({"role": "user", "content": results})
            continue
        # ─── 紧凑路径：compact 被调用了（for 循环被 break）───
        # results 已经追加到 messages，跳到下一轮迭代
        continue


# ═══════════════════════════════════════════════════════════
#  主入口
# ═══════════════════════════════════════════════════════════
if __name__ == "__main__":
    print("s08: Context Compact — four-layer compaction pipeline")
    print("输入问题，回车发送。输入 q 退出。\n")
    history = []
    while True:
        try: query = input("\033[36ms08 >> \033[0m")
        except (EOFError, KeyboardInterrupt): break
        if query.strip().lower() in ("q", "exit", ""): break
        history.append({"role": "user", "content": query})
        agent_loop(history)
        for block in history[-1]["content"]:
            if getattr(block, "type", None) == "text": print(block.text)
        print()
