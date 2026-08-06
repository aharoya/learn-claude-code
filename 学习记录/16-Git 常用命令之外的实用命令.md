# Git 进阶：pull/push/clone/checkout 之外的常用命令

> 你平时只用 `git pull` / `git push` / `git clone` / `git checkout`。本文补齐日常开发真正会用到的 git 命令：**分支管理、合并（merge）、worktree 隔离、回退/撤销、暂存 stash、查看历史**。以 s18 用到的 git worktree 为主线，配合命令行演示，学完可以放心在真实项目里操作。

---

## 0. 先建立心智模型：git 的三块区域

所有 git 命令都可以归到"三块区域"的流转里：

```
工作区（你看到的文件）──git add──→ 暂存区（准备提交）──git commit──→ 版本库（历史快照）
```

| 命令 | 作用 | 移动 |
|------|------|------|
| `git add <文件>` | 把改动放入暂存区 | 工作区 → 暂存区 |
| `git commit -m "..."` | 把暂存区固化成一个历史版本 | 暂存区 → 版本库 |
| `git status` | 看现在哪些文件改了、哪些已暂存 | 查看 |
| `git diff` | 看改动内容（未暂存的差异） | 查看 |

你常用的 `pull/push/clone` 是**版本库和远程**之间的同步；`checkout` 是切换分支/恢复文件。理解三块区域后，下面每个命令都是"把东西在某个方向挪一挪"。

---

## 1. 你已会的命令（一句话对照）

| 命令 | 一句话 | 属于 |
|------|--------|------|
| `git clone <url>` | 第一次把远程仓库复制到本地 | 本地↔远程 |
| `git pull` | 拉取远程最新并合并到当前分支（= fetch + merge） | 本地↔远程 |
| `git push` | 把本地提交推送到远程 | 本地↔远程 |
| `git checkout <分支>` | 切换分支；或 `git checkout -- <文件>` 丢弃工作区改动 | 切换/恢复 |

> 后面你会发现：**`git checkout` 是个"万能工具"，功能太多容易混**。现代 git 推荐用 `git switch`（切分支）和 `git restore`（恢复文件）替代它，职责单一更清晰。

---

## 2. 分支管理：git branch / git switch

### 查看与创建

```sh
git branch                # 列出所有本地分支（* 表示当前所在）
git branch <名字>          # 创建新分支（但不切过去）
git switch <名字>          # 切换到某个分支
git switch -c <名字>       # 创建并切换（等价于旧写法 git checkout -b <名字>）
git branch -d <名字>       # 删除分支（-d 安全，-D 强制删）
```

### 为什么需要分支

分支 = 一条独立的开发线。你在分支 A 改代码，不影响分支 B。等 A 做完，再把 A **合并**回主线（见第 3 节）。

```
main ──●──●──────────●────────（合并回来的点）
           └── feature ──●──●──┘
               （独立开发线，互不干扰）
```

### ★ 一个新手最容易犯的错

```sh
git switch feature   # 切到 feature 分支
# 改了一堆代码……忘了切回来
git switch main      # ← 报错：有未提交的改动，切换失败！
```

git 会阻止你带着未提交改动切分支（怕丢）。三种处理：
1. `git add` + `git commit`（真的想留在这个分支）
2. `git stash`（暂时存起来，见第 6 节）
3. `git restore <文件>`（不想要了，丢弃）

---

## 3. 合并：git merge（重点，你没用过）

### 为什么需要合并

两条开发线做完，要把成果汇到一条线上：

```
main ──●──●──────────────●（merge 后）
           \            /
feature ───●──●──●─────
```

### 基本用法

```sh
git switch main            # 先切到要"接收合并"的分支
git merge feature          # 把 feature 合并进 main
```

### 三种合并结果（理解这个就懂 merge）

| 情况 | 结果 | 说明 |
|------|------|------|
| main 没有新提交 | **Fast-forward**（快进） | main 直接指向 feature 的最新提交，无合并提交 |
| 两边都改了**不同**文件 | **普通合并**，自动完成 | git 自动生成一个 merge commit |
| 两边都改了**同一个文件同一处** | **冲突（conflict）** | 需要手动解决 |

### 冲突怎么办（最让新手头疼的）

```sh
git merge feature
# 输出：CONFLICT (content): Merge conflict in config.py
```

git 会把冲突标记写进文件：

```
<<<<<<< HEAD
version = "1.0"          # 这是 main 分支的版本
=======
version = "2.0"          # 这是 feature 的版本
>>>>>>> feature
```

**解决步骤**：
1. 打开文件，手动决定保留哪个（或都保留），删掉 `<<<<<<<` / `=======` / `>>>>>>>` 三行标记
2. `git add config.py`（告诉 git"我解决了"）
3. `git commit`（完成合并提交）

