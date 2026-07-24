#!/usr/bin/env python3
"""
s11: 错误恢复 —— 三条恢复路径 + 指数退避。

Run:  python s11_error_recovery/demo_code.py
Need: pip install anthropic python-dotenv + .env with ANTHROPIC_API_KEY

Changes from s10:
  - LLM 调用包裹在 try/except 中，含三条恢复路径
  - Path 1: max_tokens → 升级 8K→64K（首次升级不追加截断输出），
            然后 continuation prompt（最多 3 次）
  - Path 2: prompt_too_long → reactive compact → 重试（最多 1 次）
  - Path 3: 429/529 → 带抖动的指数退避（最多 10 次），
            连续 529 时切换到 fallback 模型
  - with_retry 包装器处理瞬时错误
  - RecoveryState 追踪升级/压缩/529/模型切换状态

ASCII 流程:
  messages → prompt assembly → compress+load → [try] LLM [except] → tools → loop
                                                    |          |
                                              stop_reason   error type
                                              max_tokens?   prompt_too_long? → compact
                                              escalate /    429/529? → backoff
                                              continue      other? → log + exit
"""

# ======================================================================
# 执行流程（入口 → 结束）
# ======================================================================
#
#   1. 启动 → 初始化 RecoveryState + context + SYSTEM
#
#   2. 主循环等待用户输入
#
#   3. 用户输入 → agent_loop(history, context)
#
#   4. agent_loop 核心循环（每次 LLM 调用都包裹在错误恢复中）：
#
#      a. with_retry(llm_call, state) 调用 LLM
#         内部处理 429/529 → 自动退避重试（最多 10 次）
#
#      b. 如果 with_retry 仍失败 → 进入外层的三条恢复路径：
#
#         Path 1 (max_tokens)：
#         首次 → max_tokens 8K→64K，不追加截断输出，重试同一请求
#         非首次 → 追加 continuation prompt（最多 3 次）
#
#         Path 2 (prompt_too_long)：
#         reactive_compact → 保留最后 5 条消息 → 重试（1 次）
#
#         Path 3 (不可恢复)：
#         打印错误 → 构造 error 消息 → 返回
#
#      c. stop_reason == "tool_use" → TOOL_HANDLERS 分发
#
#      d. 结果回写 → update_context → 回到步骤 a
#
#   5. agent_loop 返回 → 打印 LLM 文本 → 回到步骤 2
# ======================================================================

import os, subprocess, time, random, json
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
WORKDIR = Path.cwd()                            # 工作目录
MEMORY_DIR = WORKDIR / ".memory"                # 记忆目录
MEMORY_INDEX = MEMORY_DIR / "MEMORY.md"         # 记忆索引
client = Anthropic(base_url=os.getenv("ANTHROPIC_BASE_URL"))  # Anthropic 客户端
PRIMARY_MODEL = os.environ["MODEL_ID"]          # 主模型
FALLBACK_MODEL = os.getenv("FALLBACK_MODEL_ID") # 降级模型（连续 529 时切换）

# ═══════════════════════════════════════════════════════════
#  错误恢复常量
# ═══════════════════════════════════════════════════════════

ESCALATED_MAX_TOKENS = 64000   # max_tokens 升级后的上限（首次命中时从 8K→64K）
DEFAULT_MAX_TOKENS = 8000      # 默认 max_tokens
MAX_RECOVERY_RETRIES = 3       # continuation prompt 最多重试次数
MAX_RETRIES = 10               # 429/529 退避重试上限
BASE_DELAY_MS = 500            # 退避基础延迟（毫秒）
MAX_CONSECUTIVE_529 = 3        # 连续 529 多少次后切换模型

# continuation prompt：LLM 输出被截断时追加的消息
# 要求 LLM 直接从中断点继续，不道歉、不复述
CONTINUATION_PROMPT = (
    "Output token limit hit. Resume directly — "
    "no apology, no recap. Pick up mid-thought."
)


# ═══════════════════════════════════════════════════════════
#  提示词组装系统（s10 引入）
#
#  运行时根据 context 选择 + 拼接提示词片段。
#  带确定性缓存（json.dumps），context 不变时不重新拼接。
# ═══════════════════════════════════════════════════════════

