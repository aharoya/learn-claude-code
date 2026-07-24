#!/usr/bin/env python3
"""
s07: 技能加载 — 两级按需知识注入。

  Layer 1（廉价，始终加载）：
    SYSTEM 提示词包含技能名称 + 一行描述（约 100 tokens/技能）
    "Skills available: agent-builder, code-review, mcp-builder, pdf"

  Layer 2（昂贵，按需加载）：
    Agent 调用 load_skill("code-review") → 通过 tool_result
    注入完整的 SKILL.md 内容（约 2000 tokens/技能）

  skills/
    agent-builder/SKILL.md
    code-review/SKILL.md
    mcp-builder/SKILL.md
    pdf/SKILL.md

Changes from s06:
  + build_system() — 启动时扫描 skills/ 目录，将目录注入 SYSTEM
  + load_skill(name) — 通过 tool_result 返回完整 SKILL.md 内容
  + SKILLS_DIR + SKILL_REGISTRY
  循环不变：load_skill 通过 TOOL_HANDLERS 自动分发。

Run: python s07_skill_loading/demo_code.py
Needs: pip install anthropic python-dotenv pyyaml + ANTHROPIC_API_KEY in .env
"""

# ======================================================================
# 执行流程（入口 → 结束）
# ======================================================================
#
#   1. 启动程序 → if __name__ == "__main__" 入口
#
#   2. 加载环境变量，配置常量：
#      WORKDIR、SKILLS_DIR、client、MODEL、CURRENT_TODOS
#
#   3. 【s07 新增】技能系统初始化：
#      a. _scan_skills() 扫描 skills/ 目录
#         ├─ 遍历每个子目录 → 找到 SKILL.md
#         ├─ _parse_frontmatter() 解析 YAML 元数据
#         └─ 填充 SKILL_REGISTRY（name/description/content）
#      b. build_system() 将技能目录注入 SYSTEM 提示词
#         Layer 1：所有技能的名称 + 一行描述（~100 tokens/技能）
#
#   4. 注册所有 Hook 回调
#
#   5. 主循环等待用户输入（while True → input("s07 >> ")）
#
#   6. 用户输入 → Hook → 追加到 messages → agent_loop(history)
#
#   7. agent_loop 核心循环（与 s06 一致）：
#
#      a. nag 提醒检查（>=3 轮未更新 todo → 注入提醒）
#      b. 调用 LLM（SYSTEM 中已包含技能目录）
#      c. 检查 stop_reason
#      d. 遍历 tool_use block：
#         i.   PreToolUse Hook → 权限 + 日志
#         ii.  TOOL_HANDLERS 分发执行
#
#              【s07 新增】如果 LLM 调用了 load_skill 工具：
#                → load_skill("code-review") 被调用：
#                → 从 SKILL_REGISTRY 查找完整 SKILL.md 内容
#                → 通过 tool_result 返回给 LLM（~2000 tokens）
#                Layer 2：完整技能内容注入 LLM 上下文
#
#         iii. PostToolUse Hook
#         iv.  todo_write → nag 归零
#         v.   结果收集
#
#      e. results 追加到 messages → 回到步骤 a
#
#   8. agent_loop 返回 → 打印 LLM 最终文本 → 回到步骤 5
#
#   9. 用户输入 q/exit/空行 → 程序退出
# ======================================================================

import ast, json, os, subprocess
from pathlib import Path
import yaml

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
SKILLS_DIR = WORKDIR / "skills"                               # 技能文件目录（每个子目录 = 一个技能）
client = Anthropic(base_url=os.getenv("ANTHROPIC_BASE_URL"))  # Anthropic 客户端
MODEL = os.environ["MODEL_ID"]                                # 模型 ID
CURRENT_TODOS: list[dict] = []                                # 全局待办列表