> ⚠️ **冲突不是错误**，是正常现象。并行开发同一文件几乎必然冲突。关键是冷静看两个版本、决定谁对。

### 冲突处理详解（命令行 vs IDEA 图形化）

**核心事实**：IDEA 的图形对比器和命令行处理的是同一件事——**冲突标记（conflict markers）**。IDEA 把它可视化了，命令行要自己动手编辑文本。

#### ① 冲突发生后，`git status` 显示什么

```sh
$ git status
Unmerged paths:
  (use "git add <file>..." to mark resolution)
        both modified:   config.py   ← 冲突文件
```

关键词 **both modified**（双方都改了）——git 没有自动合并，等着你决定。文件里被插入冲突标记：

```python
<<<<<<< HEAD
version = "1.0"            # 你的分支（当前所在分支）的版本
=======
version = "2.0"            # 被合并分支（feature）的版本
>>>>>>> feature
```

三段式：`<<<<<<<` 到 `=======` 是**你的**版本，`=======` 到 `>>>>>>>` 是**对方的**版本。

#### ② 命令行纯手动流程（基本功，必须会）

```sh
# 1. 打开冲突文件，手动编辑：保留想要的，删掉三行标记
#    假设决定用 2.0：
version = "2.0"            # ← 只留这一行

# 2. 告诉 git "这个文件解决了"
git add config.py

# 3. 全部冲突解决后，提交完成合并
git commit
```

**五步口诀**：看 status → 改文件 → 删标记 → `git add` → `git commit`。

辅助命令：

```sh
git status                 # 看还剩哪些冲突文件
git diff                   # 看冲突文件当前内容
git merge --abort          # 反悔了，放弃整个 merge，回到合并前
```

#### ③ 高效法：`git mergetool` 调用图形对比器

手动编辑三行标记在冲突多时很痛苦。git 提供 `git mergetool`——**自动把你配置好的图形工具逐个打开**，逐个解决冲突文件：

```sh
git mergetool              # 有冲突时，它打开配置的 merge tool
```

默认可能用 vimdiff（不好用）。**装了 IDEA，直接把它配成 IDEA 的三路对比器**：

```sh
git config --global merge.tool idea
git config --global mergetool.idea.cmd 'idea merge "$LOCAL" "$REMOTE" "$BASE" "$MERGED"'
```

之后冲突时 `git mergetool` 就会弹出 IDEA 的三栏对比器。

#### ④ 结合 IDEA 的实际推荐工作流（最贴合日常）

IDEA 其实**不需要** mergetool 配置——项目开着的时候它自己就能发现冲突。推荐流程：

```
1. 命令行:  git merge feature        ← 发起合并
2. 命令行:  git status               ← 看到哪些文件冲突
3. IDEA:    项目树里冲突文件是红色标红
4. IDEA:    双击文件 → 弹出 "Resolve Conflicts" 三栏对比器
            左=你的版本 | 中间=合并结果 | 右=对方版本
            点箭头选边 / 手动编辑中间栏
5. IDEA:    点 "Apply" / "Merge" → 文件标记为已解决
6. 命令行:  git commit               ← 完成合并
```

IDEA 的三栏对比器就是冲突标记的**可视化版**：

```
[你的版本 config.py] [合并结果 config.py] [对方版本 config.py]
   version = "1.0"      version = "2.0"     version = "2.0"
                              ↑
                    点这里直接选右边的 2.0
```

#### ⑤ 核心概念：ours / theirs 是谁

merge 里两个方向要分清（mergetool、命令行选项里都会遇到）：

| 术语 | 是谁 |
|------|------|
| **ours**（我们的） | 当前所在分支（`HEAD`，你 `git switch` 过去那个） |
| **theirs**（他们的） | 正在被合并进来的分支（merge 的目标） |

```sh
# 如果某文件冲突了，想"无脑用对方的版本"：
git checkout --theirs config.py && git add config.py
# 想"无脑用自己这版"：
git checkout --ours config.py && git add config.py
```

⚠️ 注意方向反直觉：merge 时 **theirs = 被合并进来的分支**，rebase 时恰好相反（所以初学者只用 merge 也是原因之一）。

#### ⑥ 实战建议

1. **冲突通常只发生在"双方改了同一文件同一区域"**——不同文件的改动 git 自动合并，你根本看不到冲突
2. **先看全局再动手**：`git status` 列出所有冲突文件 → 逐个解决，别在单个文件里纠结
3. **解决冲突 = 做决定，不是"两边都要"**——要理解两边改动的意图，保留正确逻辑，而不是机械地把两段都堆上去
4. **不确定哪个对，先问**（比如用 `git log` 了解两边提交意图），或去 IDE 里可视化对比更清楚

> 一句话：**命令行是"手动删标记"的保底基本功，IDEA 是"可视化选边"的高效工具，两者操作的是同一个东西**——没有 IDE 的环境（比如服务器上）用命令行，本地开发用 IDEA 更舒服。

