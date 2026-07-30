# s14: Cron Scheduler — 按时间表生产工作

[中文](README.md) · [English](README.en.md) · [日本語](README.ja.md)

s01 → ... → s12 → s13 → `s14` → [s15](../s15_agent_teams/) → s16 → ... → s20
> *"按时间表生产工作, 调度与执行解耦"* — cron 调度, 持久化或会话级。
>
> **Harness 层**: 调度 — 独立线程判断时间, 队列传递触发。

---

## 问题

闹钟不需要你盯着它才会响。你设好 7:00，到点它自己响，你在睡觉、在洗澡、在做饭，它都照响不误。

s13 让 Agent 能后台执行慢操作，但所有操作仍然是你手动触发的。你说一句，Agent 动一下。"每天早上 9 点跑测试"、"每 30 分钟检查 CI 状态"，这些周期性任务不该需要人每次来推。

---

## 解决方案

![Cron Scheduler Overview](images/cron-scheduler-overview.svg)

教学代码沿用 S13 的简化任务系统、后台执行和 prompt 组装；为了聚焦调度器，省略完整错误恢复、记忆和技能系统。新增：独立的 cron 调度线程，每秒检查一次，时间到了把任务塞进 `cron_queue`；再由 queue processor 在 Agent 空闲时自动交付。

手动 vs 定时：

| | 手动触发 (s13) | 定时触发 (s14) |
|---|---|---|
| 触发者 | 用户输入 | 调度线程 |
| 触发时机 | 随时 | cron 表达式指定 |
| 需要人参与 | 是 | 否（调度器自动入队，空闲时自动交付） |
| 持久性 | — | durable 跨重启 |

---

## 工作原理

### 四层模型

Cron 调度分四层：

1. **Scheduler**：daemon 线程，每秒轮询，判断时间到了没有
2. **Queue**：`cron_queue`，调度线程写入已触发任务
3. **Queue Processor**：发现队列非空且 Agent 空闲，启动一轮 agent_loop
4. **Consumer**：agent_loop 从队列消费，注入到 messages

教学版实现的是最小 queue processor：用 `agent_lock` 判断 Agent 是否空闲，空闲时自动交付定时任务。真实 CC 的 `useQueueProcessor.ts` 还会处理 UI 阻塞、队列优先级和不同消息模式。

### CronJob: 数据结构

每个 cron 任务是一个 `CronJob` 对象：

```python
@dataclass
class CronJob:
    id: str
    cron: str        # "0 9 * * *" (五段式 cron 表达式)
    prompt: str      # 触发时注入给 Agent 的消息
    recurring: bool  # True=周期性，False=一次性
    durable: bool    # True=写磁盘，跨会话保留
```

Cron 表达式，五段式，Unix 用了 50 年：

```
分钟  小时  日  月  星期
  *    *   *   *   *      每分钟
  0    9   *   *   *      每天早上 9:00
 */5    *   *   *   *      每 5 分钟
  0    9   *   *  1-5     工作日早上 9:00
```

支持 `*`、`*/N`、`N`、`N-M`、`N,M,...`。

### cron_matches: 五段式匹配

标准 cron 语义：分钟、小时、月必须全部匹配；日（DOM）和星期（DOW）同时被约束时任一匹配即可（OR）：

```python
def cron_matches(cron_expr: str, dt: datetime) -> bool:
    fields = cron_expr.strip().split()
    if len(fields) != 5:
        return False
    minute, hour, dom, month, dow = fields
    dow_val = (dt.weekday() + 1) % 7  # Python Monday=0 → cron Sunday=0

    m = _cron_field_matches(minute, dt.minute)
    h = _cron_field_matches(hour, dt.hour)
    dom_ok = _cron_field_matches(dom, dt.day)
    month_ok = _cron_field_matches(month, dt.month)
    dow_ok = _cron_field_matches(dow, dow_val)

    if not (m and h and month_ok):
        return False
    # DOM and DOW: both constrained → either matching is enough (OR)
    dom_unconstrained = dom == "*"
    dow_unconstrained = dow == "*"
    if dom_unconstrained and dow_unconstrained:
        return True
    if dom_unconstrained:
        return dow_ok
    if dow_unconstrained:
        return dom_ok
    return dom_ok or dow_ok
```

### 独立调度线程: 每秒轮询

调度器跑在独立的 daemon 线程里，不依赖 agent_loop 是否在执行。单个 job 异常不会杀掉整个线程：

