# Git 完整实战演练：从项目创建到推送 GitHub

> 一个可以**从头到尾照着敲**的完整演练。用独立示例项目（`C:\git-demo`，不碰现有仓库）走完 git 全流程：**初始化 → 分支开发 → 制造并解决冲突 → 合并 → 推送 GitHub**。前 4 阶段纯本地跑通，第 5 阶段开始连 GitHub。做完你会掌握"个人开发"的全部常用 git 操作。

---

## 阶段 0：准备（一次性）

```sh
# 检查 git 装没装
git --version

# 配置身份（全局只配一次，以后所有仓库都用）
git config --global user.name "aharoya"
git config --global user.email "你的邮箱@example.com"
```

> Windows 上 git 首次 commit 会问"你的名字/邮箱"，不配会报错，所以先配好。

---

## 阶段 1：创建本地项目

```sh
# 1. 建一个空目录作为演练场（★ 别在你现有仓库里做！）
mkdir C:\git-demo
cd C:\git-demo

# 2. 初始化仓库
git init
# 输出: Initialized empty Git repository in C:/git-demo/.git/

# 3. 确认当前分支名（新版 git 默认 main）
git branch
# 若显示 master，改成 main：
git branch -m main

# 4. 创建第一个文件
# （用记事本或编辑器建 app.py，内容：）
#   print("app version 1.0")

# 5. 第一次提交
git add app.py          # 工作区 → 暂存区
git status              # 看暂存了什么（绿色 = 已暂存）
git commit -m "feat: 初始版本 v1.0"
```

**关键理解**：`git init` 后仓库是空的；`git add` 让 git 关注文件；`git commit` 生成第一个历史快照。

> 💡 IDEA 用户：可以不用命令行建文件——IDEA 里 New Project 后点右下角分支图标 "Enable Version Control" 选 Git，效果等同 `git init`。之后 commit 用 `Ctrl+K`（提交面板）。

---

## 阶段 0.5：拉代码前，先处理好本地改动（防丢失）

**问题**：`git pull` 之前如果本地有未提交改动，可能遇到两种情况——这正是你以前踩过的"拉完代码本地改动不见了"。

### 先搞清 git pull 对未提交改动的真实行为

```sh
# 你有未提交改动，直接 git pull
git pull
```

| 情况 | git 的行为 |
|------|-----------|
| 远程改的文件 ≠ 你本地改的文件 | **静默成功**：远程改动合进来，你的未提交改动**原样保留** |
| 远程改的文件 = 你本地改的文件（同一文件） | **明确报错拒绝**：`Your local changes ... would be overwritten by merge. Please commit your changes or stash them` |

**关键结论**：git pull **不会无声无息覆盖你的未提交改动**——它要么保留、要么报错让你先 commit/stash。merge 冲突也**一定会**在终端打出 `CONFLICT` 并让你解决。

### 推荐流程（二选一）

```sh
# 方式 A：改动已经能提交了 → 直接 commit
git add . && git commit -m "wip: 保存当前进度"
git pull

# 方式 B：改动还没做完、不想提交 → stash（推荐）
git stash          # 存起来，工作区变干净
git pull           # 放心拉
git stash pop      # 取回你的改动
```

> stash 就是为"不 commit 又想拉代码"设计的。三步走，你的未提交改动一个不丢。

### 更稳妥的检查（三句话）

```sh
git status          # ① 先看本地有没有未提交改动
# 有改动 → git stash  或  git commit
git pull            # ② 干净了再拉
git stash pop       # ③ 如果用了 stash，取回来
```

养成"**拉之前看一眼 status**"的习惯，基本就不会再遇到"代码没了"的惊吓。

### ⚠️ 如果真遇到"没提示、本地代码没了"——先排查这些

标准 `git pull` 不会无声覆盖未提交改动，如果发生多半是下面几种（回想当时敲了什么）：

| 可疑操作 | 说明 |
|---------|------|
| `git checkout .` 或 `git restore .` | 丢弃工作区所有未提交改动（相当于"撤销所有改动"） |
| `git reset --hard` | 硬重置，丢弃未提交改动 + 退回指定提交（⚠️ 永久） |
| `git switch 分支` 后 `git checkout` 某些文件 | 切换分支时把文件带到了另一分支 |
| **IDEA 的 Update Project** | IDEA 的 pull 有 overwrite 策略选项，某些配置下行为不同 |
| 改的文件没保存 / 编辑器自动刷新 | 文件内容被 IDE 缓存覆盖 |

### 即使 commit 了，pull 也可能提示冲突——那是正常的

commit 之后 pull，如果远程和你本地改了同一文件同一处，git 会打 `CONFLICT` 提示你解决（就是阶段 4 的演练）。**这个提示是好事**——说明 git 在保护你，冲突不是错误。

---

## 阶段 2：创建分支并行开发

```sh
# 1. 建并切到 feature 分支
git switch -c feature-login
# 输出: Switched to a new branch 'feature-login'

# 2. 在 feature 上开发：把 app.py 改成
#   print("app version 2.0 with login")

# 3. 提交（注意当前在 feature 分支）
git add app.py
git commit -m "feat: 加登录功能 v2.0"

# 4. 看历史：main 和 feature 分叉了
git log --oneline --graph --all
# * 1234abc (feature-login) feat: 加登录功能 v2.0
# * 5678def (main) feat: 初始版本 v1.0
```

**理解**：`git switch -c` = 创建分支 + 切换（旧写法 `git checkout -b`）。两个分支现在从同一个 commit 分叉，各自往后走。

---

## 阶段 3：制造冲突（关键！）

