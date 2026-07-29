#!/usr/bin/env python3
"""
s09_memory.py - 记忆系统

为 Agent 提供跨会话的持久化知识。

存储结构：
    .memory/
      MEMORY.md          ← 索引文件（每个记忆一行，最多 200 行）
      *.md               ← 单个记忆文件（Markdown + YAML frontmatter）

四种记忆类型：
    user       — 用户偏好（如"用 4 空格缩进"）
    feedback   — 用户反馈（如"以后不要用 sed -i"）
    project    — 项目事实（如"这个 repo 用 pytest"）
    reference  — 外部引用（如"API 文档地址"）

agent_loop 中的生命周期：
    1. 将 MEMORY.md 索引加载到 SYSTEM 提示词（廉价，始终存在）
    2. 根据当前对话选择相关记忆 → 注入完整内容
    3. 运行 s08 的压缩管道
    4. 每轮结束后从原始 messages 提取新记忆
    5. 记忆数 >= 10 时定期合并去重（Dream）

基于 s08 构建（上下文压缩）。

    python s09_memory/demo_code.py
    Needs: pip install anthropic python-dotenv + ANTHROPIC_API_KEY in .env
"""

# ======================================================================
# 执行流程（入口 → 结束）
# ======================================================================
#
#   1. 启动程序 → 初始化 MEMORY_DIR + 常量
#
#   2. 主循环等待用户输入
#
#   3. 用户输入 → 追加到 history → agent_loop(history)
#
#   4. agent_loop 开始：
#      a. load_memories(messages) → LLM 选择相关记忆文件
#      b. build_system() → SYSTEM 含记忆索引
#      c. 保存 pre_compress 快照（用于后续提取记忆）
#
#   5. LLM 调用前压缩管道（同 s08）：
#      L3 budget → L1 snip → L2 micro → [超阈值?] L4 summary
#
#   6. 记忆注入：将相关记忆内容拼接到当前 user 消息前面
#
#   7. 调用 LLM
#
#   8. stop_reason != "tool_use"？
#      → extract_memories(pre_compress) + consolidate_memories() → 返回
#
#   9. stop_reason == "tool_use" → 遍历 tool_use → TOOL_HANDLERS 分发
#
#   10. 结果回写 → 回到步骤 5
#
#   11. agent_loop 返回 → 打印 LLM 文本 → 回到步骤 2
# ======================================================================

import os, subprocess, json, time, re
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
# override=True：允许 .env 中的值覆盖已存在的环境变量。
# 如果设置了 ANTHROPIC_BASE_URL（使用第三方兼容接口），需要移除
# ANTHROPIC_AUTH_TOKEN，避免与 ANTHROPIC_API_KEY 冲突。
# Anthropic SDK 优先用 ANTHROPIC_API_KEY（header），如果同时存在
# ANTHROPIC_AUTH_TOKEN（header），两个 header 都发会被服务端拒掉。
if os.getenv("ANTHROPIC_BASE_URL"): os.environ.pop("ANTHROPIC_AUTH_TOKEN", None)

# ---- 全局常量 ----
WORKDIR = Path.cwd()                                          # 工作目录
MEMORY_DIR = WORKDIR / ".memory"                              # 记忆存储目录
MEMORY_DIR.mkdir(exist_ok=True)                               # 启动时确保目录存在
MEMORY_INDEX = MEMORY_DIR / "MEMORY.md"                       # 记忆索引文件路径
SKILLS_DIR = WORKDIR / "skills"                               # 技能目录（s07 引入）
TRANSCRIPT_DIR = WORKDIR / ".transcripts"                     # 转录存档目录（s08 引入）
TOOL_RESULTS_DIR = WORKDIR / ".task_outputs" / "tool-results" # 工具输出持久化（s08 引入）
# Anthropic 客户端。base_url 从 .env 取，不设则用 SDK 默认值
# （api.anthropic.com）。设了 ANTHROPIC_BASE_URL 就可以指向
# 任意兼容 Anthropic 接口的第三方提供商（DeepSeek/GLM 等）。
client = Anthropic(base_url=os.getenv("ANTHROPIC_BASE_URL"))  # Anthropic 客户端
# MODEL_ID 必须在 .env 中设置，如 "claude-sonnet-4-20250514"
# 或第三方模型的兼容 ID（如 "deepseek-chat"）。
MODEL = os.environ["MODEL_ID"]                                # 模型 ID


# ═══════════════════════════════════════════════════════════
#  NEW in s09: 记忆系统
#
#  设计思想：
#    之前的 Agent 每次启动都是"失忆"状态，不知道用户的偏好、
#    之前的反馈、项目的约定。s09 引入持久化记忆，让 Agent
#    在多次会话之间保持"认知连续性"。
#
#  记忆的生命周期：
#    写入：extract_memories() 从对话中提取 → write_memory_file()
#    读取：build_system() 含索引 + load_memories() 按需注入内容
#    维护：consolidate_memories() 去重合并旧记忆（Dream）
#
#  存储格式：每个记忆一个 .md 文件，YAML frontmatter + 正文
#    ---
#    name: use-4-space-indent
#    description: 用户偏好四空格缩进
#    type: user
#    ---
#    用户喜欢用 4 空格缩进而不是 Tab。
# =====================================================================

# 四种记忆类型，用于分类和筛选
MEMORY_TYPES = ["user", "feedback", "project", "reference"]