```python
def cron_scheduler_loop():
    while True:
        time.sleep(1)
        now = datetime.now()
        minute_marker = now.strftime("%Y-%m-%d %H:%M")
        with cron_lock:
            for job in list(scheduled_jobs.values()):
                try:
                    if cron_matches(job.cron, now):
                        if _last_fired.get(job.id) != minute_marker:
                            cron_queue.append(job)
                            _last_fired[job.id] = minute_marker
                        if not job.recurring:
                            scheduled_jobs.pop(job.id, None)
                            if job.durable:
                                save_durable_jobs()
                except Exception as e:
                    print(f"[cron error] {job.id}: {e}")
```

关键设计：
- **独立于 agent_loop**：即使 agent_loop 没在跑，调度器也在后台检查时间
- **date-aware minute_marker**：用 `"YYYY-MM-DD HH:MM"` 防止同一分钟重复触发，同时不会在第二天跳过
- **单 job try/except**：一个坏 job 不会拖垮整个调度线程
- **一次性任务**：触发后自动从 scheduled_jobs 里删除

### Queue Processor + agent_loop: 交付端

queue processor 不检查时间，只负责在队列有任务且 Agent 空闲时拉起一轮执行：

```python
def queue_processor_loop():
    while True:
        time.sleep(0.2)
        if not has_cron_queue():
            continue
        if not agent_lock.acquire(blocking=False):
            continue
        try:
            if has_cron_queue():
                run_agent_turn_locked()
        finally:
            agent_lock.release()
```

agent_loop 也不负责检查时间，它只从 `cron_queue` 里拿已触发的任务，注入到 messages 里：

```python
fired = consume_cron_queue()
for job in fired:
    messages.append({"role": "user",
                     "content": f"[Scheduled] {job.prompt}"})
```

生产者（调度线程）、交付者（queue processor）和消费者（agent_loop）通过 `cron_queue`、`cron_lock`、`agent_lock` 解耦。

### 校验：防止坏 cron 杀掉调度器

`schedule_job` 在注册前校验 cron 表达式，非法的直接返回错误：

```python
def schedule_job(cron, prompt, recurring=True, durable=True):
    err = validate_cron(cron)
    if err:
        return err
    # ... register job
```

从磁盘加载 durable job 时也会跳过非法表达式，避免单个坏任务拖垮启动。

### Durable vs Session-only

- **Durable**：任务定义写进 `.scheduled_tasks.json`。Agent 重启后加载文件，恢复任务。
- **Session-only**：只在内存里。Agent 关闭就没了。

> **重要前提**：cron 调度器必须在 Agent 进程内跑。进程关闭，调度也停。Durable 只意味着任务定义跨重启保留，下次 Agent 启动时调度器才会发现"该触发了"并触发。如果需要"即使应用关闭也能定时跑"，请用系统 crontab 或 systemd timer。

### 合起来跑

```
1. 启动时：
   load_durable_jobs() → 从 .scheduled_tasks.json 恢复持久化任务
   Thread(cron_scheduler_loop, daemon=True).start() → 调度线程开始轮询
   Thread(queue_processor_loop, daemon=True).start() → 队列处理器等待交付

2. 注册任务：
   schedule_cron(cron="*/2 * * * *", prompt="run date", durable=True)
   → CronJob 写入 scheduled_jobs + .scheduled_tasks.json

3. 每 2 分钟：
   调度线程检查 → cron_matches 返回 True → cron_queue.append(job)
   → queue processor 发现 Agent 空闲 → agent_loop consume_cron_queue
   → 注入 "[Scheduled] run date"
   → LLM 收到消息，执行 date 命令

4. 关闭进程：
   调度线程跟着停（daemon=True）
   .scheduled_tasks.json 还在磁盘上
   下次启动 → load_durable_jobs → 任务恢复
```

---

## 相对 s13 的变更

| 组件 | 之前 (s13) | 之后 (s14) |
|------|-----------|-----------|
| 触发方式 | 用户手动触发 | 调度线程自动入队 |
| 新类型 | — | CronJob dataclass (id, cron, prompt, recurring, durable) |
| 新函数 | — | cron_matches, validate_cron, schedule_job, cancel_job, cron_scheduler_loop, queue_processor_loop |
| 新存储 | — | .scheduled_tasks.json (durable) + 内存 (session-only) |
| 线程 | 后台执行线程 | + 调度线程 (daemon, 1s 轮询) + queue processor 线程 |
| 队列 | background_results | + cron_queue (调度线程写, queue processor 交付, agent_loop 消费) |
| 工具 | 8 (s12/s13) | + schedule_cron, list_crons, cancel_cron (11) |

---

## 注意事项

### 1. 四层解耦：调度器不关心 Agent 忙不忙