# ═══════════════════════════════════════════════════════════
#  NEW in s07: 技能系统 —— 两级知识注入
#
#  设计思想：
#    领域知识（API 文档、最佳实践、代码规范）对 Agent 的工作
#    质量至关重要，但不能全部塞进 SYSTEM 提示词——那会耗尽
#    token 预算且使上下文冗长。
#
#    解决方案：两级注入。
#    Layer 1（廉价）：启动时扫描 skills/，只将技能名称 + 一行描述
#                     注入 SYSTEM。LLM 知道"有哪些技能可以用"。
#    Layer 2（昂贵）：LLM 调用 load_skill("xxx") 时，通过 tool_result
#                     注入完整 SKILL.md 内容。只在需要时才消耗 token。
#
#    类比：Layer 1 = 图书馆目录，Layer 2 = 取书翻看。
# ═══════════════════════════════════════════════════════════
def _parse_frontmatter(text: str) -> tuple[dict, str]:
    """解析 SKILL.md 的 YAML 前置元数据。

    SKILL.md 的结构：
      ---
      name: agent-builder
      description: Build custom agents
      ---
      # 正文内容...

    参数 text：SKILL.md 的完整文本。
    返回：(meta_dict, body_text)。
      meta_dict 包含 name、description 等元数据字段。
      body_text 是 YAML 块之后的正文内容。
    如果没有 frontmatter 或解析失败，返回 ({}, text)。
    """
    if not text.startswith("---"):
        return {}, text
    parts = text.split("---", 2)
    if len(parts) < 3:
        return {}, text
    try:
        meta = yaml.safe_load(parts[1]) or {}
    except yaml.YAMLError:
        meta = {}
    return meta, parts[2].strip()

# ---- 技能注册表：内存中的技能索引 ----
# 结构：{ "skill-name": {"name": ..., "description": ..., "content": ...}, ... }
# 启动时由 _scan_skills() 初始化，运行时由 load_skill() 查询。
SKILL_REGISTRY: dict[str, dict] = {}

def _scan_skills():
    """启动时扫描 skills/ 目录，填充 SKILL_REGISTRY。

    遍历 SKILLS_DIR 下的每个子目录：
      1. 查找 SKILL.md 文件
      2. 解析 YAML frontmatter 获取 name + description
      3. 将 name、description、完整 content 存入 SKILL_REGISTRY

    如果 skills/ 目录不存在，静默跳过（不报错）。
    """
    if not SKILLS_DIR.exists():
        return
    for d in sorted(SKILLS_DIR.iterdir()):
        if not d.is_dir():
            continue
        manifest = d / "SKILL.md"
        if manifest.exists():
            raw = manifest.read_text()
            meta, body = _parse_frontmatter(raw)
            # name 优先取 frontmatter 中的声明，fallback 到目录名
            name = meta.get("name", d.name)
            # description 优先取 frontmatter，fallback 到正文第一行
            desc = meta.get("description", raw.split("\n")[0].lstrip("#").strip())
            SKILL_REGISTRY[name] = {"name": name, "description": desc, "content": raw}

# 启动时立即扫描（模块加载时执行）
_scan_skills()

def list_skills() -> str:
    """列出所有可用技能（名称 + 一行描述）。

    这是 Layer 1 的数据源——build_system() 调用它来生成
    SYSTEM 中的技能目录。

    返回：Markdown 格式的技能列表，每行 "**name**: description"。
          无技能时返回 "(no skills found)"。
    """
    if not SKILL_REGISTRY:
        return "(no skills found)"
    return "\n".join(f"- **{s['name']}**: {s['description']}" for s in SKILL_REGISTRY.values())

# s07: SYSTEM includes skill catalog (cheap — just names + descriptions)
def build_system() -> str:
    """构建包含技能目录的 SYSTEM 提示词。

    启动时调用一次，将 Layer 1 信息嵌入 SYSTEM。
    LLM 在每轮对话都能看到这个目录，知道有哪些技能可以加载。

    返回：完整的 SYSTEM 提示词字符串（约 200-400 chars + 目录）。
    """
    catalog = list_skills()
    return (
        f"You are a coding agent at {WORKDIR}. "
        f"Skills available:\n{catalog}\n"
        "Use load_skill to get full details when needed."
    )