def _parse_frontmatter(text: str) -> tuple[dict, str]:
    """解析 YAML frontmatter（手动冒号解析，不依赖 pyyaml）。

    frontmatter 是 Markdown 文件开头 `---` 之间的一段 YAML 元数据：
        ---
        name: use-tab
        type: user
        ---
        正文内容...

    text.split("---", 2) 的 maxsplit=2 是关键：
      第 0 段：开头的空字符串（因为 text 以 "---" 开头）
      第 1 段：frontmatter 内容
      第 2 段：正文（可能含 "---"，比如代码块里）
    用 maxsplit=2 确保只按前两个 "---" 分割，正文里的不误伤。

    返回：(meta_dict, body_text)。
    """
    if not text.startswith("---"):
        return {}, text
    # maxsplit=2：最多切 2 刀，得到 3 段。
    # 如果正文里也有 "---"（比如代码块 `---`），
    # 不设 maxsplit 会继续切，只留最后一段，丢失大部分正文。
    parts = text.split("---", 2)
    if len(parts) < 3:
        return {}, text
    meta = {}
    for line in parts[1].strip().splitlines():
        if ":" in line:
            k, v = line.split(":", 1)
            meta[k.strip()] = v.strip().strip('"').strip("'")
    return meta, parts[2].strip()


def write_memory_file(name: str, mem_type: str, description: str, body: str):
    """将一条记忆写入 .memory/ 目录的独立 .md 文件。

    参数 name：记忆名称（如 "use-4-space-indent"）。
    参数 mem_type：记忆类型（user/feedback/project/reference）。
    参数 description：一行描述，用于索引查找。
    参数 body：完整记忆内容（Markdown 格式）。

    文件命名：name 的 kebab-case slug 化 → "use-4-space-indent.md"。
    写完后自动调用 _rebuild_index() 更新 MEMORY.md。

    返回：写入的文件路径。
    """
    slug = name.lower().replace(" ", "-").replace("/", "-")
    filename = f"{slug}.md"
    filepath = MEMORY_DIR / filename
    filepath.write_text(
        f"---\nname: {name}\ndescription: {description}\ntype: {mem_type}\n---\n\n{body}\n"
    )
    _rebuild_index()
    return filepath


def _rebuild_index():
    """从所有 .md 文件重建 MEMORY.md 索引。

    扫描 .memory/*.md（排除 MEMORY.md 自身），
    提取每条记忆的名称和描述，生成索引行：
      - [name](filename.md) — description

    索引被 build_system() 注入 SYSTEM 提示词，每轮对话都可见。
    """
    lines = []
    for f in sorted(MEMORY_DIR.glob("*.md")):
        if f.name == "MEMORY.md":
            continue  # 不索引自身
        raw = f.read_text()
        meta, body = _parse_frontmatter(raw)
        name = meta.get("name", f.stem)
        desc = meta.get("description", body.split("\n")[0][:80])
        lines.append(f"- [{name}]({f.name}) — {desc}")
    MEMORY_INDEX.write_text("\n".join(lines) + "\n" if lines else "")


def read_memory_index() -> str:
    """读取 MEMORY.md 索引内容。

    被 build_system() 调用，内容注入 SYSTEM 提示词。
    返回：索引文本（无记忆时返回空字符串）。
    """
    if not MEMORY_INDEX.exists():
        return ""
    text = MEMORY_INDEX.read_text().strip()
    return text if text else ""


def read_memory_file(filename: str) -> str | None:
    """读取单个记忆文件的完整内容。

    参数 filename：文件名（如 "use-4-space-indent.md"）。
    返回：文件完整文本；不存在返回 None。
    """
    path = MEMORY_DIR / filename
    if not path.exists():
        return None
    return path.read_text()


def list_memory_files() -> list[dict]:
    """列出所有记忆文件的元数据。

    返回：[{filename, name, description, type, body}, ...]
    用于 select_relevant_memories 和 consolidate_memories。
    """
    result = []
    for f in sorted(MEMORY_DIR.glob("*.md")):
        if f.name == "MEMORY.md":
            continue
        raw = f.read_text()
        meta, body = _parse_frontmatter(raw)
        result.append({
            "filename": f.name,
            "name": meta.get("name", f.stem),
            "description": meta.get("description", ""),
            "type": meta.get("type", "user"),
            "body": body,
        })
    return result