```
Scheduler → Queue → Queue Processor → Consumer
```

整个 cron 系统拆成四个独立角色，各管各的：

| 层 | 职责 | 只看什么 |
|---|------|---------|
| Scheduler | `cron_matches` 匹配时间 | 当前时间、cron 表达式 |
| Queue | `cron_queue` 列表 | 什么也不看，只是"筐" |
| Queue Processor | 队列非空 + Agent 空闲 → 拉起 agent_loop | 队列长度、agent_lock |
| Consumer（agent_loop） | 消费队列 → [Scheduled] 注入 messages | 队列内容 |

**好处**：每层的逻辑简单，改一层不影响其他层。比如想改成每 5 秒检查一次，只改 `cron_scheduler_loop` 里的 `time.sleep(1)` 就行。

**代价**：从触发到执行有延迟窗口。`cron_matches` 为 True 的时间点 ≠ Agent 实际处理的时间点。如果 Agent 正在忙，队列会积压。

### 2. agent_lock：用户优先于 cron

```python
# 用户输入：阻塞等待锁（用户必须等到）
with agent_lock:
    run_agent_turn_locked(query)

# cron 触发：不阻塞，锁被占就跳过
if agent_lock.acquire(blocking=False):
    try:
        run_agent_turn_locked()
    finally:
        agent_lock.release()
```

**关键区别**：
- `with agent_lock` 会**阻塞等待**，拿不到锁就等着，直到拿到为止
- `acquire(blocking=False)` **不等待**，拿不到就返回 False，直接跳过

用户交互期间 cron 任务不会打断——cron 是"后台任务"，用户输入是"前台任务"。如果用户连续输入 10 分钟，cron 任务就在队列里等 10 分钟。**cron 的触发时间是"最早可用时间"，不是精确时间。**

**Python 语法说明**：`with agent_lock:` 是 `lock.acquire()` + `try: ... finally: lock.release()` 的简写。`acquire(blocking=False)` 是"尝试获取锁，拿不到就算了"（不阻塞当前线程）。这两个是 Python `threading.Lock` 的标准用法。

### 3. `_last_fired` 用完整日期时间防止重复触发

```python
minute_marker = now.strftime("%Y-%m-%d %H:%M")
```

为什么不能只存 "HH:MM"？

**场景**：`*/5` 任务（每 5 分钟执行一次）在 `9:05:01` 触发，Agent 花了 3 分钟处理到 `9:08`。此时调度器又检查了：当前分钟是 9:05 还是匹配的！如果没有日期前缀，`_last_fired` 里已经存了 `"09:05"`，你没法判断这个 `"09:05"` 是今天 9:05 还是昨天 9:05。

实际上更直接的 bug：如果只存 `"09:05"`，第二天 9:05 发现 `_last_fired["job_x"]` 还是 `"09:05"`，以为已经触发过了，**跳过**。任务永远不再触发。

日期前缀（`"2026-07-30 09:05"` ≠ `"2026-07-31 09:05"`）彻底解决了跨天问题。

### 4. DOM 和 DOW 是 OR 语义（Unix 标准 cron）

```python
# "0 9 15 * 1" → 每月15号 OR 每周一 的 9:00
if dom_ok or dow_ok:
    return True
```

很多人以为 `"0 9 15 * 1"` 是"每月 15 号**且是**周一才触发"。实际上标准 Unix cron 的 DOM 和 DOW 是 **OR**——15 号不是周一也触发，每周一也触发。

如果 15 号是周二，这个表达式会在：每月 15 号（周二）+ 每个周一，一个月触发 5-6 次。要表达"15 号且是周一"需要额外逻辑，教学版不支持。

**判断逻辑拆解**：
1. 分钟、小时、月份必须全部匹配（AND）
2. DOM 和 DOW：如果两者都没约束（都是 `*`）→ 匹配
3. 只有一个约束了 → 只看那个
4. 两个都约束了 → 任一匹配即可（OR）

### 5. 一次性任务在调度线程里删除，不在 Consumer 里删

```python
if cron_matches(job.cron, now):
    if _last_fired.get(job.id) != minute_marker:
        cron_queue.append(job)
    if not job.recurring:
        scheduled_jobs.pop(job.id, None)      # ← 调度线程里删
        if job.durable:
            save_durable_jobs()
```

一次性任务（`recurring=False`）在**调度线程匹配时就删了**，不是等到 `consume_cron_queue` 消费时才删。这意味着即使队列中的一次性任务还没被 Agent 执行，它也不会再次触发。