PROMPT_SECTIONS = {
    "identity": "You are a coding agent. Act, don't explain.",
    "tools": "Available tools: bash, read_file, write_file.",
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
        print("  \033[90m[cache hit] system prompt unchanged\033[0m")
        return _last_prompt
    _last_context_key = key
    _last_prompt = assemble_system_prompt(context)
    loaded = ["identity", "tools", "workspace"]
    if context.get("memories"):
        loaded.append("memory")
    print(f"  \033[32m[assembled] sections: {', '.join(loaded)}\033[0m")
    return _last_prompt


# ═══════════════════════════════════════════════════════════
#  工具实现（3 个基本工具）
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
        file_path = safe_path(path)
        file_path.parent.mkdir(parents=True, exist_ok=True)
        file_path.write_text(content)
        return f"Wrote {len(content)} bytes to {path}"
    except Exception as e:
        return f"Error: {e}"


# ═══════════════════════════════════════════════════════════
#  工具定义（TOOLS）—— 发送给 LLM 的 JSON Schema
#
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
]

# ═══════════════════════════════════════════════════════════
#  工具分发映射（TOOL_HANDLERS）—— 工具名 → 执行函数
# ═══════════════════════════════════════════════════════════

TOOL_HANDLERS = {"bash": run_bash, "read_file": run_read, "write_file": run_write}


# ═══════════════════════════════════════════════════════════
#  NEW in s11: 错误恢复系统
#
#  设计思想：
#    Agent 是无人值守的长期运行进程。网络抖、API 限流、
#    输出截断、上下文超限都是常态，不是异常。
#    错误恢复不是一个"额外功能"——它是 Agent 可靠性的基石。
#
#  三条恢复路径对应三类最常见的失败模式：
#    Path 1 (max_tokens)：输出被截断——先扩大 token 上限，再提示继续
#    Path 2 (prompt_too_long)：输入太大——压缩上下文后重试
#    Path 3 (429/529)：服务端限流/过载——智能退避 + 模型降级
#
#  RecoveryState 追踪恢复尝试次数，防止无限重试。
# ═══════════════════════════════════════════════════════════

class RecoveryState:
    """追踪 agent_loop 中的恢复尝试状态。

    属性：
      has_escalated：max_tokens 是否已从 8K 升级到 64K
      recovery_count：已发送 continuation prompt 次数
      consecutive_529：连续收到 529 响应的次数
      has_attempted_reactive_compact：是否已尝试 reactive_compact
      current_model：当前使用的模型 ID（可能因 529 切换为 FALLBACK）
    """
    def __init__(self):
        self.has_escalated = False
        self.recovery_count = 0
        self.consecutive_529 = 0
        self.has_attempted_reactive_compact = False
        self.current_model = PRIMARY_MODEL


def retry_delay(attempt, retry_after=None):
    """计算指数退避延迟（秒），带随机抖动。

    公式：min(500ms * 2^attempt, 32000ms) + 25% 随机抖动

    抖动（jitter）是防止"惊群效应"——如果多个 Agent 同时收到 429，
    都等同样的时间后再同时重试，只会再次全部失败。随机抖动打破同步。

    参数 attempt：当前重试次数（0-based）。
    参数 retry_after：API 返回的 Retry-After 头（秒），优先级最高。
    返回：等待延迟（秒）。
    """
    if retry_after:
        return retry_after  # 以 API 响应头为准
    base = min(BASE_DELAY_MS * (2 ** attempt), 32000) / 1000  # 指数退避，上限 32 秒
    jitter = random.uniform(0, base * 0.25)                    # 25% 随机抖动
    return base + jitter