def select_relevant_memories(messages: list, max_items: int = 5) -> list[str]:
    """根据当前对话内容选择相关记忆的文件名列表。

    用 LLM 做语义相关性判断（比关键词匹配更准确）。
    先尝试 LLM 选择，失败时 fallback 到关键词匹配。

    流程：
      1. 收集最近 3 条 user 消息作为上下文
      2. 构建记忆目录（name + description）
      3. LLM 判断哪些记忆相关，返回索引数组如 [0, 3]
      4. 解析索引 → 返回 filename 列表

    参数 messages：消息历史。
    参数 max_items：最多返回多少个记忆文件。
    返回：[filename, ...]（如 ["use-4-space-indent.md", "project-facts.md"]）。
    """
    files = list_memory_files()
    if not files:
        return []

    # 收集最近 3 条 user 消息作为上下文（最多 2000 字符）
    # 为什么要倒序遍历？因为 messages[-1] 是最新消息，越近的越相关。
    # 用 reversed 从后往前找，找到够 3 条 user 消息就停。
    recent_texts = []
    for msg in reversed(messages):
        if msg.get("role") == "user":
            content = msg.get("content", "")
            # content 可能是 str（纯文本消息）也可能是 list（含 tool_result 的消息）。
            # 从 list 中只提取 type=="text" 的文本块，忽略 tool_result/thinking 等。
            if isinstance(content, list):
                content = " ".join(
                    str(getattr(b, "text", "")) for b in content
                    if getattr(b, "type", None) == "text"
                )
            if isinstance(content, str):
                recent_texts.append(content)
            if len(recent_texts) >= 3:
                break
    # 因为是从后往前收集的，recent_texts 的顺序是 [最新, 次新, 第三新]。
    # reversed() 把顺序翻回正常时间序 [最老...最新]，方便 LLM 理解上下文。
    recent = " ".join(reversed(recent_texts))[:2000]

    if not recent.strip():
        return []

    # 构建记忆目录供 LLM 选择
    # 每条一行："0: use-tab — User prefers tabs" ，
    # 不传正文内容给 LLM，只传 name+description 做轻量判断。
    # 这样可以降低 side-query 的 token 消耗。
    catalog_lines = []
    for i, f in enumerate(files):
        catalog_lines.append(f"{i}: {f['name']} — {f['description']}")
    catalog = "\n".join(catalog_lines)

    prompt = (
        "Given the recent conversation and the memory catalog below, "
        "select the indices of memories that are clearly relevant. "
        "Return ONLY a JSON array of integers, e.g. [0, 3]. "
        "If none are relevant, return [].\n\n"
        f"Recent conversation:\n{recent}\n\n"
        f"Memory catalog:\n{catalog}"
    )

    try:
        response = client.messages.create(
            model=MODEL,
            messages=[{"role": "user", "content": prompt}],
            max_tokens=200,
        )
        text = extract_text(response.content).strip()
        # LLM 可能返回 JSON 外还带解释文本（如 "Based on the conversation, [0, 3] are relevant."），
        # 所以用正则提取第一个 [...] 部分，而不是直接 json.loads 整个文本。
        match = re.search(r'\[.*?\]', text, re.DOTALL)
        if match:
            indices = json.loads(match.group())
            selected = []
            for idx in indices:
                if isinstance(idx, int) and 0 <= idx < len(files):
                    selected.append(files[idx]["filename"])
                    if len(selected) >= max_items:
                        break
            return selected
    except Exception:
        pass  # LLM 选择失败（API 错误/JSON 解析失败）→ Fallback 到关键词匹配

    # Fallback：关键词匹配（简单但可靠）
    # 取 recent 中长度 > 3 的词，在 name+description 中搜索。
    # 长度 > 3 的过滤去掉 "the"、"and"、"for" 等无意义短词。
    keywords = [w.lower() for w in recent.split() if len(w) > 3]
    selected = []
    for f in files:
        text = (f["name"] + " " + f["description"]).lower()
        if any(kw in text for kw in keywords):
            selected.append(f["filename"])
            if len(selected) >= max_items:
                break
    return selected


def load_memories(messages: list) -> str:
    """加载相关记忆内容字符串，准备注入上下文。

    调用 select_relevant_memories() 选择记忆文件，
    读取每个文件的完整内容，包裹在 <relevant_memories> XML 标签中。

    参数 messages：消息历史（用于判断相关性）。
    返回：格式化的记忆内容字符串；无相关记忆时返回 ""。

    注意：这个函数只返回字符串，真正的"注入"发生在 agent_loop 中——
    将返回的字符串拼接到当前 user 消息的 content 前面。
    这里不直接修改 messages，保持职责单一。
    """
    selected_files = select_relevant_memories(messages)
    if not selected_files:
        return ""

    parts = ["<relevant_memories>"]
    for filename in selected_files:
        content = read_memory_file(filename)
        if content:
            parts.append(content)
    parts.append("</relevant_memories>")
    return "\n\n".join(parts)


def extract_memories(messages: list):
    """从最近对话中提取新记忆（每轮结束调用一次）。

    用 LLM 分析最近 10 条消息，提取用户偏好、反馈、项目事实等。
    LLM 返回 JSON 数组，每条包含 name/type/description/body。
    写入前检查已有记忆避免重复。

    参数 messages：原始消息列表（压缩前的快照）。

    为什么用 messages[-10:]？只取最相关的尾部对话，避免把整个
    对话历史传给 LLM，节省 token。10 条足够 LLM 判断"用户刚刚
    表达了什么偏好"。

    为什么传 pre_compress 而不是压缩后的 messages？
    压缩后 tool_result 被替换成占位符、中间消息被 snipped，
    LLM 看不到完整信息，无法准确判断"这个值不值得记"。
    """
    # 收集最近 10 条消息的文本
    dialogue_parts = []
    for msg in messages[-10:]:
        role = msg.get("role", "?")
        content = msg.get("content", "")
        # content 可能是 list（含 tool_result/thinking 等块），
        # 提取其中的 text 块，忽略 tool_result 等非文本内容。
        # tool_result 是工具执行输出，通常不会包含用户偏好信息。
        if isinstance(content, list):
            content = " ".join(
                str(getattr(b, "text", "")) for b in content
                if getattr(b, "type", None) == "text"
            )
        if isinstance(content, str) and content.strip():
            # 格式成 "user: xxx" / "assistant: xxx"，
            # LLM 能通过角色区分"是用户说的还是 AI 说的"。
            dialogue_parts.append(f"{role}: {content}")
    dialogue = "\n".join(dialogue_parts)

    if not dialogue.strip():
        return

    # 列出现有记忆，帮助 LLM 判断"这个是不是新信息"
    # 如果不给现有记忆列表，LLM 可能重复提取已经存过的记忆。
    existing = list_memory_files()
    existing_desc = "\n".join(f"- {m['name']}: {m['description']}" for m in existing) if existing else "(none)"

    prompt = (
        "Extract user preferences, constraints, or project facts from this dialogue.\n"
        "Return a JSON array. Each item: {name, type, description, body}.\n"
        "- name: short kebab-case identifier (e.g. 'user-preference-tabs')\n"
        "- type: one of 'user' (user preference), 'feedback' (guidance), "
        "'project' (project fact), 'reference' (external pointer)\n"
        "- description: one-line summary for index lookup\n"
        "- body: full detail in markdown\n"
        "If nothing new or already covered by existing memories, return [].\n\n"
        f"Existing memories:\n{existing_desc}\n\n"
        f"Dialogue:\n{dialogue[:4000]}"
    )

    try:
        response = client.messages.create(
            model=MODEL, messages=[{"role": "user", "content": prompt}], max_tokens=800
        )
        text = extract_text(response.content).strip()
        # Extract JSON array from response
        # 用 re.DOTALL 让 . 可以匹配换行符，跨行 JSON 也能提取
        match = re.search(r'\[.*\]', text, re.DOTALL)
        if not match:
            return
        items = json.loads(match.group())
        if not items:
            return
        count = 0
        for mem in items:
            name = mem.get("name", f"memory_{int(time.time())}")
            mem_type = mem.get("type", "user")
            desc = mem.get("description", "")
            body = mem.get("body", "")
            # 只有 description 和 body 都非空才写入，
            # 避免 LLM 返回了残缺的记忆条目。
            if desc and body:
                write_memory_file(name, mem_type, desc, body)
                count += 1
        if count:
            print(f"\n\033[33m[Memory: extracted {count} new memories]\033[0m")
    except Exception:
        pass  # 提取失败不阻塞 Agent 正常工作