### merge 与 rebase 的区别（进阶，可先了解）

| | merge | rebase |
|---|---|---|
| 结果 | 产生 merge commit，历史有分叉 | 把分支"重放"到主线，历史线性 |
| 历史 | 真实记录"什么时候合过" | 更整洁，像一条直线 |
| 危险 | 无 | **不要 rebase 已经 push 的公共分支**，会重写历史 |

```sh
# rebase 用法：把 feature 的提交"搬到" main 的最新提交之上
git switch feature
git rebase main
```

> 初学者建议**只用 merge**，rebase 等理解了历史模型再碰。

---

## 4. git worktree：多工作目录（s18 的核心，你没用过）

### 是什么

前面说过：普通仓库一个目录 = 一个工作区 = 一个分支。想同时改两个分支，要么切来切去（麻烦、容易丢改动），要么 clone 两份（占空间、不同步）。

`git worktree` 让你**在同一个仓库里开多个独立工作目录**，每个目录检出不同分支，共享同一个 `.git`：

```
主目录 /          → main 分支
├── .git/        → 共享对象库（只有一个）
└── .worktrees/ui/ → wt/ui 分支（独立工作区）
```

### 基本命令

```sh
# 创建：在 <路径> 建目录，基于 HEAD 开新分支
git worktree add <路径> -b <新分支名> HEAD

# 创建完可以直接进那个目录干活
cd .worktrees/ui
git switch wt/ui    # 已经是这个分支，直接改代码

# 列出所有 worktree
git worktree list

# 删除工作目录
git worktree remove <路径> --force
# 再删掉对应分支
git branch -D wt/ui
```

### s18 里怎么用的（真实代码）

```python
# 创建：.worktrees/auth 目录 + wt/auth 分支，基于当前 HEAD
run_git(["worktree", "add", str(path), "-b", f"wt/{name}", "HEAD"])

# 查询改动：未提交文件数 / 未推送 commit 数（删除前安全检查）
run_git(["status", "--porcelain"])
run_git(["log", "@{push}..HEAD", "--oneline"])

# 删除
run_git(["worktree", "remove", str(path), "--force"])
run_git(["branch", "-D", f"wt/{name}"])
```

### ★ 使用场景（什么时候用 worktree）

1. **并行开发互不干扰**：Alice 改 auth、Bob 改 ui，各占一个 worktree，谁也碰不到谁的文件（正是 s18 解决的多 Agent 冲突问题）
2. **同时看两个分支**：一个目录跑 main 稳定版，一个目录跑 feature 新功能
3. **独立跑任务**：每个任务一个 worktree，做完删掉，主目录保持干净

### 和分支的关系

worktree 是**目录层面**的隔离，分支是**提交线**的隔离。一个 worktree 一定对应一个（正在检出的）分支。你可以：
- 一个 worktree 就一个分支（`wt/ui`），干完 merge 回 main，再删 worktree

---

## 5. 回退与撤销：restore / reset / revert

这是"犯错救星"三件套，按后悔程度排列：

| 命令 | 后悔程度 | 作用 |
|------|---------|------|
| `git restore <文件>` | 最小 | 丢弃**工作区**未暂存的改动（回到暂存区/上次提交的样子） |
| `git restore --staged <文件>` | 小 | 把已暂存的文件移出暂存区（不丢改动） |
| `git reset --soft HEAD~1` | 中 | 撤销最近一次 commit，但保留改动在暂存区 |
| `git reset HEAD~1` | 中大 | 撤销 commit 并保留改动在工作区（默认） |
| `git reset --hard HEAD~1` | 大 | **彻底丢弃**最近一次提交的改动（⚠️ 无法找回） |
| `git revert <commit>` | 安全 | 用新提交"反向撤销"某个历史提交（不重写历史） |

```sh
# 示例：刚 commit 了，发现提交错了，想改但不想丢内容
git reset HEAD~1        # 撤销提交，改动回到工作区
# 现在修改文件，再重新 add + commit

# 示例：彻底不要刚才的提交了
git reset --hard HEAD~1  # ⚠️ 改动永久消失，慎用
```

### ★ 什么时候用 revert 而不是 reset

如果那个 commit **已经 push 到远程**，别用 reset（会重写历史、队友拉取会冲突），用 `git revert`：

```sh
git revert <commit号>    # 生成一个"撤销那个提交"的新提交，push 后大家都安全
```

---

## 6. 暂存：git stash（换分支前临时存东西）

"工作干到一半，老板让你切去修个紧急 bug"——这时改动不想提交又不想丢，就 `stash`：

```sh
git stash          # 把当前未提交改动存起来，工作区变干净
git switch main    # 放心切分支去修 bug
# 修完回来……
git switch feature
git stash pop      # 把存的改动取回来
```

