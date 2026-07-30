# Python `threading.Lock` 用法与模式

## 问题起源

在 s13（后台任务）中看到 `threading.Lock` 保护 `background_tasks` 和 `background_results` 两个字典，来防止主线程和工作线程同时读写导致数据混乱。这篇笔记整理锁的基本用法，以及 s13 之外的常见锁模式。

---

## 1. 锁是什么

锁（Lock）是最基本的线程同步原语。它保证**同一时刻只有一个线程可以执行被锁保护的代码段**。

类比：浴室门锁。一个人进去锁上门，其他人必须等里面的人出来才能进。出来的人开门，下一个人才能进去。

```python
import threading

lock = threading.Lock()

# 方式一：with 语句（推荐）
with lock:
    # 访问共享数据
    shared_dict["key"] = "value"

# 方式二：手动 acquire/release
lock.acquire()
shared_dict["key"] = "value"
lock.release()  # 容易忘记，忘记会导致死锁
```

`with lock:` 等价于 `try: lock.acquire() ... finally: lock.release()`，即使中间代码抛出异常也会自动释放锁。

---

## 2. s13 的锁使用：保护共享字典

### 共享的数据

```python
background_tasks: dict[str, dict] = {}      # bg_id → 任务状态
background_results: dict[str, str] = {}     # bg_id → 执行结果
background_lock = threading.Lock()
```

### 哪些线程在读写

| 线程 | 写操作 | 读操作 |
|------|--------|--------|
| **主线程** | `start_background_task` 里注册任务 | `collect_background_results` 里遍历/取出结果 |
| **工作线程** (×N) | `worker` 里标记 completed + 写结果 | — |

### 上锁的位置

```python
# start_background_task：主线程注册任务
with background_lock:
    background_tasks[bg_id] = {"status": "running", ...}   # ← 写

# worker：工作线程标记完成
with background_lock:
    background_tasks[bg_id]["status"] = "completed"         # ← 写
    background_results[bg_id] = result                       # ← 写

# collect_background_results：主线程收集结果
with background_lock:
    ready_ids = [...]                                         # ← 读
for bg_id in ready_ids:
    with background_lock:
        task = background_tasks.pop(bg_id)                  # ← 读+删
        output = background_results.pop(bg_id, "")           # ← 读+删
```

### 这个锁解决了什么问题

**数据竞争（data race）：** 如果主线程正在遍历 `background_tasks.items()` 的同时，工作线程在修改 `background_tasks` 的同一项，Python 可能会抛出异常（`dict mutated during iteration`），或者读到不一致的状态——比如 `status=completed` 但对应的 `background_results` 还没写入。

### 为什么锁的范围这么小

注意 `collect_background_results` 不是用一个连续的大锁包住整个循环，而是在每次 `pop` 时才单独上锁。这是因为构造 `<task_notification>` 字符串（`f"<task_notification>..."`）只需要花时间拼字符串，**不涉及共享数据**，所以没必要占用锁让工作线程干等。这个原则叫**锁的粒度（lock granularity）**——尽量缩小锁保护的代码范围。

---

## 3. 锁的更多用法

### 3.1 常规互斥保护（保护单个关键操作）

```python
balance = 100
balance_lock = threading.Lock()

def withdraw(amount):
    with balance_lock:
        global balance
        if balance >= amount:
            balance -= amount  # 读→判→写，三步不可分割
```

如果不加锁，两个线程同时执行 `withdraw`，可能出现：A 线程读到 balance=100，B 线程也读到 balance=100，A 减 50 变成 50，B 也减 50 变成 50——实际上应该支取两次共 100，余额应是 0。原因就是 **"读→判→写"三步不是原子的**。

### 3.2 保护复合操作（读 → 处理 → 写）

```python
# 错误：两次 with lock 之间数据可能已被修改
with cache_lock:
    item = cache.get(key)
if item is None:
    item = expensive_computation()  # 这里没有锁
    with cache_lock:
        cache[key] = item            # 但到这里 key 可能已被别人写入了

# 正确：一次性锁住
with cache_lock:
    if key not in cache:          # double-checked locking
        cache[key] = expensive_computation()
```

### 3.3 `RLock`：可重入锁（同一个线程可多次 acquire）

```python
from threading import RLock

lock = RLock()

def outer():
    with lock:
        inner()    # 同一线程可以再次 acquire，不会死锁

def inner():
    with lock:
        print("inner")  # 普通 Lock 在这里会死锁；RLock 不会
```

`Lock` 不允许同一个线程 `acquire` 两次——第二次调用会阻塞等待自己释放，但自己正在等，就**死锁**了。`RLock` 允许同一个线程重复 `acquire`，内部维护一个计数器。

**使用场景：** 函数 A 加锁后再调用函数 B，B 也需要同一个锁。或者递归函数需要锁保护。

### 3.4 `Lock(blocking=False)`：非阻塞尝试

```python
lock = threading.Lock()
if lock.acquire(blocking=False):  # 拿不到锁就返回 False，不等待
    try:
        # 拿到锁了，执行操作
        ...
    finally:
        lock.release()
else:
    # 没拿到锁，干点别的
    ...
```