# ── 记忆合并（Dream） ───────────────────────────────────────
#
# 当记忆文件数量超过阈值时，调用 LLM 合并去重。
# 类比人类的"做梦"——睡眠时整理白天的记忆。
# 这是垃圾回收机制：旧记忆不清理会越积越多，
# 影响 select_relevant_memories 的准确性和速度。

CONSOLIDATE_THRESHOLD = 10  # 触发合并的最小记忆文件数

def consolidate_memories():
    """合并重复/过时记忆。当文件数 >= 10 时触发。

    调用 LLM 执行：
      1. 合并重复记忆
      2. 删除过时/被新信息覆盖的记忆
      3. 将总数控制在 30 条以内
      4. 优先保留用户偏好

    实现方式：先清空所有旧记忆文件，再写回 LLM 整理后的列表。
    这种"先删后写"的策略比逐文件比较更简单，但要注意：
    如果 LLM 调用失败，所有旧记忆也会被清空。（当前实现中
    清空发生在 LLM 调用成功后，所以安全。）

    类比：把这个过程想象成"做梦"——睡眠时大脑整理白天的记忆，
    合并重复的、丢弃不重要的、巩固重要的。
    """
    files = list_memory_files()
    if len(files) < CONSOLIDATE_THRESHOLD:
        # 记忆太少不值得整理。阈值 10 是个经验值：
        # 太少的话 LLM 没什么可合并的，白白消耗 token。
        # 频繁整理也会让记忆文件频繁变动，影响索引稳定性。
        return

    # 把所有记忆拼成一份 Markdown 传给 LLM
    catalog = "\n\n".join(
        f"## {f['filename']}\nname: {f['name']}\ndescription: {f['description']}\n{f['body']}"
        for f in files
    )

    prompt = (
        "Consolidate the following memory files. Rules:\n"
        "1. Merge duplicates into one\n"
        "2. Remove outdated/contradicted memories\n"
        "3. Keep the total under 30 memories\n"
        "4. Preserve important user preferences above all\n"
        "Return a JSON array. Each item: {name, type, description, body}.\n\n"
        f"{catalog[:16000]}"
    )

    try:
        response = client.messages.create(
            model=MODEL, messages=[{"role": "user", "content": prompt}], max_tokens=3000
        )
        text = extract_text(response.content).strip()
        match = re.search(r'\[.*\]', text, re.DOTALL)
        if not match:
            return
        items = json.loads(match.group())

        # 清空所有旧记忆文件（保留 MEMORY.md，它会被 _rebuild_index 重建）
        # 注意：清空发生在 LLM 成功返回之后，不是之前。
        for f in MEMORY_DIR.glob("*.md"):
            if f.name != "MEMORY.md":
                # 删除旧记忆文件
                f.unlink()

        # 写入 LLM 整理后的记忆
        for mem in items:
            name = mem.get("name", f"memory_{int(time.time())}")
            mem_type = mem.get("type", "user")
            desc = mem.get("description", "")
            body = mem.get("body", "")
            if desc and body:
                write_memory_file(name, mem_type, desc, body)

        print(f"\n\033[33m[Memory: consolidated {len(files)} → {len(items)} memories]\033[0m")
    except Exception:
        pass


# ═══════════════════════════════════════════════════════════
#  SYSTEM 提示词构建
#
#  s09 的 SYSTEM = 基础角色描述 + 记忆索引。
#  build_system() 每轮调用一次（因为记忆可能在过程中更新）。
# ═══════════════════════════════════════════════════════════

def build_system() -> str:
    """构建包含记忆索引的 SYSTEM 提示词。

    调用 read_memory_index() 获取 MEMORY.md 内容，
    注入到 SYSTEM 中作为 "Memories available" 段落。
    同时提示 LLM 尊重记忆中的偏好，并在用户说 "remember" 时提取。

    SYSTEM 提示词和 user 消息的区别：
    - SYSTEM：每次 LLM 调用都可见，适合放"始终需要知道"的信息（记忆索引）
    - user 消息：按需注入完整记忆内容（详见 agent_loop 中的注入逻辑）
    这样索引常驻（可被 prompt cache 缓存），内容按需加载（不浪费 token）。
    """
    index = read_memory_index()
    memories_section = f"\n\nMemories available:\n{index}" if index else ""
    return (
        f"You are a coding agent at {WORKDIR}."
        f"{memories_section}\n"
        "Relevant memories are injected below. Respect user preferences from memory.\n"
        "When the user says 'remember' or expresses a clear preference, extract it as a memory."
    )