现在**切回 main，改同一个文件的同一行**——这样一会儿 merge 必然冲突：

```sh
# 1. 切回 main
git switch main

# 2. 把 app.py 改成（注意：也是 version 行，和 feature 撞车）
#   print("app version 1.1 hotfix")

# 3. 提交
git add app.py
git commit -m "fix: 紧急修复 v1.1"

# 4. 此时两个分支都改了 app.py 的 version 行：
#   main    → "app version 1.1 hotfix"
#   feature → "app version 2.0 with login"
```

**这就是冲突的前提**：两边改了同一文件同一区域。不同文件的改动 git 会自动合并，不会冲突。

---

## 阶段 4：合并 → 冲突爆发 → 解决

```sh
# 1. 在 main 上合并 feature
git merge feature-login
# 输出: CONFLICT (content): Merge conflict in app.py
#       Automatic merge failed; fix conflicts and then commit the result.
```

**打开 app.py，你会看到**：

```python
<<<<<<< HEAD
print("app version 1.1 hotfix")
=======
print("app version 2.0 with login")
>>>>>>> feature-login
```

**解决（命令行方式）**：编辑成你想要的最终版本，比如登录功能 + 保留热修，删掉三行标记：

```python
print("app version 2.0 with login")   # 手动决定最终版本
```

```sh
# 2. 标记已解决 + 完成合并
git add app.py
git commit -m "merge: feature-login 合并，解决 version 冲突"

# 3. 看合并后的历史（★ 用 --graph 看分叉汇合！）
git log --oneline --graph --all
# *   9abc123 merge: feature-login 合并
# |\
# | * 1234abc (feature-login) feat: 加登录功能 v2.0
# * | 5678def fix: 紧急修复 v1.1
# |/
# * 1112223 feat: 初始版本 v1.0
```

**一句话总结冲突解决**：看 status → 打开文件删标记保留正确内容 → `git add` → `git commit`。

**辅助命令**（遇到困惑时用）：

```sh
git status                 # 看还剩哪些冲突文件（both modified）
git merge --abort          # 反悔了，放弃整个 merge，回到合并前
```

> 💡 IDEA 用户：`git merge feature-login` 冲突后，IDEA 里 app.py 会标红，双击弹出 "Resolve Conflicts" 三栏对比器（左=你的版本，中=合并结果，右=对方版本），点箭头选边 → Apply → 再命令行 `git commit`。详见 `学习记录/16` 第 3 节冲突详解。

---

## 阶段 5：推送到 GitHub

去 [github.com](https://github.com) 新建一个**空仓库**（叫 `git-demo`，**不要勾选** README / .gitignore / License 初始化，否则会和本地仓库冲突），然后：

```sh
# 1. 关联远程仓库（用你自己的 GitHub 用户名）
git remote add origin https://github.com/你的用户名/git-demo.git

# 2. 推送 main 并建立跟踪关系（-u 记住，以后直接 git push 即可）
git push -u origin main
# 首次会弹窗让你登录 GitHub（浏览器授权 或 填 Personal Access Token）

# 3. 远程看效果：GitHub 页面上能看到 app.py、提交历史、合并图
```

**之后推 feature 分支**：

```sh
git switch feature-login
git push -u origin feature-login
# GitHub 上会看到两个分支

# 在 GitHub 上还可以点 "Compare & pull request" 发 PR
# 但教学版我们已经在本地 merge 了，GitHub 上的分支可以删掉：
git push origin --delete feature-login
```

**⚠️ 推送到空仓库常见的坑**：

| 报错 | 原因 | 解决 |
|------|------|------|
| `rejected: failed to push` | 远程有本地没有的提交（如远程仓库建时勾了 README） | `git pull origin main --allow-unrelated-histories` 再 push |
| `Please tell me who you are` | 阶段 0 没配 user.name/email | 补配后重新 commit |

---

## 阶段 6：日常收尾循环

```sh
# 上班第一件事：拉最新
git pull                    # = fetch + merge

# 开发新功能：建分支 → 提交 → 推 → 合并
git switch -c feature-xxx
# ...改代码...
git add . && git commit -m "feat: xxx"
git switch main
git pull
git merge feature-xxx
git push
git branch -d feature-xxx   # 合并完删本地分支
```

---

## 演练检查单（做完核对）

- [ ] `git init` 创建了仓库，`.git/` 目录出现
- [ ] `git switch -c` 建了 feature 分支，历史用 `--graph` 能看到分叉
- [ ] 冲突爆发时 `<<<<<<<` / `=======` / `>>>>>>>` 标记出现在 app.py
- [ ] 手动解决后 `git commit`，`--graph` 显示合并汇合
- [ ] `git push -u origin main` 后 GitHub 页面能看到仓库内容
- [ ] `git pull` / `git push` 日常循环跑通

---

## 全程命令速查（去掉注释的浓缩版）

```sh
# 初始化 + 首次提交
git init
git branch -m main
git add . && git commit -m "feat: 初始版本"

# 分支开发
git switch -c feature-xxx
git add . && git commit -m "feat: xxx"

# 合并（遇冲突：改文件删标记 → add → commit）
git switch main
git merge feature-xxx

# 推送到远程
git remote add origin <url>
git push -u origin main

# 日常循环
git pull
git push
git branch -d feature-xxx   # 合并后删本地分支
```

---

## 关联阅读

- `学习记录/16-Git 常用命令之外的实用命令.md` — 命令详解版（merge 冲突处理、worktree、stash、reset 等）
- `学习记录/16` 第 3 节冲突详解 — 命令行 vs IDEA 图形化的完整对比

---

**文档生成时间：** 2026-08-06