**使用场景：** s15 的团队协议中，多个 Agent 可能同时试图认领同一个任务。非阻塞锁可以让 Agent"试一下，拿不到就去干别的"，而不是死等。s17 的自治 Agent 可以配合这个做**空闲轮询**——尝试拿锁，拿到就处理任务，拿不到就继续轮询。

### 3.5 Condition：等待-通知模式

```python
import threading

queue = []
cv = threading.Condition()

def producer():
    with cv:
        queue.append("item")
        cv.notify()   # 通知消费者：有数据了

def consumer():
    with cv:
        while not queue:
            cv.wait()  # 等待生产者通知（自动释放锁，被唤醒后自动重新拿锁）
        item = queue.pop()
```

`Condition` 比 `Lock` 多了 `wait()` 和 `notify()` 两个操作。消费者如果发现队列为空，就释放锁并等待；生产者放入数据后通知，消费者被唤醒并重新拿到锁后继续。

**使用场景：** 任务队列（生产者-消费者模型）、管道（pipe）通信。s14 的 Cron 调度器可以用 Condition 来让主线程等待定时器触发。

### 3.6 Event：一次性的信号通知

```python
import threading

event = threading.Event()

def worker():
    print("等待信号...")
    event.wait()           # 阻塞直到 event.set()
    print("收到信号，开始工作")

def starter():
    time.sleep(5)
    event.set()            # 发出信号

t = threading.Thread(target=worker)
t.start()
starter()
```

`Event` 比 `Condition` 更简单——就是一个开关，`set()` 之后所有 `wait()` 的线程全部放行，无法重置（除非手动 `clear()`）。

### 3.7 Semaphore：限制并发数量

```python
import threading

sem = threading.Semaphore(3)  # 最多允许 3 个线程同时进入

def limited_work():
    with sem:
        print("doing work...")
        time.sleep(1)

for i in range(10):
    threading.Thread(target=limited_work).start()
```

`Semaphore` 维护一个计数器，`acquire` 减一（为 0 时阻塞），`release` 加一。相当于**允许多少个线程同时访问资源**的场景——数据库连接池、API 限流等。

s13 的 `threading.Thread(target=worker, daemon=True)` 启动的是无限并发（每来一个后台任务就起一个新线程）。生产环境应该用 `Semaphore` 或线程池（`concurrent.futures.ThreadPoolExecutor`）限制并发数。

---

## 4. 常见问题

### 4.1 什么是原子操作？哪些操作不需要锁？

Python 中某些操作是原子的（GIL 保证），CPython 解释器下一行代码不会被线程切换打断到一半：

| 操作 | 是否原子 | 说明 |
|------|---------|------|
| `d[key] = val` | 是 | 单个 dict 写入 |
| `list.append(x)` | 是 | 单个 list 写入 |
| `count += 1` | **否** | 读→加→写三步，会被切换 |
| `d[key] += 1` | **否** | 同上 |
| `if d[key]: ...` | 是 | 但判断后紧接着的操作不是 |

**规则：** 如果不知道是不是原子操作，就加锁。在 s13 中，虽然 `background_tasks[bg_id] = {...}` 本身是原子的，但 `collect_background_results` 中遍历和 `pop` 交替进行，不加锁就会有问题（遍历过程中被其他线程修改字典）。

### 4.2 什么是死锁？

```python
lock_a = threading.Lock()
lock_b = threading.Lock()

# 线程 1
with lock_a:
    with lock_b:  # 等待 lock_b
        ...

# 线程 2
with lock_b:
    with lock_a:  # 等待 lock_a
        ...
```

线程 1 拿着 lock_a 等 lock_b，线程 2 拿着 lock_b 等 lock_a——互相等对方释放，永远等不到。

**避免方法：**
- 所有线程按**固定顺序**拿锁（总是先 A 后 B）
- 使用 `lock.acquire(timeout=...)` 超时放弃，检测到死锁后回滚重试
- s13 只有一把锁，所以不存在死锁问题

### 4.3 锁对性能的影响

- 锁本身很轻量（不竞争时约 50ns）
- 锁竞争（contention）才是问题：一个线程拿着锁，其他线程在 `with lock:` 外面排队
- 规则：**缩小锁范围**——只锁住最短的必要代码
- s13 中 `collect_background_results` 两次上锁而不是一次大锁，就是这个原因

---

## 5. s13 的锁设计总结

| 方面 | s13 的做法 | 为什么这样设计 |
|------|-----------|-------------|
| 锁的类型 | `threading.Lock`（普通互斥锁） | 只有两个线程角色，不需要重入 |
| 锁的数量 | 1 把锁 | 保护同一组共享数据，一把够用 |
| 锁的粒度 | 只覆盖字典的读写那一行 | 不阻塞格式化字符串等无关操作 |
| 字典遍历 | 先锁住获得 `ready_ids` 列表，再逐个锁住 `pop` | 避免大锁长时间占用 |
| `pop` 移除 | 取走后从字典中删除 | 确保每个通知只生成一次，不被重复收集 |