# ---- 子 Agent 系统提示词（不含记忆功能） ----
SUB_SYSTEM = (
    f"You are a coding agent at {WORKDIR}. "
    "Complete the task you were given, then return a concise summary. "
    "Do not delegate further."
)


# ═══════════════════════════════════════════════════════════
#  工具实现（5 个标准工具）
#
#  bash/read/write/edit/glob。s09 简化了工具集。
# ═══════════════════════════════════════════════════════════

def safe_path(p: str) -> Path:
    """将用户输入的相对路径解析为绝对路径，确保不逃逸工作目录。"""
    path = (WORKDIR / p).resolve()
    if not path.is_relative_to(WORKDIR): raise ValueError(f"Path escapes workspace: {p}")
    return path

def run_bash(command: str) -> str:
    """执行 Shell 命令并返回 stdout/stderr。最长 50000 字符，超时 120 秒。"""
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
    """写入文件（覆盖写入，自动创建父目录）。"""
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

def extract_text(content) -> str:
    """从 message content 中提取纯文本。兼容 list 和 str 两种格式。"""
    if not isinstance(content, list): return str(content)
    return "\n".join(getattr(b, "text", "") for b in content if getattr(b, "type", None) == "text")

# Subagent (simplified from s06-s07)
SUB_TOOLS = [
    {"name": "bash", "description": "Run a shell command.",
     "input_schema": {"type": "object", "properties": {"command": {"type": "string"}}, "required": ["command"]}},
    {"name": "read_file", "description": "Read file contents.",
     "input_schema": {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]}},
    {"name": "write_file", "description": "Write content to a file.",
     "input_schema": {"type": "object", "properties": {"path": {"type": "string"}, "content": {"type": "string"}}, "required": ["path", "content"]}},
]
SUB_HANDLERS = {"bash": run_bash, "read_file": run_read, "write_file": run_write}

def spawn_subagent(description: str) -> str:
    """创建子 Agent，用全新上下文执行子任务。

    子 Agent 不具备记忆功能——记忆属于父 Agent。
    子 Agent 用独立的 SUB_SYSTEM（不包含记忆索引）和独立的
    SUB_TOOLS（只有 bash/read_file/write_file 三个工具，
    没有 edit/glob/task，防止子 Agent 再创建子 Agent）。

    30 轮上限（for _ in range(30)）：防止子 Agent 陷入无限循环。
    LLM 每轮要么调工具要么结束，30 轮对于大多数子任务足够了。

    返回值提取策略：
    1. 先看最后一条 user 消息的文本（子 Agent 结束时留下的摘要）
    2. 如果为空，从后往前找第一条 assistant 消息的文本
    3. 都找不到则返回兜底信息
    """
    print(f"\n\033[35m[Subagent spawned]\033[0m")
    messages = [{"role": "user", "content": description}]
    for _ in range(30):
        response = client.messages.create(model=MODEL, system=SUB_SYSTEM,
            messages=messages, tools=SUB_TOOLS, max_tokens=8000)
        messages.append({"role": "assistant", "content": response.content})
        if response.stop_reason != "tool_use": break
        results = []
        for block in response.content:
            if block.type == "tool_use":
                handler = SUB_HANDLERS.get(block.name)
                output = handler(**block.input) if handler else f"Unknown: {block.name}"
                print(f"  \033[90m[sub] {block.name}: {str(output)[:100]}\033[0m")
                results.append({"type": "tool_result", "tool_use_id": block.id, "content": output})
        messages.append({"role": "user", "content": results})
    # 子 Agent 结束后，提取最终结果文本
    result = extract_text(messages[-1]["content"])
    if not result:
        # 如果最后一条不是文本（比如退出的最后一步是 tool_result），
        # 从后往前找第一条 assistant 消息的文本内容。
        for msg in reversed(messages):
            if msg["role"] == "assistant":
                result = extract_text(msg["content"])
                if result: break
        if not result: result = "Subagent stopped after 30 turns without final answer."
    print(f"\033[35m[Subagent done]\033[0m")
    return result


# ═══════════════════════════════════════════════════════════
#  上下文压缩管道（s08 引入）
#
#  四层压缩：budget → snip → micro → auto → reactive
#  与 s08 完全一致。
# ═══════════════════════════════════════════════════════════

CONTEXT_LIMIT = 50000       # 触发 L4 摘要的字符数阈值
KEEP_RECENT = 3             # micro_compact 保留最近 N 个 tool_result
PERSIST_THRESHOLD = 30000   # 单个输出超此值则持久化到磁盘

def estimate_size(msgs):
    """估算 messages 列表的字符串长度。"""
    return len(str(msgs))

def _block_type(block):
    """获取 block 的 type 属性（兼容 dict 和 object 两种格式）。

    Anthropic SDK 返回的 response.content 是对象列表（有 .type 属性），
    但 agent_loop 中构造的 tool_result 是 dict（有 ["type"] 键）。
    这个函数兼容两者，避免类型判断散落在各处。
    """
    return block.get("type") if isinstance(block, dict) else getattr(block, "type", None)

def _message_has_tool_use(msg):
    """判断 assistant 消息是否包含 tool_use block。用于边界保护。

    在裁剪消息时，如果一条 assistant 消息包含 tool_use，
    它后面通常跟着一条 user 消息包含对应的 tool_result。
    裁剪时要把这对"请求-响应"当作一个整体处理，
    不能拆开（否则 LLM 看到 tool_result 没有对应的 tool_use 会困惑）。
    """
    if msg.get("role") != "assistant":
        return False
    content = msg.get("content")
    if not isinstance(content, list):
        return False
    return any(_block_type(block) == "tool_use" for block in content)

