# s18: Worktree Isolation — 各干各的，互不干扰

[中文](README.md) · [English](README.en.md) · [日本語](README.ja.md)

s01 → ... → s16 → s17 → `s18` → [s19](../s19_mcp_plugin/) → s20

> *"各干各的目录, 互不干扰"* — 任务管目标, worktree 管目录, 按 ID 绑定。
>
> **Harness 层**: 隔离 — 并行执行的目录隔离。

---

## 问题

s17 中，Alice 和 Bob 都在同一个目录下工作。Alice 的任务是"重构认证模块"，Bob 的任务是"重构 UI 登录页"。

Alice `write_file("config.py", ...)`。Bob 也 `write_file("config.py", ...)`。两个人改同一个文件，互相覆盖。而且无法干净地回滚——分不清哪些改动是谁的。

s15-s17 解决了"谁干什么"（任务系统）和"怎么通信"（消息总线），但没解决"在哪干"。

---

## 解决方案

![Worktree Overview](images/worktree-overview.svg)

Git worktree 让你在同一仓库中创建多个独立的工作目录，每个有自己的分支。Alice 在 `.worktrees/auth-refactor/` 下工作，Bob 在 `.worktrees/ui-login/` 下工作——互不干扰。

沿用 S17 的教学版 MessageBus、协议和自治认领机制。本章新增：

| 能力 | 作用 |
|------|------|
| create_worktree | 为任务创建独立目录 + 独立分支 |
| bind_task_to_worktree | 把任务和工作目录绑定（不改状态） |
| remove_worktree / keep_worktree | 完成后清理或保留 |
| validate_worktree_name | 拒绝路径穿越和非法字符 |

---

## 补充：先搞懂 git worktree 本身

在讲 s18 之前，先弄清楚 `git worktree` 是什么——它是 **git 的原生功能**（子命令），不是 Claude/Agent 框架发明的。s18 只是把它包装成 Agent 的工具。

### 它解决什么问题

普通 git 仓库：一个仓库目录 = 一份工作区 = 一个当前分支。想同时干两件事（比如同时改 `auth` 和 `ui`），要么来回 `git checkout` 切换，要么 clone 两份。

`git worktree` 允许**同一个仓库有多个工作目录，每个目录各自检出一个不同的分支**。文件层面物理隔离，但共享同一个 `.git` 对象库（提交历史、对象都共用）：

```
主仓库目录 /         → 检出 main 分支
├── .git/           → 对象库（共享，只有一个）
├── .worktrees/auth/ → 检出 wt/auth 分支（独立工作区）
└── .worktrees/ui/   → 检出 wt/ui 分支（独立工作区）
```

### 它和普通分支/目录的区别

| | 普通分支（branch） | git worktree |
|---|---|---|
| 工作目录 | 只有一个，切分支就换内容 | 每个 worktree 一个独立目录，同时存在 |
| 隔离程度 | 逻辑隔离（同一目录内容切换） | **物理隔离**（不同目录，文件不互相覆盖） |
| 并行 | 不能同时改两个分支（要切来切去） | 可以同时改多个分支 |
| 对象库 | 共享 | 共享（`.git` 只有一个） |

### 常用命令

```sh
git worktree add <路径> -b <新分支名> HEAD   # 创建：新目录 + 新分支
git worktree remove <路径> --force           # 删除工作目录
git branch -D <分支名>                        # 删除分支
git worktree list                            # 列出所有 worktree
```

### s18 里怎么用它

s18 的代码就是直接调 git 子命令，没有另起炉灶：

```python
# 创建：在 .worktrees/{name} 建目录，基于 HEAD 开新分支 wt/{name}
run_git(["worktree", "add", str(path), "-b", f"wt/{name}", "HEAD"])

# 删除：删工作目录
run_git(["worktree", "remove", str(path), "--force"])
# 删分支
run_git(["branch", "-D", f"wt/{name}"])

# 查询改动：stat 未提交文件 / log 未推送 commit
run_git(["status", "--porcelain"])
run_git(["log", "@{push}..HEAD", "--oneline"])
```