def with_retry(fn, state: RecoveryState):
    """对瞬时错误（429/529）自动指数退避重试。

    这是"内层"恢复——在 agent_loop 的每一轮 LLM 调用中生效。
    只处理 429（限流）和 529（过载）两种瞬时错误。
    其他错误（max_tokens/prompt_too_long/其他）不在这里处理，
    直接 re-raise 给外层的 agent_loop try/except。

    特殊逻辑：
      - 连续 MAX_CONSECUTIVE_529 次 529 → 切换到 FALLBACK_MODEL
      - 切换模型后重置 consecutive_529 计数器

    参数 fn：要执行的 LLM 调用 lambda（由 agent_loop 提供）。
    参数 state：恢复状态追踪对象。
    返回：LLM 响应对象。
    """
    for attempt in range(MAX_RETRIES):
        try:
            result = fn()
            state.consecutive_529 = 0  # 成功则重置 529 计数器
            return result
        except Exception as e:
            name = type(e).__name__
            msg = str(e).lower()

            # ── 429：限流 → 指数退避 ──
            if "ratelimit" in name.lower() or "429" in msg:
                delay = retry_delay(attempt)
                print(f"  \033[33m[429 rate limit] retry {attempt+1}/{MAX_RETRIES},"
                      f" wait {delay:.1f}s\033[0m")
                time.sleep(delay)
                continue

            # ── 529：过载 → 指数退避 + 可能切换模型 ──
            if "overloaded" in name.lower() or "529" in msg or "overloaded" in msg:
                state.consecutive_529 += 1
                # 连续 529 超阈值 → 切换降级模型
                if state.consecutive_529 >= MAX_CONSECUTIVE_529:
                    if FALLBACK_MODEL:
                        state.current_model = FALLBACK_MODEL
                        state.consecutive_529 = 0
                        print(f"  \033[31m[529 x{MAX_CONSECUTIVE_529}]"
                              f" switching to {FALLBACK_MODEL}\033[0m")
                    else:
                        state.consecutive_529 = 0
                        print(f"  \033[31m[529 x{MAX_CONSECUTIVE_529}]"
                              f" no FALLBACK_MODEL_ID configured, continuing retry\033[0m")
                delay = retry_delay(attempt)
                print(f"  \033[33m[529 overloaded] retry {attempt+1}/{MAX_RETRIES},"
                      f" wait {delay:.1f}s\033[0m")
                time.sleep(delay)
                continue

            # 不是瞬时错误（max_tokens/prompt_too_long/其他）→ 交给外层处理
            raise

    raise RuntimeError(f"Max retries ({MAX_RETRIES}) exceeded")


def is_prompt_too_long_error(e: Exception) -> bool:
    """检测 API 错误是否为"提示词/上下文太长"。

    覆盖 Anthropic 和兼容 API 的多种错误消息格式。
    """
    msg = str(e).lower()
    return (("prompt" in msg and "long" in msg)
            or "prompt_is_too_long" in msg
            or "context_length_exceeded" in msg
            or "max_context_window" in msg)


def reactive_compact(messages: list) -> list:
    """应急压缩：保留最后 5 条消息，前面替换为提示。

    教学简化版——只保留尾部。真正的 Claude Code 会：
      1. 调用 LLM 对前半部分做摘要
      2. 将摘要 + 尾部 5 条作为新 messages
      3. 重试 API 调用

    这里的简化版是因为 s08/s09 已经覆盖了 LLM 摘要的逻辑。
    """
    print("  \033[31m[reactive compact] trimming to last 5 messages\033[0m")
    tail = messages[-5:]
    return [{"role": "user",
             "content": "[Reactive compact] Earlier conversation trimmed. "
                        "Continue from where you left off."}, *tail]


# ═══════════════════════════════════════════════════════════
#  上下文评估（s10 引入）
# ═══════════════════════════════════════════════════════════

