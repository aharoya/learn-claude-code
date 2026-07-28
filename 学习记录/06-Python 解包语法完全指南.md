---

# Python 解包（Unpacking）语法完全指南：`*` 与 `**` 的使用场景大全

> 本文源于 s09 中 `{**messages[memory_turn], "content": new_content}` 用法的追问，系统整理 Python 解包语法的所有常见场景。从基础的列表解包到高级的嵌套结构展开，覆盖 `*` 和 `**` 的完整用法。

---

## 1. 基础：两种解包运算符

| 运算符 | 作用对象 | 含义 |
|---|---|---|
| `*` | 可迭代对象（list、tuple、str、range……） | 展开成位置元素 |
| `**` | 字典（dict） | 展开成键值对 |

它们有两个核心使用场景：**赋值解包**（把容器拆成变量）和 **表达式解包**（在表达式里展开容器）。

---

## 2. 赋值解包：把容器拆成变量

### 2.1 基础序列解包 —— 左右数量相等

```python
a, b, c = [1, 2, 3]
# a=1, b=2, c=3

x, y = (10, 20)
# x=10, y=20
```

### 2.2 `*` 收集剩余元素 —— 左右数量不等

左侧带 `*` 的变量会收集所有剩下的元素（永远返回一个 list）：

```python
first, *rest = [1, 2, 3, 4]
# first=1, rest=[2, 3, 4]

*head, last = [1, 2, 3, 4]
# head=[1, 2, 3], last=4

first, *middle, last = [1, 2, 3, 4, 5]
# first=1, middle=[2, 3, 4], last=5
```

**常见场景 1**：取首元素，剩下的扔给 `_` 忽略

```python
first, *_ = some_list   # 只关心第一个
```

**常见场景 2**：切片替代——比索引更可读

```python
# 不推荐
head = items[0]
tail = items[1:]

# 推荐
head, *tail = items
```

**常见场景 3**：字符串拆成首字符 + 剩余

```python
first_char, *rest_chars = "hello"
# first_char='h', rest_chars=['e', 'l', 'l', 'o']
```

### 2.3 `**` 字典解包赋值 —— Python 3.10+

Python 3.10 开始支持在赋值目标中使用 `**`：

```python
d = {"a": 1, "b": 2, "c": 3, "d": 4}
a, b, **rest = d
# a='a', b='b', rest={'c': 3, 'd': 4}
# 注意：默认解包的是键，不是值！
```

想解包成值和键一起，要搭配 `items()`：

```python
# 没有直接的一行写法，需要循环
```

---

## 3. 表达式解包：在表达式中展开容器

### 3.1 合并列表 / 元组 — `*`

```python
a = [1, 2, 3]
b = [4, 5, 6]

# 传统：a + b
combined = [*a, *b]      # [1, 2, 3, 4, 5, 6]

# 在中间插入
combined = [0, *a, 99, *b]  # [0, 1, 2, 3, 99, 4, 5, 6]

# 元组同理
t = (*a, *b)             # (1, 2, 3, 4, 5, 6)

# 字符串也会展开成字符
char_list = [*"hello"]   # ['h', 'e', 'l', 'l', 'o']
```

### 3.2 合并字典 — `**`（这也是 s09 所用的！）

```python
d1 = {"a": 1, "b": 2}
d2 = {"c": 3, "d": 4}

combined = {**d1, **d2}
# {'a': 1, 'b': 2, 'c': 3, 'd': 4}
```

**关键特性：后出现的键覆盖前面的**：

```python
d1 = {"a": 1, "b": 2}
d2 = {"b": 999, "c": 3}

combined = {**d1, **d2}
# {'a': 1, 'b': 999, 'c': 3}   ← d2 的 "b" 覆盖了 d1 的 "b"

combined = {**d2, **d1}
# {'a': 1, 'b': 2, 'c': 3}     ← d1 的 "b" 覆盖了 d2 的 "b"
```

**这就是 s09 的实际用法**：

```python
# 把原消息字典展开，然后用一个新的 "content" 覆盖它
request_messages[memory_turn] = {
    **messages[memory_turn],          # 展开 {"role": "user", "content": "写个 test.py"}
    "content": new_memories_content,  # 覆盖 content
}
# 结果：{"role": "user", "content": "新内容……"}
```

**和 dict() 构造函数的对比**：

```python
# 等价的几种写法
{**old, "key": "val"}       # 解包 + 覆盖（推荐，最直观）
dict(old, key="val")        # dict() 构造函数合并（只能处理字符串键）
{**dict1, **dict2, "key": "val"}  # 多个字典合并 + 覆盖
```