**边界情况**：删除操作（`scheduled_jobs.pop` → `save_durable_jobs`）分两步走，中间可能崩溃。如果 `pop` 成功了但 `save_durable_jobs` 还没写磁盘就崩溃了，`.scheduled_tasks.json` 里还有这个任务。下次启动重新加载，任务回来了——但 `_last_fired` 是内存变量，丢了。所以任务会**再触发一次**。这不算大问题（一次性任务多触发一次），但如果你需要精确的一次性语义，需要考虑这个边界。

### 6. Double-check：获取锁的间隙队列可能已被消费

```python
if not agent_lock.acquire(blocking=False):
    continue       # ← 队列有货但 Agent 忙，跳过
try:
    if not has_cron_queue():   # ← 这步是 double-check
        continue               # ← 队列已经空了，跳过
    run_agent_turn_locked()
finally:
    agent_lock.release()
```

为什么 `has_cron_queue()` 要检查两次？

时间线：
```
T0: queue_processor 检查 has_cron_queue() = True（有工作）
T1: 用户输入触发的 agent_loop 执行了 consume_cron_queue，队列空了
T2: queue_processor.acquire(blocking=False) = True（拿到锁了）
T3: 如果不 double-check，直接调 run_agent_turn_locked ≡ 跑一轮空的 agent_loop
```

你拿到锁不代表队列里的东西还在。两个锁（`agent_lock` 和 `cron_lock`）是独立的，获取 `agent_lock` 的间隙，另一条路径可能已经消费了队列。这就是 double-check pattern 的必要性。

### 7. cron 任务积压：一次性注入可能淹没 LLM

```python
fired = consume_cron_queue()  # 取出所有积压任务
for job in fired:
    messages.append({"role": "user", "content": f"[Scheduled] {job.prompt}"})
```

如果用户 10 分钟没交互，积压了 30 个定时任务，这 30 条 `[Scheduled]` 消息一次性全部注入到 messages 里。LLM 看到一堆 `[Scheduled]` 可能混乱，也可能超出 token 预算。

CC 的做法是按队列优先级区分（`priority: 'later'` + `workload: WORKLOAD_CRON`），API 容量紧张时降低 cron 请求的 QoS。教学版没有这个机制。

### 8. Daemon 线程：主线程退出时强制杀死

```python
threading.Thread(target=cron_scheduler_loop, daemon=True).start()
threading.Thread(target=queue_processor_loop, daemon=True).start()
```

`s14` 启动后，后台总共有 3 个线程并行运行：
- **主线程**：`input()` 等待用户输入
- **cron_scheduler_loop**：每秒检查时间
- **queue_processor_loop**：每 0.2 秒检查队列

加上 s13 的后台 worker 线程（`start_background_task` 每次调用创建一个），线程数更多。

**daemon=True 的含义**：主线程退出时，这些线程会被**强制杀死**，不管它们是否在执行关键操作。好处是程序按 Ctrl+C 退出不用等它们慢慢关。坏处是：
- `save_durable_jobs()` 可能写到一半就被杀，`.scheduled_tasks.json` 写半截变乱码
- 后台 worker 正在 `execute_tool` 时被杀死，结果丢失

教学版不考虑 graceful shutdown。production 版本需要信号处理（`signal.SIGINT`/`SIGTERM`）让线程有机会收尾。

### 9. 持久化只存任务定义，不停 Agent 进程

```python
def save_durable_jobs():
    durable = [asdict(j) for j in scheduled_jobs.values() if j.durable]
    DURABLE_PATH.write_text(json.dumps(durable, indent=2))
```

**重要前提**：cron 调度器在 Agent **进程内**跑。进程关闭，调度也停。`durable=True` 只保证"下次启动时任务定义还在"，不保证"关闭期间也能定时触发"。

如果你希望"即使应用关闭，每天早上 9 点也跑任务"，请用系统 crontab 或 Windows Task Scheduler。

### 与官方 Claude Code 对比

| 方面 | 教学版 s14 | 官方 Claude Code |
|------|-----------|-----------------|
| 任务数量上限 | 无限制（可能导致内存泄漏） | 50 个上限（`MAX_JOBS = 50`） |
| 自动过期 | 无（必须手动 cancel） | 重复任务 7 天后自动过期（可配到 30 天） |
| 抖动（防惊群） | 无（同一秒触发所有匹配任务） | 确定性哈希抖动，触发延迟 ±10%（上限 15 分钟） |
| 文件锁 | 无（多个 session 可重复触发同一任务） | `.scheduled_tasks.lock` 防多 session 冲突 |
| 任务优先级 | 全部一视同仁 | 队列优先级 + QoS 标签分类 |
| Cron 语法 | `*/N`、`N-M`、`N,M,...` | 额外支持 `N-M/S`（跳步范围） |
| 任务积压 | 一次性全部注入 messages | 按优先级分批 + 容量感知 |
| 关闭处理 | daemon 线程直接杀 | 文件锁清理 + graceful shutdown |
| 文件监听 | 无 | `chokidar` 监听 `scheduled_tasks.json` 变更 |