def update_context(context: dict, messages: list) -> dict:
    """从真实状态推导上下文字典。检查 MEMORY_INDEX 是否存在。

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
#  agent_loop — 带三条恢复路径的 LLM 交互
#
#  结构：with_retry（内层，处理 429/529）
#         └─ agent_loop try/except（外层，处理 max_tokens/prompt_too_long）
#
#  Path 1 (max_tokens) 的设计说明：
#    首次命中 → 升级 token 上限但不追加截断输出。
#    因为 8K 截断时 LLM 的输出通常是不完整的（半句话），
#    追加到 messages 会让 LLM 看到"一句没说完的话"。
#    更好的做法：用 64K 重新请求同一个 prompt，让 LLM 完整输出。
#
#    64K 仍然截断 → 追加截断输出 + "Please continue"。
#    这时 LLM 至少有完整的思路前半段作为上下文。
#    最多追加 3 次 continuation prompt。
# ═══════════════════════════════════════════════════════════

def agent_loop(messages: list, context: dict):
    """Agent 核心循环：带错误恢复的 LLM 交互。

    流程：
      1. get_system_prompt(context) → SYSTEM
      2. with_retry(LLM call) → 自动处理 429/529
      3. 检查 stop_reason：
         - max_tokens → Path 1（升级/continuation）
         - 正常 → 追加 assistant + 检查 tool_use
      4. except（with_retry 未处理）：
         - prompt_too_long → Path 2（reactive compact ×1）
         - 其他 → 不可恢复，构造 error 消息 → 返回
      5. 工具分发 → 结果回写 → 回到步骤 1

    参数 messages：消息历史列表。
    参数 context：当前上下文字典。
    """
    system = get_system_prompt(context)
    state = RecoveryState()
    max_tokens = DEFAULT_MAX_TOKENS

    while True:
        # ─── 步骤 1：LLM 调用（with_retry 内部处理 429/529） ───
        try:
            response = with_retry(
                lambda mt=max_tokens, mdl=state.current_model:
                    client.messages.create(
                        model=mdl, system=system, messages=messages,
                        tools=TOOLS, max_tokens=mt),
                state)
        except Exception as e:
            # with_retry 没处理 = 非瞬时错误 → 进入路径判断

            # ─── Path 2: prompt_too_long → reactive compact（1 次） ───
            if is_prompt_too_long_error(e):
                if not state.has_attempted_reactive_compact:
                    messages[:] = reactive_compact(messages)
                    state.has_attempted_reactive_compact = True
                    continue  # 用压缩后的 messages 重试
                # compact 后仍然太长 → 放弃
                print("  \033[31m[unrecoverable] still too long after compact\033[0m")
                messages.append({"role": "assistant", "content": [
                    {"type": "text",
                     "text": "[Error] Context too large, cannot continue."}]})
                return

            # ─── Path 3: 不可恢复错误 → 优雅降级 ───
            name = type(e).__name__
            print(f"  \033[31m[unrecoverable] {name}: {str(e)[:100]}\033[0m")
            messages.append({"role": "assistant", "content": [
                {"type": "text", "text": f"[Error] {name}: {str(e)[:200]}"}]})
            return

        # ─── Path 1: max_tokens 截断 ───
        if response.stop_reason == "max_tokens":
            # 首次截断：扩大上限到 64K，不追加截断输出
            # （避免 LLM 看到半句不完整的话）
            if not state.has_escalated:
                max_tokens = ESCALATED_MAX_TOKENS
                state.has_escalated = True
                print(f"  \033[33m[max_tokens] escalating"
                      f" {DEFAULT_MAX_TOKENS} -> {ESCALATED_MAX_TOKENS}\033[0m")
                continue  # 用更大的 max_tokens 重试同一请求

            # 64K 仍截断：追加截断输出 + continuation prompt
            messages.append({"role": "assistant", "content": response.content})
            if state.recovery_count < MAX_RECOVERY_RETRIES:
                messages.append({"role": "user", "content": CONTINUATION_PROMPT})
                state.recovery_count += 1
                print(f"  \033[33m[max_tokens] continuation"
                      f" {state.recovery_count}/{MAX_RECOVERY_RETRIES}\033[0m")
                continue  # LLM 收到 "Please continue" 后从中断点继续

            # continuation 次数用尽 → 放弃
            print("  \033[31m[max_tokens] recovery limit reached\033[0m")
            return

        # ─── 正常完成 ───
        messages.append({"role": "assistant", "content": response.content})

        if response.stop_reason != "tool_use":
            return  # LLM 完成，返回

        # ─── 工具分发 ───
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

        # ─── 每轮重新评估 context ───
        context = update_context(context, messages)
        system = get_system_prompt(context)


# ═══════════════════════════════════════════════════════════
#  主入口
# ═══════════════════════════════════════════════════════════
if __name__ == "__main__":
    print("s11: error recovery")
    print("Enter a question, press Enter to send. Type q to quit.\n")

    history = []
    context = update_context({}, [])
    while True:
        try:
            query = input("\033[36ms11 >> \033[0m")
        except (EOFError, KeyboardInterrupt):
            break
        if query.strip().lower() in ("q", "exit", ""):
            break

        turn_start = len(history)  # 记录本轮起始位置（用于只打印本轮文本）
        history.append({"role": "user", "content": query})
        agent_loop(history, context)
        context = update_context(context, history)

        # 只打印本轮新增的 assistant 文本
        for msg in history[turn_start:]:
            if msg.get("role") != "assistant":
                continue
            for block in msg["content"]:
                if getattr(block, "type", None) == "text":
                    print(block.text)
        print()