def _is_tool_result_message(msg):
    """判断 user 消息是否包含 tool_result block。用于边界保护。

    和 _message_has_tool_use 配对使用，确保 tool_use/tool_result
    这对消息在裁剪时不会被拆散。
    """
    if msg.get("role") != "user":
        return False
    content = msg.get("content")
    if not isinstance(content, list):
        return False
    return any(isinstance(block, dict) and block.get("type") == "tool_result" for block in content)

def snip_compact(msgs, mx=50):
    """L1：裁剪中间消息，保留头尾 + 配对保护。

    策略：保留前 3 条（通常是初始请求+早期工具调用）和
    后 (mx-3) 条最新消息，中间的替换为一条 snipped 占位符。

    配对保护（head_end/tail_start 调整）：
    如果裁剪边界正好切在 tool_use 和 tool_result 之间，
    会调整边界把整对消息保留下来。否则 LLM 看到孤立的
    tool_result 会不知道对应哪个调用。
    """
    if len(msgs) <= mx: return msgs
    head_end, tail_start = 3, len(msgs) - (mx - 3)
    # 头部保护：如果 head_end 前一条是含 tool_use 的 assistant 消息，
    # 把后面的 tool_result 也保留，避免 tool_use/tool_result 被拆散。
    if head_end > 0 and _message_has_tool_use(msgs[head_end - 1]):
        while head_end < len(msgs) and _is_tool_result_message(msgs[head_end]):
            head_end += 1
    # 尾部保护：如果 tail_start 正好是一条 tool_result 消息，
    # 且它前面是含 tool_use 的 assistant 消息，把 tail_start 前移一位，
    # 保留这对 tool_use/tool_result。
    if (tail_start > 0 and tail_start < len(msgs)
            and _is_tool_result_message(msgs[tail_start])
            and _message_has_tool_use(msgs[tail_start - 1])):
        tail_start -= 1
    if head_end >= tail_start:
        return msgs
    return msgs[:head_end] + [{"role": "user", "content": f"[snipped {tail_start - head_end} msgs]"}] + msgs[tail_start:]

def collect_tool_results(msgs):
    """收集所有 tool_result block 的位置。返回 [(msg_idx, block_idx, block), ...]."""
    blocks = []
    for mi, msg in enumerate(msgs):
        if msg.get("role") != "user" or not isinstance(msg.get("content"), list): continue
        for bi, block in enumerate(msg["content"]):
            if isinstance(block, dict) and block.get("type") == "tool_result": blocks.append((mi, bi, block))
    return blocks

def micro_compact(msgs):
    """L2：旧 tool_result 替换为占位符（保留最近 KEEP_RECENT 个）。

    策略：tool_result 的内容通常很长（文件内容、命令输出），
    但只有最近的几个对"当前正在做什么"有影响。
    早期的 tool_result 用短占位符替换，节省 token。
    注意：只替换长度 > 120 的，短的结果没必要替换。
    """
    tr = collect_tool_results(msgs)
    if len(tr) <= KEEP_RECENT: return msgs
    for _, _, b in tr[:-KEEP_RECENT]:
        if len(b.get("content", "")) > 120: b["content"] = "[Earlier tool result compacted.]"
    return msgs

def persist_large(tid, out):
    """L3：超大输出写入磁盘，返回 <persisted-output> 引用。

    把大输出写到 .task_outputs/tool-results/<tid>.txt，
    在消息中只保留一个引用链接 + 前 2000 字符预览。
    LLM 如果确实需要完整内容，可以用 read_file 工具读这个文件。

    tid：tool_use_id，确保文件名唯一。
    """
    if len(out) <= PERSIST_THRESHOLD: return out
    TOOL_RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    p = TOOL_RESULTS_DIR / f"{tid}.txt"
    if not p.exists(): p.write_text(out)
    return f"<persisted-output>\nFull: {p}\nPreview:\n{out[:2000]}\n</persisted-output>"

def tool_result_budget(msgs, mx=200_000):
    """L3：检查最新一轮 tool_result 总量，超大则持久化。

    不是替换整个消息，而是对超大（>PERSIST_THRESHOLD）的
    单个 tool_result 做持久化。从最大的开始处理，直到
    总大小降到 mx 以下。
    """
    last = msgs[-1] if msgs else None
    if not last or last.get("role") != "user" or not isinstance(last.get("content"), list): return msgs
    blocks = [(i, b) for i, b in enumerate(last["content"]) if isinstance(b, dict) and b.get("type") == "tool_result"]
    total = sum(len(str(b.get("content", ""))) for _, b in blocks)
    if total <= mx: return msgs
    # 按内容长度降序排列，从最大的开始持久化
    for _, block in sorted(blocks, key=lambda p: len(str(p[1].get("content", ""))), reverse=True):
        if total <= mx: break
        c = str(block.get("content", ""))
        if len(c) <= PERSIST_THRESHOLD: continue
        block["content"] = persist_large(block.get("tool_use_id", "?"), c)
        total = sum(len(str(b.get("content", ""))) for _, b in blocks)
    return msgs

def write_transcript(msgs):
    """将完整 messages 保存为 JSONL 文件。L4 和 reactive 的安全网。

    在压缩前保存一份完整转录，防止压缩不可逆丢失信息。
    JSONL 格式（每行一个 JSON 对象），方便后续按行读取。
    """
    TRANSCRIPT_DIR.mkdir(parents=True, exist_ok=True)
    p = TRANSCRIPT_DIR / f"transcript_{int(time.time())}.jsonl"
    with p.open("w") as f:
        for m in msgs: f.write(json.dumps(m, default=str) + "\n")
    return p