### 3.3 解包字面量创建集合

```python
a = [1, 2, 3]
b = [3, 4, 5]

s = {*a, *b}    # {1, 2, 3, 4, 5}
```

---

## 4. 函数调用时解包

这是最常用也最容易混淆的地方。

### 4.1 `*` 将可迭代对象展开为位置参数

```python
def add(a, b, c):
    return a + b + c

nums = [1, 2, 3]
add(*nums)          # 相当于 add(1, 2, 3) → 6

# str 也会逐个字符展开
list("abc")         # 相当于 list('a', 'b', 'c') → 不对！list 只接受一个参数
# 正确：list("abc") → ['a', 'b', 'c']  # 这是 str 可迭代，不是解包！
print(*"abc")       # print('a', 'b', 'c') → 输出 "a b c"
```

**典型场景**：`print()` 的参数列表拼接

```python
lines = ["a.txt", "b.txt", "c.txt"]
print(*lines)           # a.txt b.txt c.txt
print(*lines, sep="\n") # 每个一行

# 等价于
print("a.txt", "b.txt", "c.txt", sep="\n")
```

**典型场景**：`range()` 拆开

```python
args = [5, 10, 2]
list(range(*args))   # range(5, 10, 2) → [5, 7, 9]
```

**典型场景**：`zip()` 解包

```python
pairs = [(1, 'a'), (2, 'b'), (3, 'c')]
numbers, letters = zip(*pairs)
# numbers = (1, 2, 3), letters = ('a', 'b', 'c')
# 相当于 zip((1, 'a'), (2, 'b'), (3, 'c'))
```

### 4.2 `**` 将字典展开为关键字参数

```python
def greet(name, greeting="Hello"):
    return f"{greeting}, {name}!"

params = {"name": "Alice", "greeting": "Hi"}
greet(**params)     # 相当于 greet(name="Alice", greeting="Hi") → "Hi, Alice!"
```

**典型场景**：配置字典传给函数

```python
config = {"host": "localhost", "port": 8080, "debug": True}
# 传统：server.setup(config["host"], config["port"], config["debug"])
# 解包：server.setup(**config)  ← 前提是参数名和键名一致
```

**典型场景**：`dataclass` 或 Pydantic 模型的动态构造

```python
data = {"name": "Alice", "age": 30}
user = User(**data)

# 等价于 User(name="Alice", age=30)
```

**典型场景**：继承时的 `super().__init__` 透传

```python
class Base:
    def __init__(self, a, b, c):
        ...

class Derived(Base):
    def __init__(self, **kwargs):
        # 把子类收到的关键字参数原样传给父类
        super().__init__(**kwargs)
```

### 4.3 混合使用 `*` 和 `**`

```python
def f(a, b, c, d, e):
    pass

args = [1, 2]
kwargs = {"d": 4, "e": 5}
f(*args, 3, **kwargs)   # 相当于 f(1, 2, 3, d=4, e=5)
```

顺序规则：**位置参数 → `*` 解包 → 位置参数 → `**` 解包 → 关键字参数**（Python 3.5+）

---

## 5. 函数定义时的"反向解包"（参数收集）

也就是 `*args` 和 `**kwargs`——它和解包互为逆操作。

### 5.1 `*args`：收集多余的位置参数

```python
def log(msg, *tags):
    # tags 是一个 tuple，收集所有额外的位置参数
    print(f"[{', '.join(tags)}] {msg}")

log("hello", "info", "debug")  # msg="hello", tags=("info", "debug")
```

### 5.2 `**kwargs`：收集多余的关键字参数

```python
def render(template, **options):
    # options 是一个 dict，收集所有额外的关键字参数
    print(f"Render {template} with {options}")

render("index.html", cache=True, lang="zh")
# template="index.html", options={"cache": True, "lang": "zh"}
```

### 5.3 强制关键字参数（`*` 分隔）

```python
def f(a, b, *, c, d):
    # c 和 d 只能通过关键字传递，不能传位置参数
    pass

f(1, 2, c=3, d=4)   # ✅
f(1, 2, 3, 4)        # ❌ TypeError
```

### 5.4 多级透传

```python
def outer(**kwargs):
    # 只处理自己关心的参数，其余透传给 inner
    inner(**kwargs)

def inner(host, port, **extra):
    # 同样可以继续透传
    deeper(**extra)
```

---

## 6. 嵌套解包

### 6.1 列表嵌套