**s18 做的事，本质就是把这些 git 原生命令封装成 Agent 的工具**——让 Lead/队友的 LLM 能通过 function calling 调 `create_worktree` / `remove_worktree` / `keep_worktree`，而不是人手动敲 git 命令。

### 关键点：git 负责"隔离"，s18 负责"编排"

| 层面 | 谁做的 | 干什么 |
|------|--------|--------|
| **隔离** | **git worktree**（原生命令） | 创建独立目录+分支、物理隔离文件、共享对象库 |
| **编排** | s18 harness 代码 | 把 worktree 绑定到任务（`worktree` 字段）、让队友 cwd 自动切换、删除前安全检查、事件审计 |

也就是说：**git worktree 解决"目录怎么隔离"，s18 解决"哪个任务用哪个 worktree、队友怎么自动进对目录、干完怎么清理"**。前者是底层机制，后者是 Agent 层把这机制变成"工具 + 任务绑定 + 生命周期管理"。

### worktree 实际开发中用得多吗

**对普通开发者：用得确实比较少。** 大多数人一个仓库一个目录，用 `git switch` 来回切分支就够了。原因：

1. 开发思维大多是**单线**的——一次专注一个任务，做完 merge 再开下一个，不需要同时开两个目录
2. 切换成本多数场景可接受——`git switch` + 重编译/重加载，不觉得痛
3. **不熟悉**——很多人不知道有这功能（包括你之前），自然不会用
4. 团队协作上它没直接帮助——它解决的是"个人同时多任务"，不是"多人协作"

**但"用得少"≠"没用"，它解决的场景是刚需：**

| 场景 | 不用 worktree 的痛点 | worktree 的价值 |
|------|---------------------|----------------|
| 同时改两个分支（A 开发新功能、B 修线上 bug） | 切来切去，编译缓存/IDE 状态全乱，容易带错代码 | 两个目录各干各的，互不打扰 |
| 想对比分支运行效果 | 只能切过去看，切回来再切回去 | 两个目录同时跑，直接对比 |
| 任务周期长、要保留现场 | 切走再切回，IDE 打开的文件/终端状态丢了 | 每个 worktree 是独立现场 |
| CI/部署脚本需要干净检出 | 主目录一堆未提交改动 | 独立 worktree 不污染主目录 |

真实开发里，**前端/全栈工程师（经常并行多任务）比后端用得多**，有工作流洁癖的人也爱用。不是大多数人的日常，但用起来的人普遍觉得香。

**★ 但在这个项目的 Agent 领域，它是核心——这正是 s18 的主题。** 普通开发者少用，Agent 场景却几乎必须：

- 多个 Agent **同时工作**，必须物理隔离，否则 Alice 和 Bob 会互相覆盖文件（s18 开头的问题就是 `config.py` 被两个人改）
- Agent 不能像人一样"记得切回来再继续"，它需要**每个任务一个干净的独立工作目录**
- Claude Code 里 `EnterWorktree`（会话切入工作树）和 `AgentTool isolation`（子 agent 隔离）都是核心功能

**一句话：worktree 对"单线程的人类开发者"是可选优化，对"多线程的 Agent 并行"是刚需。** 你学它不为日常多用，而是理解"怎么让多个执行体互不干扰"——这个思想比 worktree 命令本身更值钱（也是 s19/s20 隔离概念的基础）。

### 一句话类比

> git worktree ≈ 给同一个代码库开**多张独立工作台**，各有各的桌子（目录）、各有各的分支，但共用同一个资料库（`.git`）。git worktree ≈ 给每个开发者在同一个代码库开了独立的**工作台**；s18 ≈ 给每个任务**自动分配一张桌子**，告诉 Agent"你的活在这张桌子上干"，干完检查桌上有没有落下的东西（未提交改动）再决定收不收拾。

---

## 工作原理

### 创建：任务-Worktree 绑定

