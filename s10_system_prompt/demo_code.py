#!/usr/bin/env python3
"""
s10: 系统提示词 —— 运行时组装 + 确定性缓存。

Run:  python s10_system_prompt/demo_code.py
Need: pip install anthropic python-dotenv + .env with ANTHROPIC_API_KEY

Changes from s09:
  - PROMPT_SECTIONS: 按主题分段的提示词片段字典
  - assemble_system_prompt(context): 根据真实状态选择 + 拼接片段
  - get_system_prompt(context): 通过 json.dumps 做确定性缓存
  - agent_loop 使用 get_system_prompt(context) 替代硬编码 SYSTEM

记忆片段在 .memory/MEMORY.md 存在且有内容时才加载（基于真实状态，而非关键词）。
"""

# ======================================================================
# 执行流程（入口 → 结束）
# ======================================================================
#
#   1. 启动程序 → 初始化 PROMPT_SECTIONS + 常量
#
#   2. 主循环等待用户输入
#
#   3. update_context({}, []) + get_system_prompt(context) 构建初始 SYSTEM
#
#   4. 用户输入 → 追加到 history → agent_loop(history, context)
#
#   5. agent_loop 核心循环：
#
#      a. 使用 get_system_prompt(context) 获取 SYSTEM（可能命中缓存）
#
#      b. 调用 LLM
#
#      c. stop_reason != "tool_use"？→ 返回
#
#      d. 遍历 tool_use → TOOL_HANDLERS 分发
#
#      e.【s10 新增】每轮工具执行后重新评估 context：
#         update_context → 检查 .memory/MEMORY.md 是否变化
#         get_system_prompt → 缓存命中/失效 → 可能重新组装
#
#      f. 结果回写 → 回到步骤 a（SYSTEM 可能是新版）
#
#   6. agent_loop 返回 → update_context → 打印 LLM 文本 → 回到步骤 2
#
#   7. 用户输入 q/exit/空行 → 程序退出
# ======================================================================

import os, subprocess, json
from pathlib import Path

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
MEMORY_DIR = WORKDIR / ".memory"            # 记忆目录（存在检查用，不直接读写）
MEMORY_INDEX = MEMORY_DIR / "MEMORY.md"     # 记忆索引文件
client = Anthropic(base_url=os.getenv("ANTHROPIC_BASE_URL"))  # Anthropic 客户端
MODEL = os.environ["MODEL_ID"]              # 模型 ID


# ═══════════════════════════════════════════════════════════
#  NEW in s10: 提示词片段系统
#
#  设计思想：
#    之前章节的 SYSTEM 提示词要么是写死的字符串（s01-s06），
#    要么是启动时一次性构建（s07-s09）。这两种方式都有问题：
#
#    1. 写死 = 无法根据运行时状态调整（有记忆？没记忆？有 MCP？）
#    2. 一次性构建 = 状态变了提示词不更新（运行时写了新记忆，但 SYSTEM 没变）
#
#    s10 的解决方案：
#    - 将 SYSTEM 拆成独立"片段"（PROMPT_SECTIONS）
#    - 运行时根据 context（真实状态）选择哪些片段生效
#    - 每轮工具执行后重新评估（状态可能变化）
#    - 用确定性缓存避免不必要的重复拼接
#
#  类比：C 语言的 #ifdef 条件编译，但这里的"条件"是运行时状态。
# ═══════════════════════════════════════════════════════════

# ---- 提示词片段字典：按主题分段，可独立开关 ----
# 每个片段是一个 key → 文本的映射。
# assemble_system_prompt 根据 context 决定包含哪些片段。
# 新增片段只需加一个 key，不需要改其他代码。
# 注意：tools 和 workspace 不再是硬编码的 PROMPT_SECTIONS 条目，
# 而是从 context 动态组装——update_context 每轮工具执行后重新评估，
# 将 enabled_tools 和 workspace 写入 context，这里从 context 读取。
# 这样新增工具或工作目录变化时无需修改 PROMPT_SECTIONS 常量。
PROMPT_SECTIONS = {
    "identity": "You are a coding agent. Act, don't explain.",
}


