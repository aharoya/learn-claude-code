#!/usr/bin/env python3
"""
s01_agent_loop.py - Agent 核心循环

整个 AI 编程 Agent 的秘密都在一个模式里：

    while stop_reason == "tool_use":
        response = LLM(messages, tools)
        execute tools
        append results

    +----------+      +-------+      +---------+
    |   User   | ---> |  LLM  | ---> |  Tool   |
    |  prompt  |      |       |      | execute |
    +----------+      +---+---+      +----+----+
                          ^               |
                          |   tool_result |
                          +---------------+
                          (loop continues)

This is the core loop: feed tool results back to the model
until the model decides to stop. Production agents layer
policy, hooks, and lifecycle controls on top.

Usage:
    pip install anthropic python-dotenv
    ANTHROPIC_API_KEY=... python s01_agent_loop/demo_code.py
"""

# ======================================================================
# 执行流程（入口 → 结束）
# ======================================================================
#
#   1. 启动程序 → if __name__ == "__main__" 入口
#
#   2. 加载环境变量（load_dotenv + Anthropic 客户端初始化）
#      配置常量：client、MODEL、SYSTEM、TOOLS
#
#   3. 主循环等待用户输入（while True → input("s01 >> ")）
#
#   4. 用户输入 → 追加到 messages → 进入 agent_loop(history)
#
#   5. agent_loop 核心循环：
#
#      a. 调用 LLM（client.messages.create）
#
#      b. 追加 assistant 消息到 messages
#
#      c. stop_reason != "tool_use"？→ 返回（LLM 认为任务完成）
#
#      d. stop_reason == "tool_use" → 遍历 response.content：
#         i.   找到 tool_use block → 打印命令（黄色）
#         ii.  调用 run_bash(command) 执行
#         iii. 打印输出前 200 字符
#         iv.  构造 tool_result 对象
#
#      e. 所有结果收集到 results → 追加到 messages → 回到步骤 a
#
#   6. agent_loop 返回 → 打印 LLM 最终文本响应 → 回到步骤 3
#
#   7. 用户输入 q/exit/空行 → 程序退出
# ======================================================================

import os
import subprocess

# ---- readline：让终端输入支持 UTF-8 和特殊字符（仅 Unix） ----
# macOS 的 libedit 在处理中文输入时有退格问题，这四行修复它
try:
    import readline
    # macOS 的 libedit 在处理中文输入时有退格问题，这四行修复它
    readline.parse_and_bind('set bind-tty-special-chars off')
    readline.parse_and_bind('set input-meta on')
    readline.parse_and_bind('set output-meta on')
    readline.parse_and_bind('set convert-meta off')
except ImportError:
    pass

from anthropic import Anthropic
from dotenv import load_dotenv

# ---- 环境变量：加载 .env 文件，配置 API 端点和模型 ----
# ANTHROPIC_BASE_URL：兼容第三方 API（DeepSeek/GLM/Kimi 等）
# MODEL_ID：指定模型名称
# ANTHROPIC_API_KEY：API 密钥
load_dotenv(override=True)

if os.getenv("ANTHROPIC_BASE_URL"):
    os.environ.pop("ANTHROPIC_AUTH_TOKEN", None)

# ---- 全局常量 ----
client = Anthropic(base_url=os.getenv("ANTHROPIC_BASE_URL"))  # Anthropic 客户端（兼容多 provider）
MODEL = os.environ["MODEL_ID"]                                # 模型 ID（从环境变量读取）
SYSTEM = f"You are a coding agent at {os.getcwd()}. Use bash to solve tasks. Act, don't explain."  # 系统提示词

# ═══════════════════════════════════════════════════════════
#  工具定义（TOOLS）—— 发送给 LLM 的 JSON Schema
#
#  这段列表定义 Agent 有哪些"手"可以用。
#  它被直接传入 client.messages.create(tools=TOOLS)，
#  LLM 根据这些定义判断何时调用哪个工具、传什么参数。
#
#  每个工具定义包含三个关键字段：
#    - name：工具名称，LLM 返回的 tool_use block.name 就是它
#    - description：工具用途说明，帮助 LLM 判断"现在该用这个吗？"
#    - input_schema：参数 JSON Schema，定义类型、属性和必填项
#
#  s01 只有一个工具：bash。这是最简单的形态。
#  后续章节会逐步增加 read、write、edit、glob 等工具。
# ═══════════════════════════════════════════════════════════
TOOLS = [{
    "name": "bash",
    "description": "Run a shell command.",
    "input_schema": {
        "type": "object",
        "properties": {"command": {"type": "string"}},
        "required": ["command"],
    },
}]