```python
def create_worktree(name: str, task_id: str = "") -> str:
    validate_worktree_name(name)       # 只允许 [A-Za-z0-9._-]{1,64}
    path = WORKTREES_DIR / name
    ok, result = run_git(["worktree", "add", str(path), "-b", f"wt/{name}", "HEAD"])
    if not ok:
        return f"Git error: {result}"
    if task_id:
        bind_task_to_worktree(task_id, name)
    log_event("create", name, task_id)
    return f"Worktree '{name}' created at {path}"

def bind_task_to_worktree(task_id: str, worktree_name: str):
    task = load_task(task_id)
    task.worktree = worktree_name       # 只写 worktree 字段
    save_task(task)                     # 状态保持 pending，等队友 claim
```

绑定规则：一个任务绑定一个 worktree。绑定不改任务状态——任务仍是 `pending`，队友自动认领时才推进到 `in_progress`。这样 Lead 可以提前创建任务和 worktree，队友 idle 时自然认领带 worktree 的任务。

### 队友工具的 cwd 切换

教学版给每个队友维护一个 `wt_ctx` 字典，记录当前 worktree 路径。队友认领带 worktree 的任务时，`wt_ctx` 自动设置为 worktree 路径；队友的 `bash`、`read_file`、`write_file` 在 worktree 目录下执行：

```python
# 队友线程内部
wt_ctx = {"path": None}

def _run_claim_task(task_id):
    result = claim_task(task_id, owner=name)
    if "Claimed" in result:
        task = load_task(task_id)
        if task.worktree:
            wt_ctx["path"] = str(WORKTREES_DIR / task.worktree)
    return result

def _run_bash(command):
    return run_bash(command, cwd=wt_ctx["path"])  # 在 worktree 下执行
```

这是教学简化。真实 CC 的 EnterWorktree 用 `process.chdir()` 切换整个进程目录，AgentTool isolation 用 `cwdOverride` 包住子 agent 执行。

### 收尾：Keep 还是 Remove

任务完成后，两个选择：

```python
def remove_worktree(name: str, discard_changes: bool = False) -> str:
    # 安全检查：有改动时默认拒绝
    if not discard_changes:
        files, commits = _count_worktree_changes(path)
        if files > 0 or commits > 0:
            return "有未提交改动，使用 discard_changes=true 强制删除，或 keep_worktree 保留"
    ok, _ = run_git(["worktree", "remove", str(path), "--force"])
    if not ok:
        return "删除失败"
    run_git(["branch", "-D", f"wt/{name}"])
    log_event("remove", name)

def keep_worktree(name: str) -> str:
    log_event("keep", name)
    return f"Worktree '{name}' kept for review (branch: wt/{name})"
```

Keep = 留着分支，等人工 review 后合并到主分支。Remove = 有改动时默认拒绝，需要 `discard_changes=true` 确认。不自动 complete task——任务完成由队友的 `complete_task` 显式触发。

### 事件流：可审计

每次生命周期操作写入日志，方便排查：

```python
def log_event(event_type: str, worktree_name: str, task_id: str = ""):
    event = {"type": event_type, "worktree": worktree_name,
             "task_id": task_id, "ts": time.time()}
    # append to .worktrees/events.jsonl
```

事件类型：`create`（创建）、`remove`（删除）、`keep`（保留）。教学版只记录事件用于人工排查；完整恢复还需要 index 或 `git worktree list` 扫描。

### run_git：返回成功/失败

```python
def run_git(args: list[str]) -> tuple[bool, str]:
    r = subprocess.run(["git"] + args, cwd=WORKDIR, ...)
    return r.returncode == 0, output
```

`create_worktree` 和 `remove_worktree` 只在 git 命令成功后才写事件日志，保证日志反映真实状态。

---

## 相对 s17 的变更

| 组件 | 之前 (s17) | 之后 (s18) |
|------|-----------|-----------|
| 工作目录 | 所有 Agent 共享 WORKDIR | 每个任务可绑定独立 git worktree |
| Task 数据 | id/subject/status/owner/blockedBy | + worktree 字段 |
| 队友工具 cwd | 始终 WORKDIR | 认领带 worktree 的任务时自动切换 |
| 新函数 | — | create_worktree, bind_task_to_worktree, remove_worktree, keep_worktree, validate_worktree_name |
| worktree 安全 | 无 | name 校验 + 有改动时拒绝删除 |
| 事件日志 | 无 | events.jsonl 生命周期审计 |
| Lead 工具 | 14 (s17) | + create_worktree, remove_worktree, keep_worktree (17) |
| 队友工具 | 8 (s17) | 8（bash/read/write 在 worktree cwd 执行） |