def assemble_system_prompt(context: dict) -> str:
    """根据当前上下文选择并拼接提示词片段。

    这是 SYSTEM 的"组装车间"——决定哪些片段生效。
    组装逻辑有三类：
      - 始终加载：identity（身份描述）
      - 动态组装：tools 从 context["enabled_tools"] 拼接列表，
                  workspace 从 context["workspace"] 读取
      - 条件加载：memory（仅在 context["memories"] 非空时）

    参数 context：update_context 返回的上下文字典。
    返回：完整的 SYSTEM 提示词字符串。
    """
    sections = []

    # 始终加载的基础片段
    sections.append(PROMPT_SECTIONS["identity"])

    # 动态组装：从 context 读取真实状态
    # 注意：这里没有直接读 PROMPT_SECTIONS，而是从 context 获取，
    # 因为 context 由 update_context 每轮从真实状态推导，
    # 确保 tools 列表和 workspace 路径始终是最新的。
    tools = ", ".join(context.get("enabled_tools", []))
    if tools:
        sections.append(f"Available tools: {tools}.")
    sections.append(f"Working directory: {context.get("workspace", WORKDIR)}")

    # 条件加载：MEMORY.md 有内容时才注入记忆
    memories = context.get("memories", "")
    if memories:
        sections.append(f"Relevant memories:\n{memories}")

    return "\n\n".join(sections)


# ═══════════════════════════════════════════════════════════
#  确定性缓存
#
#  为什么不用 Python 的 hash()？
#    - hash() 有进程随机化（PYTHONHASHSEED），跨进程不安全
#    - hash() 对嵌套 dict/list 无法直接计算
#  为什么用 json.dumps？
#    - sort_keys=True 保证键顺序一致
#    - ensure_ascii=False 保留非 ASCII 字符
#    - default=str 处理无法序列化的对象
#    - 输出是确定性字符串，相同输入永远相同输出
#
#  注意：此缓存仅避免进程内的重复字符串拼接。
#  真正的 Claude Code 还通过 stable section ordering 和
#  SYSTEM_PROMPT_DYNAMIC_BOUNDARY 保护 API 级别的提示词缓存。
# ═══════════════════════════════════════════════════════════

_last_context_key = None  # 上一次 context 的 JSON 序列化结果
_last_prompt = None       # 上一次组装完成的 SYSTEM 字符串

def get_system_prompt(context: dict) -> str:
    """获取 SYSTEM 提示词（带缓存）。

    仅在 context 发生变化时才重新调用 assemble_system_prompt。
    缓存命中时打印 [cache hit]，未命中时打印 [assembled] + 已加载的片段列表。

    参数 context：当前上下文字典。
    返回：SYSTEM 提示词字符串。
    """
    global _last_context_key, _last_prompt
    # 将 context 序列化为确定性字符串作为缓存 key
    key = json.dumps(context, sort_keys=True, ensure_ascii=False, default=str)
    # 缓存命中 → 直接返回
    if key == _last_context_key and _last_prompt:
        print("  \033[90m[cache hit] system prompt unchanged\033[0m")
        return _last_prompt
    # 缓存未命中 → 重新组装
    _last_context_key = key
    _last_prompt = assemble_system_prompt(context)

    # 打印哪些片段被加载（方便调试）
    loaded = ["identity", "tools", "workspace"]
    if context.get("memories"):
        loaded.append("memory")
    print(f"  \033[32m[assembled] sections: {', '.join(loaded)}\033[0m")
    return _last_prompt


# ═══════════════════════════════════════════════════════════
#  工具实现（3 个基本工具）
#
#  s10 聚焦系统提示词设计，只保留最基本的工具集。
# ═══════════════════════════════════════════════════════════

def safe_path(p: str) -> Path:
    """将用户输入的相对路径解析为绝对路径，确保不逃逸工作目录。"""
    path = (WORKDIR / p).resolve()
    if not path.is_relative_to(WORKDIR):
        raise ValueError(f"Path escapes workspace: {p}")
    return path


def run_bash(command: str) -> str:
    """执行 Shell 命令并返回 stdout/stderr。最长 50000 字符，超时 120 秒。"""
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
        file_path = safe_path(path)
        file_path.parent.mkdir(parents=True, exist_ok=True)
        file_path.write_text(content)
        return f"Wrote {len(content)} bytes to {path}"
    except Exception as e:
        return f"Error: {e}"


# ═══════════════════════════════════════════════════════════
#  工具定义（TOOLS）—— 发送给 LLM 的 JSON Schema
#
#  s10 最简工具集：bash/read/write 三个。
#  每个工具定义包含三个关键字段：
#    - name：工具名称，LLM 返回的 tool_use block.name
#    - description：工具用途，帮助 LLM 判断何时调用
#    - input_schema：参数 JSON Schema，定义类型和必填项
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
]

# ═══════════════════════════════════════════════════════════
#  工具分发映射（TOOL_HANDLERS）—— 工具名 → 执行函数
# ═══════════════════════════════════════════════════════════
TOOL_HANDLERS = {"bash": run_bash, "read_file": run_read, "write_file": run_write}