```python
data = [1, [2, 3], 4]
a, (b, c), d = data
# a=1, b=2, c=3, d=4
```

### 6.2 字典嵌套（结合 items）

```python
nested = {"x": {"a": 1, "b": 2}, "y": {"c": 3}}
# 遍历时解包
for key, (sub_a, sub_b) in nested.items():
    pass  # 注意：这只是解包键，不是解包值
```

### 6.3 match-case 模式匹配中的解包（Python 3.10+）

```python
match point:
    case (0, 0):
        print("原点")
    case (x, 0):
        print(f"x 轴上的 {x}")
    case (0, y):
        print(f"y 轴上的 {y}")
    case (x, y):
        print(f"({x}, {y})")
```

---

## 7. 完整速查表

### 表达式解包

| 写法 | 结果类型 | 说明 |
|---|---|---|
| `[*a, *b]` | list | 合并列表 |
| `{*a, *b}` | set | 合并成集合并去重 |
| `(*a, *b)` | tuple | 合并元组 |
| `{**d1, **d2}` | dict | 合并字典，后者覆盖前者 |

### 函数参数

| 位置 | 写法 | 含义 |
|---|---|---|
| 调用时 | `f(*iterable)` | 可迭代对象 → 位置参数 |
| 调用时 | `f(**dict)` | 字典 → 关键字参数 |
| 定义时 | `def f(*args)` | 多余位置参数 → tuple |
| 定义时 | `def f(**kwargs)` | 多余关键字参数 → dict |
| 定义时 | `def f(a, b, *, c)` | `*` 之后只能关键字传递 |

### 赋值

| 写法 | 含义 |
|---|---|
| `a, *b = iterable` | 首元素 → a，剩余 → b（list） |
| `*a, b = iterable` | 尾元素 → b，前面全部 → a |
| `a, *b, c = iterable` | 首 → a，尾 → c，中间 → b |

---

## 8. 常见误区

### 误区 1：`{**d1, **d2}` 是浅拷贝

```python
d1 = {"key": [1, 2, 3]}
d2 = {**d1}
d2["key"].append(4)
# d1["key"] 也变成了 [1, 2, 3, 4]  ← 浅拷贝！
```

### 误区 2：`*` 不能解包 dict（默认只解包键）

```python
d = {"a": 1, "b": 2}
# print(**d)        ❌ 语法错误
# print(*d)         ✅ 输出 "a b"（只展开键）
# print(*d.values()) ✅ 输出 "1 2"
```

### 误区 3：函数定义和调用的 `*` / `**` 是不同方向的操作

不要混淆：
- **函数定义**：`def f(*args)` — 把收到的参数**收集**成一个 tuple
- **函数调用**：`f(*list)` — 把 list **展开**成多个位置参数

它们是互逆操作，但方向不同。

### 误区 4：`**kwargs` 的键必须是字符串

```python
{**{1: "a"}}       # ✅ 可以，dict 的键可以是 int
f(**{1: "a"})      # ❌ TypeError: keywords must be strings
```

---

## 9. 和 s09 代码的具体关联

s09 的那一行：

```python
request_messages[memory_turn] = {
    **messages[memory_turn],          # 展开原消息字典
    "content": memories_content + "\n\n" + messages[memory_turn]["content"],
}
```

拆解：
1. `messages[memory_turn]` 是一个 dict：`{"role": "user", "content": "原始问题"}`
2. `{**messages[memory_turn], "content": "新内容"}` 合并展开并覆盖
3. 结果：`{"role": "user", "content": "记忆内容\n\n原始问题"}`
4. 这个操作**不修改**原始的 `messages[memory_turn]`，因为字典字面量 `{...}` 创建了一个新 dict

如果不解包，等价的两段式写法：

```python
new_msg = dict(messages[memory_turn])           # 复制原消息
new_msg["content"] = memories_content + "\n\n" + messages[memory_turn]["content"]  # 替换 content
request_messages[memory_turn] = new_msg
```

解包写法更紧凑，而且一眼就能看出"其他不变，只改 content"的意图。

---

## 10. 参考链接

- [PEP 448 – Additional Unpacking Generalizations](https://peps.python.org/pep-0448/)（Python 3.5，扩展了 `*` 和 `**` 在表达式中的使用）
- [Python 官方文档 – 解包列表](https://docs.python.org/zh-cn/3/tutorial/controlflow.html#unpacking-argument-lists)
- [Python 官方文档 – `**` 运算符](https://docs.python.org/zh-cn/3/reference/expressions.html#dictionary-displays)

---

**文档生成时间：** 2026-07-28