# ---- SYSTEM 提示词（s07 改为运行时构建） ----
# 之前章节直接写死字符串，s07 改为 build_system() 动态生成，
# 因为技能目录的内容在启动时才知道。
SYSTEM = build_system()

# s07: subagent gets its own system prompt — no skill loading, no task
# ---- 子 Agent 系统提示词 ----
# 子 Agent 不需要加载技能（任务单一，父 Agent 已经做了技能选择）
SUB_SYSTEM = (
    f"You are a coding agent at {WORKDIR}. "
    "Complete the task you were given, then return a concise summary. "
    "Do not delegate further."
)


# ═══════════════════════════════════════════════════════════
#  工具实现（6 个标准工具）
#
#  bash/read/write/edit/glob/todo_write 与 s05-s06 完全一致。
# ═══════════════════════════════════════════════════════════

def safe_path(p: str) -> Path:
    """将用户输入的相对路径解析为绝对路径，并确保不逃逸工作目录。

    返回：合法的 Path 对象。
    异常：路径逃逸时抛出 ValueError。
    """
    path = (WORKDIR / p).resolve()
    if not path.is_relative_to(WORKDIR):
        raise ValueError(f"Path escapes workspace: {p}")
    return path

def run_bash(command: str) -> str:
    """执行 Shell 命令并返回 stdout/stderr。

    返回：命令输出，最长 50000 字符；超时 120 秒。
    """
    try:
        r = subprocess.run(command, shell=True, cwd=WORKDIR,
                           capture_output=True, text=True, timeout=120)
        out = (r.stdout + r.stderr).strip()
        return out[:50000] if out else "(no output)"
    except subprocess.TimeoutExpired:
        return "Error: Timeout (120s)"

def run_read(path: str, limit: int | None = None) -> str:
    """读取文件内容。参数 limit 可选，限制行数。"""
    try:
        lines = safe_path(path).read_text().splitlines()
        if limit and limit < len(lines):
            lines = lines[:limit] + [f"... ({len(lines) - limit} more lines)"]
        return "\n".join(lines)
    except Exception as e:
        return f"Error: {e}"

def run_write(path: str, content: str) -> str:
    """将内容写入文件（覆盖写入，自动创建父目录）。"""
    try:
        file_path = safe_path(path)
        file_path.parent.mkdir(parents=True, exist_ok=True)
        file_path.write_text(content)
        return f"Wrote {len(content)} bytes to {path}"
    except Exception as e:
        return f"Error: {e}"

def run_edit(path: str, old_text: str, new_text: str) -> str:
    """精确文本替换（只替换第一次出现）。old_text 必须精确匹配。"""
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
    """按 glob 模式匹配文件列表。"""
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

    LLM 可能以多种格式传入：Python list[dict]、JSON 字符串、AST 字面量。
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
    """更新全局待办列表并打印格式化输出（s05 引入）。"""
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
    if not isinstance(content, list):
        return str(content)
    return "\n".join(getattr(b, "text", "") for b in content if getattr(b, "type", None) == "text")


# ═══════════════════════════════════════════════════════════
#  子 Agent 系统（s06 引入）
#
#  子 Agent 用 SUB_TOOLS（无 task、无 load_skill、无 todo_write）
#  子 Agent 不需要加载技能——父 Agent 已经做了技能选择后委派任务。
# ═══════════════════════════════════════════════════════════

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
# 没有 task → 不能递归创建子 Agent
# 没有 load_skill → 不需要加载技能（父 Agent 已做选择）
# 没有 todo_write → 任务单一，不需分步计划

SUB_HANDLERS = {"bash": run_bash, "read_file": run_read, "write_file": run_write,
                "edit_file": run_edit, "glob": run_glob}

