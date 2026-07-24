# Agent 多层 while 循环设计（s01-s06）

## 概述

s01-s06 中每个章节都包含至少两层循环结构。s01-s05 为两层，s06 引入子 Agent 后变为三层。所有层级之间是严格的**嵌套调用**关系，而非并发。

---

## 一、共用结构（全部 6 章一致）

### 外层 while：会话循环

```python
# 位置：__main__
while True:
    query = input(">> ")          # 阻塞等待用户输入
    if query == "q": break        # 用户主动退出
    history.append(query)         # 追加用户消息
    agent_loop(history)           # 进入内层（阻塞，直到 LLM 完成）
    打印 LLM 最终文本              # agent_loop 返回后的后处理
    # 回到等待下一个用户输入
```

**职责**：管理多轮对话的生命周期。用户说一句 → Agent 处理一整轮（可能包含多次工具调用）→ 用户说下一句。

**退出条件**：用户输入 `q` / `exit` / 空行。

**谁驱动**：人。

### 内层 while：工具调用循环

```python
# 位置：agent_loop()
while True:
    response = client.messages.create(    # 调用 LLM
        model=MODEL, system=SYSTEM,
        messages=messages, tools=TOOLS,
        max_tokens=8000)
    messages.append({"role": "assistant", "content": response.content})

    if response.stop_reason != "tool_use":
        return                             # LLM 认为任务完成 → 退出

    results = []
    for block in response.content:
        if block.type == "tool_use":
            # 工具分发执行（各章节不同，见第二节）
            output = 执行工具(block)
            results.append({"type": "tool_result",
                            "tool_use_id": block.id,
                            "content": output})

    messages.append({"role": "user", "content": results})
    # 回到 while，LLM 看到工具结果后决定下一步
```

**职责**：管理单轮对话内的工具调用链。一次用户请求可能触发 LLM 多轮"调用工具 → 看结果 → 再调用"的循环。

**退出条件**：`stop_reason != "tool_use"`（LLM 输出纯文本，认为任务完成）。

**谁驱动**：LLM。

---

## 二、内层 while 在各章节的演变

两个 while 的骨架从 s01 到 s05 **完全不变**。变化的只是内层 while 中"执行工具"这一步的具体逻辑：

### s01：硬编码单工具

```python
# 只有一个工具，直接硬编码调用
output = run_bash(block.input["command"])
```

### s02：策略模式查表分发

```python
# 5 个工具，用映射表做策略模式分发
handler = TOOL_HANDLERS.get(block.name)
output = handler(**block.input) if handler else f"Unknown: {block.name}"
```

新增工具只需：写函数 + 注册到 `TOOL_HANDLERS`，循环代码零改动。

### s03：分发前插入三道门权限检查

```python
# 在执行前先过权限管道
if not check_permission(block):          # ← 新增这一行
    results.append({... "Permission denied."})
    continue                              # 拒绝 → 跳过执行
handler = TOOL_HANDLERS.get(block.name)
output = handler(**block.input)
```

权限检查的三道门：
1. **Gate 1（DENY_LIST）**：黑名单匹配 → 无提示直接拒绝
2. **Gate 2（PERMISSION_RULES）**：规则匹配 → 进入 Gate 3
3. **Gate 3（ask_user）**：交互式确认 → y/yes 放行，其他拒绝

### s04：用 Hook 替代硬编码的权限检查

```python
# check_permission() 被 Hook 系统取代
blocked = trigger_hooks("PreToolUse", block)  # ← 改为 Hook 驱动
if blocked:
    results.append({... str(blocked)})
    continue
handler = TOOL_HANDLERS.get(block.name)
output = handler(**block.input)
trigger_hooks("PostToolUse", block, output)   # ← 执行后也触发 Hook
```

四种 Hook 事件：

| 事件 | 触发时机 | 用途 |
|------|---------|------|
| `UserPromptSubmit` | 用户输入后、LLM 调用前 | 上下文注入 |
| `PreToolUse` | 工具执行前 | 权限检查、日志记录 |
| `PostToolUse` | 工具执行后 | 输出检查、后处理 |
| `Stop` | agent_loop 退出前 | 统计汇总 |

### s05：增加 nag 提醒机制

```python
# 每轮开头检查是否需要提醒 LLM 更新计划
if rounds_since_todo >= 3 and messages:
    messages.append({"role": "user",
                     "content": "<reminder>Update your todos.</reminder>"})
    rounds_since_todo = 0

# ... LLM 调用 + 工具分发 ...

rounds_since_todo += 1

# LLM 调用 todo_write → 重置计数器
if block.name == "todo_write":
    rounds_since_todo = 0
```

`rounds_since_todo` 是模块级全局变量，跨 `agent_loop()` 调用保持状态。

---

## 三、s06：第三层出现

s06 在内层 while 中嵌套了第三层——子 Agent 的独立循环。

### 结构