def summarize_history(msgs):
    """L4：调用 LLM 生成对话摘要。保留目标、发现、文件变更、剩余工作、约束。"""
    conv = json.dumps(msgs, default=str)[:80000]
    r = client.messages.create(model=MODEL, messages=[{"role": "user", "content":
        "Summarize this coding-agent conversation so work can continue.\n"
        "Preserve: 1. current goal, 2. key findings, 3. files changed, 4. remaining work, 5. user constraints.\n\n" + conv}],
        max_tokens=2000)
    return extract_text(r.content).strip()

def compact_history(msgs):
    """L4：保存转录 → LLM 摘要 → 返回紧凑 messages。

    用 LLM 生成的摘要替换整个对话历史。
    返回只有一条消息的列表，内容是 "[Compacted]\n\n<摘要>"。
    这是最激进的压缩，只在其他层都不够时触发。
    """
    write_transcript(msgs)
    summary = summarize_history(msgs)
    return [{"role": "user", "content": f"[Compacted]\n\n{summary}"}]

def reactive_compact(msgs):
    """应急压缩：API 报 prompt_too_long 时，保留尾部 5 条 + 摘要前半部分。

    和 compact_history 的区别：
    - compact_history 压缩整个对话（API 调用前主动压缩）
    - reactive_compact 只在 API 抛出 prompt_too_long 后被动触发
    - reactive_compact 保留尾部 5 条原始消息，不是全部摘要
    """
    write_transcript(msgs)
    tail_start = max(0, len(msgs) - 5)
    # 尾部保护：同样要确保 tool_use/tool_result 不被拆散
    if (tail_start > 0 and tail_start < len(msgs)
            and _is_tool_result_message(msgs[tail_start])
            and _message_has_tool_use(msgs[tail_start - 1])):
        tail_start -= 1
    summary = summarize_history(msgs[:tail_start])
    return [{"role": "user", "content": f"[Reactive compact]\n\n{summary}"}, *msgs[tail_start:]]


# ═══════════════════════════════════════════════════════════
#  工具定义（TOOLS）—— 发送给 LLM 的 JSON Schema
#
#  s09 简化工具集：聚焦记忆系统，去掉了 todo_write/load_skill/compact。
#  共 6 个工具：bash/read/write/edit/glob/task。
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
     "input_schema": {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]}},
    {"name": "write_file", "description": "Write content to a file.",
     "input_schema": {"type": "object", "properties": {"path": {"type": "string"}, "content": {"type": "string"}}, "required": ["path", "content"]}},
    {"name": "edit_file", "description": "Replace exact text in a file once.",
     "input_schema": {"type": "object", "properties": {"path": {"type": "string"}, "old_text": {"type": "string"}, "new_text": {"type": "string"}}, "required": ["path", "old_text", "new_text"]}},
    {"name": "glob", "description": "Find files matching a glob pattern.",
     "input_schema": {"type": "object", "properties": {"pattern": {"type": "string"}}, "required": ["pattern"]}},
    {"name": "task", "description": "Launch a subagent to handle a subtask.",
     "input_schema": {"type": "object", "properties": {"description": {"type": "string"}}, "required": ["description"]}},
]

# ═══════════════════════════════════════════════════════════
#  工具分发映射（TOOL_HANDLERS）—— 工具名 → 执行函数
# ═══════════════════════════════════════════════════════════
TOOL_HANDLERS = {
    "bash": run_bash, "read_file": run_read, "write_file": run_write,
    "edit_file": run_edit, "glob": run_glob, "task": spawn_subagent,
}


# ═══════════════════════════════════════════════════════════
#  agent_loop — s09 核心：记忆注入 + 压缩 + 提取
#
#  与 s08 的两个关键不同：
#    1. LLM 调用前注入相关记忆到 user 消息
#    2. 每轮结束后 extract_memories + 定期 consolidate_memories
#
#  另一个重要细节：pre_compress 快照。
#  压缩会修改 messages（snip/micro/compact 都会改），
#  但记忆提取需要原始完整内容。所以在压缩前复制一份快照。
# ═══════════════════════════════════════════════════════════

MAX_REACTIVE_RETRIES = 1