常用变体：
```sh
git stash list         # 看存了几份
git stash pop          # 取回最近一份并删除
git stash apply        # 取回但不删除（还想保留）
git stash drop         # 丢弃某一份
```

> 本质：stash 是"临时保险箱"，专治"想切分支但改动还没做完"。

---

## 7. fetch 与 pull 的区别

`git pull` = `git fetch` + `git merge`（两步合一）。

```sh
git fetch          # 只把远程的提交下载到本地，但不合并（安全，不影响工作区）
git pull           # 下载 + 自动合并到当前分支
```

什么时候用 fetch：想先看看远程有什么新东西、但还不确定要不要合并：

```sh
git fetch
git log origin/main   # 查看远程分支最新提交，不碰本地
```

---

## 8. 查看历史：git log / git diff

```sh
git log                # 提交历史（最近的在上）
git log --oneline      # 每行一个提交，简洁
git log --oneline --graph   # 用图形看分支合并结构（强烈推荐！）
git diff               # 工作区 vs 暂存区（没 add 的改动）
git diff --staged      # 暂存区 vs 上次提交（已 add 的改动）
git diff <文件>        # 只看某个文件的改动
```

`--graph` 能把第 3 节的合并图真实显示出来，是理解 merge 的最佳可视化工具：

```
* 8f3a2d1 Merge branch 'feature'
|\
| * c9b44e2 add ui feature
| * 5a1f0d3 fix button
* | b2e9c88 fix main bug
|/
* 7d0c1a2 initial commit
```

---

## 9. 完整实战串联（一个典型工作流）

### 场景：开发一个功能，做完合并回主线

```sh
# 1. 拉最新 + 建功能分支
git pull
git switch -c feature-login

# 2. 在功能分支上改代码
git add .                # 暂存所有改动
git commit -m "feat: 登录功能"

# 3. 干到一半要修紧急 bug → stash
git stash
git switch main
git pull
# 修 bug... git add + commit
git switch feature-login
git stash pop            # 取回登录功能的改动

# 4. 功能做完了，合并回 main
git switch main
git pull                 # 先拉最新（避免和远程冲突）
git merge feature-login  # 合并
# 如果有冲突 → 手动解决 → git add → git commit

# 5. 推送到远程 + 清理
git push
git branch -d feature-login   # 本地分支不需要了，删掉
```

### 场景：s18 式多任务隔离（用 worktree）

```sh
# 一个仓库，开两个独立工作目录
git worktree add .worktrees/auth -b wt/auth HEAD
git worktree add .worktrees/ui -b wt/ui HEAD

# 各自在各自目录改、各自 commit
cd .worktrees/auth && git commit -am "auth 模块"
cd .worktrees/ui && git commit -am "ui 模块"

# 完成后合并回主仓库的 main，再删 worktree
git switch main
git merge wt/auth
git worktree remove .worktrees/auth --force
git branch -D wt/auth
```

---

## 10. 命令速查表

| 类别 | 命令 | 作用 |
|------|------|------|
| 状态 | `git status` | 看改动 |
| 查看 | `git diff` / `git log --oneline --graph` | 看差异 / 看历史 |
| 分支 | `git branch` / `git switch -c` | 列出 / 新建并切换 |
| 合并 | `git merge <分支>` | 把分支并入当前 |
| 隔离 | `git worktree add/remove/list` | 多工作目录 |
| 暂存 | `git stash` / `git stash pop` | 临时存 / 取回 |
| 回退 | `git restore` / `git reset` / `git revert` | 撤销改动 |
| 远程 | `git fetch` / `git pull` / `git push` | 本地↔远程 |
| 提交 | `git add` / `git commit` | 暂存 / 固化 |

---

## 11. 安全须知（三条红线）

1. **`git reset --hard` 会永久丢改动**——先用 `git stash` 或确认不要了再用
2. **不要 `git rebase` 已 push 的公共分支**——会重写历史，队友拉取会炸
3. **`git push --force` 同理慎用**——覆盖远程历史，除非你清楚后果（本项目里从未用过）

> 一句话总结：git 命令都是在**三块区域 + 远程**之间搬东西。你已会的 pull/push/clone 是"本地↔远程"的搬运工；本文的分支、merge、worktree、stash、restore 是"本地内部"的搬运工。记住每类命令搬什么，就不会慌。

---

## 关联阅读

- `学习记录/15-Agent 工程五层递进全景.md` — 第 ④/⑤ 层：git 命令作为 Agent 工具的底层
- `s18_worktree_isolation/demo_code.py` — worktree 的完整封装（run_git / create_worktree / remove_worktree）
- `s18_worktree_isolation/README.md` — git worktree 基础 + s18 如何编排

---

**文档生成时间：** 2026-08-06