def spawn_subagent(description: str) -> str:
    """创建子 Agent，用全新上下文执行一个子任务（s06 引入）。

    子 Agent 看不到父对话历史（messages 从零开始），
    最多 30 轮，只返回最终文本摘要。
    工具调用同样经过权限 Hook。
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
#  NEW in s07: load_skill —— 运行时按需加载完整技能内容
#
#  当 LLM 看到 SYSTEM 中的技能目录，认为某个技能对当前任务
#  有帮助时，调用 load_skill("skill-name")。
#  此函数从 SKILL_REGISTRY 中查找并返回完整 SKILL.md 内容。
#
#  安全性：通过 SKILL_REGISTRY 查询而非直接读文件系统，
#  防止路径遍历攻击（如 load_skill("../../../etc/passwd")）。
#  只有 _scan_skills() 注册过的技能才能被加载。
# ═══════════════════════════════════════════════════════════

def load_skill(name: str) -> str:
    """从注册表加载完整技能内容（Layer 2 注入）。

    参数 name：技能名称（如 "code-review"、"mcp-builder"）。
          来自 LLM 调用的 load_skill 工具参数。
    返回：完整的 SKILL.md 内容（包含 YAML frontmatter + 正文）；
          技能不存在时返回 "Skill not found: {name}"。

    注：查询走 SKILL_REGISTRY 而非直接读文件系统，
        防止路径遍历攻击。所有可用技能在 _scan_skills()
        阶段已校验完整路径。
    """
    skill = SKILL_REGISTRY.get(name)
    if not skill:
        return f"Skill not found: {name}"
    return skill["content"]


# ═══════════════════════════════════════════════════════════
#  工具定义（TOOLS）—— 发送给 LLM 的 JSON Schema
#
#  s07 新增 load_skill 工具（共 8 个工具）。
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
    # ── s07 新增：load_skill ──
    # LLM 在 SYSTEM 中看到技能目录后，调用此工具获取完整技能内容
    {"name": "load_skill", "description": "Load the full content of a skill by name.",
     "input_schema": {"type": "object", "properties": {"name": {"type": "string"}}, "required": ["name"]}},
]

# ═══════════════════════════════════════════════════════════
#  工具分发映射（TOOL_HANDLERS）—— 工具名 → 执行函数
#
#  通过 LLM 返回的 block.name 查表找到对应的 Python 函数。
#  s07 新增 load_skill 的映射。
# ═══════════════════════════════════════════════════════════
TOOL_HANDLERS = {
    "bash": run_bash, "read_file": run_read, "write_file": run_write,
    "edit_file": run_edit, "glob": run_glob, "todo_write": run_todo_write,
    "task": spawn_subagent, "load_skill": load_skill,
}


# ═══════════════════════════════════════════════════════════
#  钩子系统（s04 引入）
#
#  四种事件：UserPromptSubmit / PreToolUse / PostToolUse / Stop。
#  父 Agent 和子 Agent 的工具执行都经过 Hook。
# ═══════════════════════════════════════════════════════════

HOOKS = {"UserPromptSubmit": [], "PreToolUse": [], "PostToolUse": [], "Stop": []}

def register_hook(event: str, callback):
    """将回调函数注册到指定事件。"""
    HOOKS[event].append(callback)

def trigger_hooks(event: str, *args):
    """触发指定事件上所有已注册的回调。

    返回：第一个非 None 的返回值（拦截信号）；全 None 则放行。
    """
    for callback in HOOKS[event]:
        result = callback(*args)
        if result is not None:
            return result
    return None

# ---- 危险命令黑名单 ----
DENY_LIST = ["rm -rf /", "sudo", "shutdown", "reboot", "mkfs", "dd if="]

def permission_hook(block):
    """PreToolUse：黑名单权限检查。bash 命令命中 DENY_LIST 则拒绝。"""
    if block.name == "bash":
        for p in DENY_LIST:
            if p in block.input.get("command", ""):
                print(f"\n\033[31m⛔ Blocked: '{p}'\033[0m")
                return "Permission denied"
    return None

def log_hook(block):
    """PreToolUse：记录工具调用日志。"""
    print(f"\033[90m[HOOK] {block.name}\033[0m")
    return None

def context_inject_hook(query: str):
    """UserPromptSubmit：打印工作目录信息。"""
    print(f"\033[90m[HOOK] UserPromptSubmit: working in {WORKDIR}\033[0m")
    return None

def summary_hook(messages: list):
    """Stop：统计工具调用次数。"""
    tool_count = sum(1 for m in messages
                     for b in (m.get("content") if isinstance(m.get("content"), list) else [])
                     if isinstance(b, dict) and b.get("type") == "tool_result")
    print(f"\033[90m[HOOK] Stop: session used {tool_count} tool calls\033[0m")
    return None

# ---- 注册 Hook ----
register_hook("UserPromptSubmit", context_inject_hook)
register_hook("PreToolUse", permission_hook)
register_hook("PreToolUse", log_hook)
register_hook("Stop", summary_hook)


# ═══════════════════════════════════════════════════════════
#  agent_loop — 与 s05-s06 结构一致
#
#  load_skill 的处理完全透明——当 LLM 调用 load_skill 时，
#  TOOL_HANDLERS["load_skill"] = load_skill，自动执行。
#  循环代码不需要知道 load_skill 是什么。
# ═══════════════════════════════════════════════════════════

rounds_since_todo = 0  # nag 计数器

def agent_loop(messages: list):
    """Agent 核心循环：在 s06 基础上透明支持技能加载。

    流程：
      1. nag 检查（>= 3 轮未更新 todo → 注入提醒）
      2. 调用 LLM（SYSTEM 含技能目录，TOOLS 含 load_skill）
      3. 检查 stop_reason
      4. 遍历 tool_use block：
         a. PreToolUse Hook
         b. TOOL_HANDLERS 分发（含 load_skill → 按需注入技能内容）
         c. PostToolUse Hook
         d. todo_write → nag 归零
      5. 结果追加 → nag +1 → 回到步骤 1

    参数 messages：消息历史列表（对话上下文）。
    """
    global rounds_since_todo
    while True:
        # --- 步骤 1：nag 提醒 ---
        if rounds_since_todo >= 3 and messages:
            messages.append({"role": "user",
                             "content": "<reminder>Update your todos.</reminder>"})
            rounds_since_todo = 0
        # --- 步骤 2：调用 LLM ---
        response = client.messages.create(
            model=MODEL, system=SYSTEM, messages=messages,
            tools=TOOLS, max_tokens=8000,
        )
        messages.append({"role": "assistant", "content": response.content})

        # --- 步骤 3：LLM 任务完成？ ---
        if response.stop_reason != "tool_use":
            force = trigger_hooks("Stop", messages)
            if force:
                messages.append({"role": "user", "content": force})
                continue
            return

        # --- 步骤 4：计数器递增 ---
        rounds_since_todo += 1
        # --- 步骤 5：处理工具调用 ---
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

            # 5b：工具分发（load_skill 在此通过 TOOL_HANDLERS 自动执行）
            handler = TOOL_HANDLERS.get(block.name)
            output = handler(**block.input) if handler else f"Unknown: {block.name}"

            # 5c：PostToolUse Hook
            trigger_hooks("PostToolUse", block, output)

            # 5d：todo_write → nag 归零
            if block.name == "todo_write":
                rounds_since_todo = 0

            results.append({"type": "tool_result", "tool_use_id": block.id,
                            "content": output})

        # --- 步骤 6：结果回写，继续循环 ---
        messages.append({"role": "user", "content": results})


# ═══════════════════════════════════════════════════════════
#  主入口
# ═══════════════════════════════════════════════════════════
if __name__ == "__main__":
    print("s07: Skill Loading — catalog in SYSTEM, content on demand")
    print("Type a question, press Enter. Type q to quit.\n")

    history = []  # 对话历史，跨轮次复用
    while True:
        try:
            query = input("\033[36ms07 >> \033[0m")
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