---

## 试一下

```sh
cd learn-claude-code
python s18_worktree_isolation/demo_code.py
```

试试这个 prompt：

`Create two tasks, then create worktrees for each (bind with task_id). Spawn alice and bob. Watch them auto-claim and work in isolated directories.`

观察重点：两个 worktree 的 `git status` 输出是否显示不同的分支？队友认领带 worktree 的任务后，bash 命令是否在 worktree 目录下执行？`remove_worktree` 对有改动的 worktree 是否拒绝？`.tasks/` 中的任务在绑定后状态是否仍为 `pending`？

---

## 接下来

Agent 团队能在隔离的工作空间中自组织了。但 Agent 的能力受限于我们给它写的工具——bash、read、write、task...

如果用户已经有了自己的工具怎么办？比如一个公司内部的 Jira API、一个自建的部署系统？

s19 MCP Plugin → 给 Agent 装一个插件系统。外部工具通过标准协议接入，Agent 不需要知道它们是谁写的。

<details>
<summary>深入 CC 源码</summary>

CC 的 worktree 系统有两条路径：**EnterWorktree**（当前会话切入）和 **AgentTool isolation**（子 agent 隔离）。

### EnterWorktree：当前会话切换

`EnterWorktreeTool.ts:92-97` 创建 worktree 后立即 `process.chdir(worktreePath)`、`setCwd()`、`setOriginalCwd()`、`saveWorktreeState()`。当前会话的工作目录直接切换到 worktree——不是 prompt 提醒，而是进程级目录变更。

`ExitWorktreeTool.ts:261-320` 的 keep/remove 都会 `restoreSessionToOriginalCwd()` 恢复原目录。Remove 时检查未提交改动（`ExitWorktreeTool.ts:190-220`），没有 `discard_changes: true` 就拒绝删除。

### AgentTool isolation：子 agent 隔离

`AgentTool.tsx:590-641` 在 `isolation: "worktree"` 时调用 `createAgentWorktree()` 创建 worktree，用 `cwdOverridePath` 包住子 agent 执行。子 agent 的所有操作自动在 worktree 目录下进行。`AgentTool/prompt.ts:272` 告诉模型：这是临时 worktree，无改动自动清理，有改动返回路径和分支。

`worktree.ts:902-951` 的 `createAgentWorktree()` 不修改全局 session cwd，只给子 agent 用。`worktree.ts:961-1020` 的 `removeAgentWorktree()` 从主 repo root 删除。

### name 校验

`worktree.ts:76-84` 校验 slug：拒绝 `.`/`..`，允许 `[a-zA-Z0-9._-]`。`worktree.ts:48` 定义 `VALID_WORKTREE_SLUG_SEGMENT`。教学版的 `validate_worktree_name` 用同样的规则。

### 路径和分支命名

真实路径是 `.claude/worktrees/`，分支名 `worktree-{slug}`（`worktree.ts:204-227`，斜杠用 `+` 替代）。教学版用 `.worktrees/` 和 `wt/{name}` 简化。

创建时用 `git worktree add -B`（`worktree.ts:326-328`），优先基于 `origin/<defaultBranch>` 而非当前 HEAD。

### 状态管理

CC 没有 task-worktree 绑定。Worktree 状态通过 `PersistedWorktreeSession`（`worktree.ts:756-768`）管理，字段包括 `originalCwd`、`worktreePath`、`worktreeName`、`worktreeBranch`、`originalBranch`、`originalHeadCommit`、`sessionId` 等——没有 taskId。`saveWorktreeState()`（`sessionStorage.ts:2883-2920`）以 `type: 'worktree-state'` 写入 session transcript。

教学版用 task 的 `worktree` 字段做绑定，是教学简化。CC 把 worktree 和 task 作为两个独立系统，通过 Agent 理解上下文来关联。

</details>

<!-- translation-sync: zh@v1, en@v0, ja@v0 -->