# ── 工具执行 ─────────────────────────────────────────────────
def run_bash(command: str) -> str:
    """执行 Shell 命令并返回 stdout/stderr。

    安全措施：内置危险命令黑名单（rm -rf /、sudo 等），
    包含这些字符串的命令会被直接拒绝。

    参数 command：要执行的 shell 命令字符串。
    返回：命令输出，最长 50000 字符；超时 120 秒。
    """
    # 简单黑名单：s01 的最小安全机制（s03 会升级为正式权限系统）
    dangerous = ["rm -rf /", "sudo", "shutdown", "reboot", "> /dev/"]
    if any(d in command for d in dangerous):
        return "Error: Dangerous command blocked"
    try:
        r = subprocess.run(command, shell=True, cwd=os.getcwd(),
                           capture_output=True, text=True, timeout=120)
        out = (r.stdout + r.stderr).strip()
        return out[:50000] if out else "(no output)"
    except subprocess.TimeoutExpired:
        return "Error: Timeout (120s)"
    except (FileNotFoundError, OSError) as e:
        return f"Error: {e}"


# ── 核心循环：while True 中调用工具直到模型停止 ──────────────
def agent_loop(messages: list):
    """Agent 核心循环：与 LLM 交互直到任务完成。

    这是整个教程中最重要的函数——后续所有章节（s02-s20）
    都在这个循环之上叠加机制，循环本身保持不变。

    流程：
      1. 调用 LLM（client.messages.create），传入 messages + tools
      2. 追加 assistant 消息到 messages
      3. 检查 stop_reason：
         - != "tool_use" → 模型正常结束，返回
         - == "tool_use" → 遍历 tool_use block，执行工具
      4. 将 tool_result 追加到 messages → 回到步骤 1

    参数 messages：消息历史列表，格式为 [{"role": "user", "content": ...}, ...]
    """
    while True:
        # --- 步骤 1：调用 LLM ---
        response = client.messages.create(
            model=MODEL, system=SYSTEM, messages=messages,
            tools=TOOLS, max_tokens=8000,
        )

        # --- 步骤 2：追加 assistant 消息 ---
        messages.append({"role": "assistant", "content": response.content})

        # --- 步骤 3：判断 LLM 是否完成了任务 ---
        if response.stop_reason != "tool_use":
            # 不是工具调用 → 模型认为任务已完成，退出循环
            return

        # --- 步骤 4：遍历所有 tool_use block，逐个执行 ---
        results = []
        for block in response.content:
            if block.type == "tool_use":
                # 打印即将执行的命令（黄色，用户可观察 Agent 在做什么）
                print(f"\033[33m$ {block.input['command']}\033[0m")
                # 执行命令
                output = run_bash(block.input["command"])
                # 打印输出前 200 字符（避免刷屏）
                print(output[:200])
                # 构造 tool_result 对象，tool_use_id 用于 LLM 关联请求和响应
                results.append({
                    "type": "tool_result",
                    "tool_use_id": block.id,
                    "content": output,
                })

        # --- 步骤 5：工具结果追加到 messages，继续循环 ---
        # LLM 下一轮会看到这些结果，根据结果决定下一步行动
        messages.append({"role": "user", "content": results})


# ── 入口 ─────────────────────────────────────────────────────
if __name__ == "__main__":
    print("s01: Agent Loop")
    print("输入问题，回车发送。输入 q 退出。\n")

    # history：跨轮次共享的对话历史
    # 每轮对话的上下文都累积在这里，Agent 会"记住"之前做过什么
    history = []
    while True:
        try:
            query = input("\033[36ms01 >> \033[0m")
        except (EOFError, KeyboardInterrupt):
            break
        if query.strip().lower() in ("q", "exit", ""):
            break

        # 追加用户消息
        history.append({"role": "user", "content": query})

        # 进入核心循环
        agent_loop(history)

        # 打印 LLM 最终文本响应（绿色省略了颜色，直接打印）
        response_content = history[-1]["content"]
        if isinstance(response_content, list):
            for block in response_content:
                if getattr(block, "type", None) == "text":
                    print(block.text)
        print()