# ═══════════════════════════════════════════════════════════
#  上下文评估
#
#  update_context 是 s10 的核心——从"真实状态"推导 context。
#  之前章节的 context 要么没有，要么是隐含的（函数内部读全局变量）。
#  s10 将其显式化：context 是一个 dict，在所有函数间传递。
# ═══════════════════════════════════════════════════════════

def update_context(context: dict, messages: list) -> dict:
    """从真实状态推导上下文字典。

    这是"条件编译"的条件来源——assemble_system_prompt 根据此字典
    决定哪些提示词片段生效。

    当前推导项：
      - enabled_tools：从 TOOL_HANDLERS 的 keys 推导（有哪些工具可用）
      - workspace：当前工作目录路径
      - memories：MEMORY.md 的内容（文件存在且非空时），文件不存在则为 ""

    参数 context：先前的上下文字典（可能被覆盖的值）。
    参数 messages：消息历史（当前未使用，预留给后续扩展如对话分析）。
    返回：新的上下文字典。
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
#  agent_loop — 运行时组装 SYSTEM 提示词
#
#  与之前章节的关键变化：
#    1. SYSTEM 不再是全局常量，而是通过 get_system_prompt(context) 获取
#    2. 每次工具执行后重新评估 context（update_context），
#       确保 SYSTEM 反映最新状态（如刚写入的 MEMORY.md）
#    3. get_system_prompt 内部有缓存——context 不变时不重新拼接字符串
# ═══════════════════════════════════════════════════════════

def agent_loop(messages: list, context: dict):
    """Agent 核心循环：使用动态组装的 SYSTEM 提示词。

    流程：
      1. get_system_prompt(context) → 组装 SYSTEM（可能命中缓存）
      2. 调用 LLM
      3. 检查 stop_reason
      4. 遍历 tool_use → TOOL_HANDLERS 分发
      5. 结果回写
      6. update_context → get_system_prompt → 可能重新组装 → 回到步骤 1

    参数 messages：消息历史列表。
    参数 context：当前上下文字典（在 agent_loop 内部会更新）。
    """
    system = get_system_prompt(context)
    while True:
        # --- 步骤 1：调用 LLM（使用动态 SYSTEM） ---
        response = client.messages.create(
            model=MODEL, system=system, messages=messages,
            tools=TOOLS, max_tokens=8000)
        messages.append({"role": "assistant", "content": response.content})
        # --- 步骤 2：LLM 任务完成？ ---
        if response.stop_reason != "tool_use":
            return  # 完成，返回给调用方

        # --- 步骤 3：工具分发执行 ---
        results = []
        for block in response.content:
            if block.type != "tool_use":
                continue
            print(f"\033[36m> {block.name}\033[0m")
            handler = TOOL_HANDLERS.get(block.name)
            output = handler(**block.input) if handler else f"Unknown: {block.name}"
            print(str(output)[:200])
            results.append({"type": "tool_result",
                            "tool_use_id": block.id, "content": output})
        messages.append({"role": "user", "content": results})

        # --- 步骤 4：【s10 新增】每轮工具执行后重新评估 context ---
        # 这一轮可能写入了 MEMORY.md 或创建了新文件，
        # 下一轮 LLM 应该看到更新后的 SYSTEM
        context = update_context(context, messages)
        system = get_system_prompt(context)


# ═══════════════════════════════════════════════════════════
#  主入口
# ═══════════════════════════════════════════════════════════
if __name__ == "__main__":
    print("s10: system prompt — runtime assembly")
    print("Enter a question, press Enter to send. Type q to quit.\n")
    history = []
    # 启动时初始化 context
    # context的结构是：
    # {
    #     "enabled_tools": list(TOOL_HANDLERS.keys()),
    #     "workspace": str(WORKDIR),
    #     "memories": memories,
    # }
    # update_context()这个函数是重新构建context，和历史context以及messages并没有关系，函数入参并没有用到
    # 但是在单轮agent_loop中context是有用到的，用来动态构建system_prompt
    context = update_context({}, [])
    while True:
        try:
            query = input("\033[36ms10 >> \033[0m")
        except (EOFError, KeyboardInterrupt):
            break
        if query.strip().lower() in ("q", "exit", ""):
            break
        history.append({"role": "user", "content": query})
        agent_loop(history, context)
        # agent_loop 返回后重新评估 context（状态可能已变化）
        context = update_context(context, history)
        # 打印 LLM 最终文本
        for block in history[-1]["content"]:
            if getattr(block, "type", None) == "text":
                print(block.text)
        print()