```python
def spawn_subagent(description):
    messages = [{"role": "user", "content": description}]  # ① 全新上下文

    for _ in range(30):                    # ② 第三层：for 循环（30 轮硬上限）
        response = client.messages.create(
            model=MODEL, system=SUB_SYSTEM,
            messages=messages, tools=SUB_TOOLS,
            max_tokens=8000)
        messages.append({"role": "assistant", "content": response.content})

        if response.stop_reason != "tool_use":
            break                          # LLM 认为子任务完成

        for block in response.content:
            if block.type == "tool_use":
                blocked = trigger_hooks("PreToolUse", block)
                if blocked:
                    results.append(...)
                    continue
                handler = SUB_HANDLERS.get(block.name)
                output = handler(**block.input)
                trigger_hooks("PostToolUse", block, output)
                results.append({"type": "tool_result", ...})

        messages.append({"role": "user", "content": results})

    return extract_text(messages[-1])      # ③ 只返回摘要
```

### 为什么用 `for` 而非 `while`

子 Agent 有**30 轮安全上限**——用 `for _ in range(30)` 比 `while + 手动计数器` 更简洁且不会遗漏。但结构上与 agent_loop 的 while 完全同构：LLM → 判断停止 → 工具分发 → 结果回写 → 循环。

### 关键设计决策

| 决策 | 实现 | 原因 |
|------|------|------|
| 上下文隔离 | `messages = [{"role": "user", "content": description}]` | 子 Agent 看不到父对话历史 |
| 仅返回摘要 | `return extract_text(messages[-1])` | 子 Agent 的中间工具结果全部丢弃 |
| 防止递归 | `SUB_TOOLS` 不含 `task` 工具 | 子 Agent 无法创建孙 Agent |
| 安全上限 | `for _ in range(30)` | 防止无限循环消耗 API 费用 |
| 权限共享 | 子 Agent 同样经过 `trigger_hooks("PreToolUse", ...)` | 安全策略统一 |

---

## 四、三层嵌套调用的完整流程

```
外层 while（__main__）：等用户输入
  │
  ├─ 用户输入"帮我创建一个 Python 项目" → agent_loop(history)
  │    │
  │    └─ 内层 while（agent_loop）：LLM ↔ 工具
  │         │
  │         ├─ 第 1 轮：LLM 调 bash("mkdir myproject")
  │         │          → 执行 → 结果回写 → 继续内层 while
  │         │
  │         ├─ 第 2 轮：LLM 调 write_file("myproject/main.py", ...)
  │         │          → 执行 → 结果回写 → 继续内层 while
  │         │
  │         ├─ 第 3 轮：LLM 调 task("帮我写 setup.py 并运行测试")
  │         │    │
  │         │    └─ spawn_subagent("帮我写 setup.py 并运行测试")
  │         │         │
  │         │         └─ 子 Agent for（全新 messages，最多 30 轮）
  │         │              │
  │         │              ├─ 第 1 轮：LLM 调 read_file("myproject/")
  │         │              │          → 执行 → 结果回写
  │         │              ├─ 第 2 轮：LLM 调 write_file("setup.py", ...)
  │         │              │          → 执行 → 结果回写
  │         │              ├─ 第 3 轮：LLM 调 bash("python -m pytest")
  │         │              │          → 执行 → 结果回写
  │         │              └─ 第 4 轮：LLM 输出纯文本 → break
  │         │                   → 返回摘要 "测试全部通过，项目已就绪"
  │         │
  │         │    父 Agent 收到 tool_result: "测试全部通过，项目已就绪"
  │         │    → 继续内层 while
  │         │
  │         └─ 第 4 轮：LLM 输出纯文本 → stop_reason="end_turn" → return
  │
  ├─ 打印 LLM 最终文本 → 回到外层 while
  │
  └─ 用户输入"再帮我加个 Dockerfile" → agent_loop(history) → ...
```

---

## 五、三层对照总结

| | 外层 while | 内层 while | 子 Agent for |
|---|---|---|---|
| **位置** | `__main__` | `agent_loop()` | `spawn_subagent()` |
| **循环写法** | `while True` | `while True` | `for _ in range(30)` |
| **管理边界** | 多轮对话 | 单轮内的工具链 | 一个子任务的工具链 |
| **退出条件** | 用户输入 `q` | `stop_reason != "tool_use"` | LLM 完成 或 30 轮上限 |
| **谁驱动退出** | 人 | LLM | LLM（有硬上限兜底） |
| **messages 来源** | 跨轮累积 | 当前轮新增 | 全新空列表 + 任务描述 |
| **工具集** | — | `TOOLS`（含 task） | `SUB_TOOLS`（不含 task） |
| **出现章节** | s01-s06 | s01-s06 | s06 |

这三层之间的关系是**严格嵌套调用**（不是并发线程）：

- 外层调用 `agent_loop()` 后**阻塞等待**其返回
- 内层调用 `spawn_subagent()` 后**阻塞等待**其返回
- 每一层都对上一层"透明"——外层不知道内层跑了几轮，只关心最终结果
