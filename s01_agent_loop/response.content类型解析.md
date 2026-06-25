
---

# `response.content` 类型解析：Anthropic SDK 中的可辨识联合（Discriminated Union）

> 本文基于对 `anthropic` Python SDK 源码中 `ContentBlock` 类型定义的深入分析整理而成。它不是一个通用教程，而是**对 SDK 内部实际类型结构的记录**，解释了为什么在 s01 中可以用 `block.type == "tool_use"` 来判断消息块类型。

---

## 1. `ContentBlock` 的真实定义

在 `anthropic` Python SDK 中，`ContentBlock` 是这样定义的：

```python
from typing import Union
from typing_extensions import Annotated
from anthropic._utils._transform import PropertyInfo   # ← SDK 内部类，不是项目自定义

ContentBlock = Annotated[
    Union[
        TextBlock,
        ThinkingBlock,
        RedactedThinkingBlock,
        ToolUseBlock,
        ServerToolUseBlock,
        WebSearchToolResultBlock,
        WebFetchToolResultBlock,
        CodeExecutionToolResultBlock,
        BashCodeExecutionToolResultBlock,
        TextEditorCodeExecutionToolResultBlock,
        ToolSearchToolResultBlock,
        ContainerUploadBlock,
        # 共 12 种
    ],
    PropertyInfo(discriminator="type")
]

content: list[ContentBlock]   # Message.content 字段的类型
```

### 1.1 直白解释

**`ContentBlock` 不是一个具体的类，而是一个"带判别的联合类型"**，它表示：

> `response.content` 列表里的每一个元素，都可能是这 12 种具体类型中的**任意一种**。系统根据消息中的 `type` 字段值自动决定使用哪个类来解析。

### 1.2 核心机制

| 组成部分 | 含义 |
| :--- | :--- |
| `Union[...]` | **或**的关系——"可能是其中之一" |
| `Annotated[..., PropertyInfo(discriminator="type")]` | 告诉 SDK：**根据数据中的 `type` 字段值来决定用哪个类** |
| 赋值别名（`ContentBlock = ...`） | 将这 12 种类型的联合简写为一个名字 |

---

## 2. 12 种 Block 类型全览

| 类名 | `type` 字段值 | 用途 | 关键属性 |
|:---|:---|:---|:---|
| `TextBlock` | `"text"` | 文本回复 | `text`, `citations` |
| `ThinkingBlock` | `"thinking"` | 模型思考过程（extended thinking） | `thinking`, `signature` |
| `RedactedThinkingBlock` | `"redacted_thinking"` | 被截断的思考内容 | `data` |
| `ToolUseBlock` | `"tool_use"` | 用户定义的工具调用 | `id`, `name`, `input` |
| `ServerToolUseBlock` | `"server_tool_use"` | 内置服务端工具调用（搜索/抓取/执行代码） | `id`, `name`, `input` |
| `WebSearchToolResultBlock` | `"web_search_tool_result"` | 联网搜索的结果 | `tool_use_id`, `content` |
| `WebFetchToolResultBlock` | `"web_fetch_tool_result"` | 网页抓取的结果 | `tool_use_id`, `content` |
| `CodeExecutionToolResultBlock` | `"code_execution_tool_result"` | 代码执行结果 | `tool_use_id`, `content` |
| `BashCodeExecutionToolResultBlock` | `"bash_code_execution_tool_result"` | Bash 执行结果 | `tool_use_id`, `content` |
| `TextEditorCodeExecutionToolResultBlock` | `"text_editor_code_execution_tool_result"` | 文本编辑器执行结果 | `tool_use_id`, `content` |
| `ToolSearchToolResultBlock` | `"tool_search_tool_result"` | 工具搜索（BM25/正则）结果 | `tool_use_id`, `content` |
| `ContainerUploadBlock` | `"container_upload"` | 容器上传 | `file_id` |