def agent_loop(messages: list):
    """Agent 核心循环：带记忆注入和提取的 LLM 交互。

    流程：
      1. load_memories 选择相关记忆
      2. build_system 构建含记忆索引的 SYSTEM
      3. 保存 pre_compress 快照
      4. 压缩管道（budget→snip→micro→[auto]）
      5. 记忆注入到当前 user 消息
      6. try LLM；prompt_too_long → reactive + retry
      7. stop_reason != "tool_use"？
         → extract_memories + consolidate_memories → 返回
      8. 工具分发 → 结果回写 → 回到步骤 4

    参数 messages：消息历史列表（对话上下文）。

    ────── 关于 while True 的迭代边界 ──────
    每一次 while 迭代 = 一轮 LLM 调用。
    每轮可能是 LLM 调用工具（tool_use）、或者 LLM 结束（end_turn）。
    工具调用后，把结果追加到 messages，继续下一轮 while。
    所以一轮用户输入可能对应多次 while 迭代（每次 LLM 调一个工具）。

    ────── 关于记忆的持久性 ──────
    记忆提取（extract_memories）发生在整个 agent_loop 结束时
    （所有工具调用完成，LLM 给出最终回答后）。
    这意味着：如果用户在对话中间说了"记住这个偏好"，
    要等到这一轮对话全部结束才会写入记忆文件。
    """
    reactive_retries = 0

    # ─── 步骤 1-2：记忆选择 + SYSTEM 构建 ───
    # 每次进入 agent_loop 调用一次（每个 user 轮次）
    memories_content = load_memories(messages)
    # 记录当前 user 消息的索引（用于步骤 5 的记忆注入定位）
    # 判断逻辑：最新一条必须是纯文本 user 消息（content 是 str），
    # 不能是 tool_result 列表——记忆只能注入到文本消息前面。
    memory_turn = len(messages) - 1 if messages and isinstance(messages[-1].get("content"), str) else None
    system = build_system()  # 含最新记忆索引的 SYSTEM

    while True:
        # ─── 步骤 3：保存压缩前快照 ───
        # extract_memories 需要完整对话内容来判断是否值得提为新记忆。
        # 压缩后的 messages 会丢失细节（snip/micro 的占位符）。
        # 所以先复制一份。
        pre_compress = [m if isinstance(m, dict) else {"role": m.get("role",""),
            "content": str(m.get("content",""))} for m in messages]

        # ─── 步骤 4：压缩管道（同 s08） ───
        messages[:] = tool_result_budget(messages)    # L3：超大 tool_result 持久化
        messages[:] = snip_compact(messages)          # L1：裁剪中间消息
        messages[:] = micro_compact(messages)         # L2：旧 tool_result 占位符

        if estimate_size(messages) > CONTEXT_LIMIT:
            print("[auto compact]")
            messages[:] = compact_history(messages)   # L4：LLM 摘要

        # ─── 步骤 5-6：记忆注入 + LLM 调用 ───
        try:
            # 将相关记忆内容拼接到当前 user 消息前面。
            # 注意：记忆内容不能直接改到 messages 里，否则下轮
            # while 迭代再拼接时会把上一轮的也带上，越叠越长。
            #
            # 所以策略是：只有当有记忆内容要注入时，才做浅拷贝
            # （request_messages = messages.copy()），在拷贝上修改，
            # 传给 LLM。原始 messages 保持干净。
            request_messages = messages  # 默认：无记忆注入，直接复用
            if memories_content and memory_turn is not None and memory_turn < len(messages):
                request_messages = messages.copy()
                # 构造新字典：用 ** 展开原消息的所有字段（role 等），
                # 再单独写 content 覆盖掉解包进来的旧 content。
                # 等效于：先复制再 request_messages[turn]["content"] = ...
                # 但解包写法在一行表达"只改 content"更紧凑，且不修改原字典。
                request_messages[memory_turn] = {
                    **messages[memory_turn],
                    "content": memories_content + "\n\n" + messages[memory_turn]["content"],
                }
            response = client.messages.create(
                model=MODEL, system=system, messages=request_messages, tools=TOOLS, max_tokens=8000
            )
            reactive_retries = 0
        except Exception as e:
            if ("prompt_too_long" in str(e).lower() or "too many tokens" in str(e).lower()) and reactive_retries < MAX_REACTIVE_RETRIES:
                print("[reactive compact]")
                messages[:] = reactive_compact(messages)
                reactive_retries += 1
                continue  # 用压缩后的 messages 重试 LLM 调用
            raise  # 非 prompt_too_long 错误，直接抛出

        # ─── 步骤 7：LLM 完成 → 提取记忆 → 返回 ───
        messages.append({"role": "assistant", "content": response.content})
        if response.stop_reason != "tool_use":
            # LLM 不再调用工具了，本轮对话结束。
            # 使用 pre_compress 快照（完整对话，未压缩）提取记忆。
            # 为什么用 pre_compress 而不是当前的 messages？
            # 当前的 messages 已经被压缩过了（snipped/micro/compact），
            # 细节丢失了。pre_compress 保存了调用 LLM 之前的完整对话。
            extract_memories(pre_compress)
            # 定期检查是否需要合并去重（文件数 >= 10 时触发）
            consolidate_memories()
            return

        # ─── 步骤 8：工具分发执行 ───
        # LLM 调用了工具 → 遍历每个 tool_use block，执行对应的函数。
        results = []
        for block in response.content:
            if block.type != "tool_use": continue
            print(f"\033[36m> {block.name}\033[0m")
            handler = TOOL_HANDLERS.get(block.name)
            output = handler(**block.input) if handler else f"Unknown: {block.name}"
            print(str(output)[:200])
            results.append({"type": "tool_result", "tool_use_id": block.id, "content": output})
        # 把工具执行结果追加到 messages，下一轮 while 迭代 LLM 会看到。
        messages.append({"role": "user", "content": results})
        # 回到 while 顶部 → 步骤 3：再次压缩、记忆注入、调 LLM……


# ═══════════════════════════════════════════════════════════
#  主入口
# ═══════════════════════════════════════════════════════════
if __name__ == "__main__":
    print("s09: Memory — persistent cross-session knowledge")
    print("输入问题，回车发送。输入 q 退出。\n")
    history = []
    while True:
        try: query = input("\033[36ms09 >> \033[0m")
        except (EOFError, KeyboardInterrupt): break
        if query.strip().lower() in ("q", "exit", ""): break
        history.append({"role": "user", "content": query})
        agent_loop(history)
        # agent_loop 返回后，history[-1] 是最后一条 assistant 消息。
        # assistant 消息的 content 可能包含多种 block（text, tool_use 等），
        # 但 agent_loop 结束时只会剩 text block（因为 stop_reason != "tool_use"）。
        # 只打印 text 块，忽略其他类型。
        for block in history[-1]["content"]:
            if getattr(block, "type", None) == "text": print(block.text)
        print()