---

## 试一下

```sh
cd learn-claude-code
python s14_cron_scheduler/demo_code.py
```

试试这些 prompt：

1. `Schedule a task to print the current date every 2 minutes`
2. `List all cron jobs`
3. `Create a one-shot reminder in 1 minute to check the build status`
4. `Cancel the recurring job and verify with list_crons`

观察重点：调度线程是否在独立运行？cron 任务是否在正确的时间点触发？不输入新 prompt 时，是否也出现 `[queue processor]` 并自动执行？durable job 是否写入了 `.scheduled_tasks.json`？

---

## 接下来

一个 Agent 能做很多事了，能计划、能压缩、能后台、能定时。但有些任务太大了，不是一个 Agent 能搞定的。

"重构整个后端"，把认证模块、数据库层、API 路由、测试全部翻新。一个 Agent 的注意力是有限的，这需要一个团队。

s15 Agent Teams → 一个 Agent 不够，组队吧。持久队友 + 异步收件箱。

<details>
<summary>深入 CC 源码</summary>

> 以下基于 CC 源码 `CronCreateTool.ts`、`cronScheduler.ts`、`cron.ts`、`cronTasks.ts`、`cronTasksLock.ts`、`useScheduledTasks.ts`（139 行）的完整分析。

### 一、三个 Cron 工具

CC 暴露了三个 cron 工具给模型：`CronCreate`、`CronDelete`、`CronList`。全部由编译时门控 `feature('AGENT_TRIGGERS')` 和运行时 GrowthBook 标志 `tengu_kairos_cron` 控制。还有一个 `CLAUDE_CODE_DISABLE_CRON` 环境变量做本地覆盖。

### 二、存储：`.claude/scheduled_tasks.json`

```json
{ "tasks": [{ "id": "abc12345", "cron": "0 9 * * *", "prompt": "...", "recurring": true, "durable": true, "createdAt": 1714567890000 }] }
```

Durable 任务写磁盘；session-only 任务存于 `STATE.sessionCronTasks` 内存数组（进程重启丢失）。还有一个 `.scheduled_tasks.lock` 文件防止同项目的多个 session 重复触发。

### 三、调度器：1 秒轮询

`cronScheduler.ts` 每秒检查一次（`CHECK_INTERVAL_MS = 1000`）。谁持有锁谁触发文件任务；所有 session 都触发仅 session 任务。还有一个 `chokidar` 文件观察者监视 `scheduled_tasks.json` 变更。

### 四、Cron 表达式：标准 5 字段

分钟 小时 日 月 星期。支持 `*`、`*/N`、`N`、`N-M`、`N-M/S`、`N,M,...`。不支持 `L`、`W`、`?`。所有时间以本地时区解释。Day-of-month 和 day-of-week 同时约束时用 OR 语义。

### 五、抖动（防惊群效应）

- 重复性任务：触发延迟最多可达期间的 10%（上限 15 分钟），基于任务 ID 的确定性哈希
- 一次性任务：当触发时间落在 `:00` 或 `:30` 时，最多提前 90 秒触发
- 抖动配置可通过 GrowthBook 实时调整，60 秒刷新一次

### 六、自动过期

重复性任务 7 天后自动过期（可配置，上限 30 天）。过期前最后一次触发，触发后自动删除。

### 七、作业数上限

`MAX_JOBS = 50`（`CronCreateTool.ts:25`）。超限时返回错误："Too many scheduled jobs (max 50). Cancel one first."

### 八、触发注入

触发后通过 `enqueuePendingNotification()` 以 `priority: 'later'` 入队命令队列。标记 `workload: WORKLOAD_CRON`，API 在容量紧张时以更低的 QoS 为 cron 发起的请求服务。

### 九、Queue Processor：自动交付

真实 CC 通过 `useQueueProcessor.ts:48-60` 在无 query、无阻塞 UI、队列非空时自动触发处理。`queueProcessor.ts:52-87` 按队列优先级把命令交给 `handlePromptSubmit()`。教学版用 `queue_processor_loop` 保留核心行为：队列有任务且 Agent 空闲时，自动启动一轮 agent_loop。

</details>

<!-- translation-sync: zh@v1, en@v1, ja@v1 -->