> **注意**：其中 `ToolUseBlock` 是用户代码中定义的普通工具调用，`ServerToolUseBlock` 是 Claude 内置能力（如 `web_search`、`web_fetch`、`code_execution`），两者 `name` 字段的语义有所不同。

---

## 3. 为什么需要这种写法？（三大核心原因）

### 3.1 精准反序列化（解决运行时歧义）

如果不加 `discriminator`，反序列化器只能暴力尝试所有类型，性能差且容易误判。

**加上 `discriminator="type"` 后**：
```json
{"type": "text", "text": "Hello"}       → 直接锁定 TextBlock
{"type": "tool_use", "name": "calc"}     → 直接锁定 ToolUseBlock
{"type": "thinking", "thinking": "..."}  → 直接锁定 ThinkingBlock
```

### 3.2 智能类型收窄（解决 IDE 补全难题）

遍历时配合 `if` 或 `match`，IDE 能自动识别具体类型，给出正确的属性补全：

```python
for block in response.content:
    if block.type == "text":
        print(block.text)          # IDE 知道这是 TextBlock，有 .text 属性
    elif block.type == "tool_use":
        print(block.name)          # IDE 知道这是 ToolUseBlock，有 .name 属性
```

### 3.3 严格遵循 OpenAPI 契约

这种写法是 OpenAPI 规范中 `oneOf` + `discriminator` 在 Python 类型系统中的 1:1 映射，保证前后端契约严格同步。`anthropic` SDK 由 [Stainless](https://stainless.com/) 代码生成器自动生成，类型定义直接来自 Anthropic API 的 OpenAPI 规范。

---

## 4. 涉及的库与归属

| 语法/类 | 所属 | 说明 |
|:---|:---|:---|
| `Union` | `typing`（Python 标准库） | 类型提示，表示"或"关系 |
| `Annotated` | `typing`（Python 3.9+） | 给类型附加额外的元数据 |
| `PropertyInfo` | **`anthropic._utils._transform`**（SDK 内部） | 存放 `discriminator` 等元数据，供 SDK 反序列化框架读取 |
| `Field(discriminator="type")` | **Pydantic**（第三方库） | 如果你自己在项目中实现类似效果，`Field` 是标准方案 |

> **容易混淆的地方**：`PropertyInfo` 并不是"项目自定义"的类——它来自 Anthropic SDK 内部，路径是 `anthropic._utils._transform.PropertyInfo`。它与 Pydantic 的 `Field` 函数作用类似，但专门为 SDK 的代码生成场景设计。

---

## 5. 与本项目 s01 代码的实际关联

在 `s01_agent_loop/code.py` 中，Agent Loop 遍历 `response.content` 来执行工具调用：

```python
for block in response.content:              # ← block 的类型就是 ContentBlock
    if block.type == "tool_use":             # ← 判断是否工具调用
        output = run_bash(block.input["command"])  # ← IDE 自动补全 .input 和 .name
        results.append({
            "type": "tool_result",
            "tool_use_id": block.id,         # ← 需要 .id 来匹配 tool_use_id
            "content": output,
        })
```

这里 `block.type == "tool_use"` 之所以能工作，正是因为 `ContentBlock` 使用了 `PropertyInfo(discriminator="type")`——SDK 在反序列化时已经根据 `type` 字段将 JSON 数据转为对应的 Python 对象，所以 `block` 上的 `.input`、`.id`、`.name` 等属性在 IDE 中都是类型安全的。

---

## 6. 自己动手：用 Pydantic 实现相同的可辨识联合

如果你在自己的项目中需要实现类似效果，Pydantic v2 原生支持 `discriminator`。

### 6.1 完整代码

```python
from typing import List, Union, Literal
from typing_extensions import Annotated, TypeAlias
from pydantic import BaseModel, Field
import json

# 一、定义具体的 Block 类型（每个有自己的 type 字面量）
class TextBlock(BaseModel):
    type: Literal["text"] = "text"
    text: str
    emoji: str | None = None

class ImageBlock(BaseModel):
    type: Literal["image"] = "image"
    url: str
    width: int = 800

class ToolCallBlock(BaseModel):
    type: Literal["tool_call"] = "tool_call"
    name: str
    arguments: dict[str, object]

# 二、定义带判别的联合类型（对应 SDK 中的 ContentBlock）
ContentBlock: TypeAlias = Annotated[
    Union[TextBlock, ImageBlock, ToolCallBlock],
    Field(discriminator="type")   # ← 核心：根据 "type" 字段区分
]

# 三、定义顶层消息模型（对应 SDK 中的 Message）
class ChatMessage(BaseModel):
    role: Literal["user", "assistant"]
    content: List[ContentBlock]

# 四、测试反序列化与类型收窄
raw_data = {
    "role": "assistant",
    "content": [
        {"type": "text", "text": "Hello!", "emoji": "👋"},
        {"type": "image", "url": "https://example.com/photo.jpg"},
        {"type": "tool_call", "name": "get_weather", "arguments": {"city": "Shanghai"}}
    ]
}

msg = ChatMessage.model_validate(raw_data)

for block in msg.content:
    match block:
        case TextBlock():
            print(f"[文本] {block.text} {block.emoji or ''}")
        case ImageBlock():
            print(f"[图片] {block.url} (尺寸: {block.width})")
        case ToolCallBlock():
            print(f"[工具] 调用 {block.name}, 参数: {block.arguments}")
```

### 6.2 案例特性清单

| 特性 | 代码体现 | 价值 |
|:---|:---|:---|
| 带判别的联合 | `Field(discriminator="type")` | 零歧义、高性能反序列化 |
| 字面量类型约束 | `Literal["text"] = "text"` | 防止拼写错误 |
| 可选字段 | `emoji: str \| None = None` | 优雅处理字段缺失 |
| 类型收窄 | `match block:` + `case TextBlock():` | IDE 自动补全 |
| 双向序列化 | `model_dump_json()` | 支持 JSON 输入/输出 |
| 自动校验 | 传入 `{"type": "unknown"}` 会抛异常 | 强健壮性 |

---

## 7. `Field(discriminator="type")` 与其他 Pydantic 参数

可辨识联合的核心是 `Field(discriminator="type")`。`Field` 函数还支持其他参数，但**与本文主题最相关的是 `discriminator`**：

| 类别 | 参数 |
|:---|:---|
| **可辨识联合** | `discriminator` |
| **默认值** | `default`, `default_factory` |
| **字段别名** | `alias`, `validation_alias`, `serialization_alias` |
| **文档** | `title`, `description`, `examples` |
| **校验** | `gt`, `lt`, `ge`, `le`, `min_length`, `max_length`, `pattern` |
| **行为** | `frozen`, `exclude`, `repr`, `init` |

---

## 8. 总结

| 场景 | 推荐方案 |
|:---|:---|
| 在项目代码中遍历 `response.content` 判断消息类型 | 使用 `block.type == "xxx"` + IDE 自动类型收窄 |
| 理解 SDK 为何能根据 `type` 字段自动转换类 | 了解 `Annotated[Union[...], PropertyInfo(discriminator="type")]` 机制 |
| 自己实现类似的多态反序列化 | 使用 Pydantic + `Field(discriminator="type")` |
| 查询 SDK 中有哪些具体的 Block 类型 | 查阅 `anthropic.types.ContentBlock` 的 `Union` 参数 |

---

## 9. 参考链接

- [Anthropic Python SDK 源码（GitHub）](https://github.com/anthropics/anthropic-sdk-python)
- [Pydantic 官方文档 - Discriminated Unions](https://docs.pydantic.dev/latest/concepts/unions/#discriminated-unions)
- [Python typing — 类型提示支持](https://docs.python.org/zh-cn/3/library/typing.html)
- [PEP 593 – Flexible function and variable annotations (`Annotated`)](https://peps.python.org/pep-0593/)

---

**文档生成时间：** 2026-06-25（基于 `anthropic` SDK 真实类型分析）
