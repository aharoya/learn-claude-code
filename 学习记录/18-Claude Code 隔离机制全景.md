# Claude Code 隔离机制全景：从 worktree 到容器，各自用什么技术实现

> 接在 s18（worktree 隔离）之后，回答一个问题：**worktree 在 Claude Code 的整个隔离体系里处在什么位置？** 本文基于 Claude Code 官方文档查证，列出 10 种隔离机制，每种讲清楚：**隔离对象是什么、用什么技术实现、复杂度/粒度**。读完你会明白：worktree 是"依赖最少、最简单"的一档，但它的简单来自"只管文件系统这一件事"——要防进程/网络/凭据越界，得上沙箱或容器，各档互相叠加。

---

## 0. 先记住一句话

> **worktree 对"单线程的人类开发者"是可选优化，对"多线程的 Agent 并行"是刚需。** 而 Claude Code 的全部隔离机制是**层层叠加**的，不是二选一：worktree（文件不互相覆盖）→ 权限门（该不该授权）→ 沙箱（就算授权了，越界了怎么办）。

---

## 1. Git worktree 隔离（工作树隔离）—— 依赖最少的一档

**隔离对象**：文件系统（源码工作副本）、git 分支 / 未提交改动。

**实现方式**：就是 git 原生 `worktree`——每个会话/子代理获得一个独立的 git 工作目录 + 独立分支，共享同一份 `.git` 历史与 remote。三条入口：

| 入口 | 用法 |
|------|------|
| CLI | `claude --worktree <name>`（或 `-w`），建在 `.claude/worktrees/<name>/`，分支 `worktree-<name>`；会话中途也可让 Claude 调用 **EnterWorktree** 工具进入 |
| 子代理 | `Agent` 工具的 `isolation: "worktree"` 参数；或 `.claude/agents/` 子代理 frontmatter 声明。子代理获得临时 worktree `agent-<hex>`，无改动则自动清理 |
| 配套 | `worktree.baseRef`（`"fresh"` 从默认分支 / `"head"` 从当前 HEAD）、`.worktreeinclude` 文件（把 `.env` 等 gitignored 文件复制进新 worktree） |

**复杂度/粒度**：**最低**之一。只依赖 git，零内核级原语、零代理进程、零安装依赖。粒度是"整棵工作副本 + 分支"。

**已知限制**（GitHub issues）：CWD 漂移导致静默退回直改（#70456）、Windows 上分支基准错误（#41368）、`EnterWorktree` 是"会话级共享 cwd 槽"导致并发子代理互相串改（#76250）、worktree 里看不到你的未提交改动却给出"自信结论"（#82955）、Windows 上误报"不在 git 仓库"（#69885）。**非 git 项目不可用**。

**为什么"轻"**：它只隔离工作副本和分支，**不隔离**进程（命令仍在宿主上跑）、网络（能联网）、凭据（`~/.ssh`、`~/.aws` 原样可读）、上下文（若不加 `isolation`，子代理上下文隔离是另一套机制）。

---

## 2. 原生沙箱（sandboxing，v2.1.0+）—— 进程级隔离

**隔离对象**：Bash 进程、文件系统读写、网络出站。

**实现方式**（OS 原生原语，这是本机制的技术核心）：

| 平台 | 技术 | 说明 |
|------|------|------|
| **macOS** | **Seatbelt**（内核级 syscall 过滤） | 开箱即用，约 1–2% CPU 开销 |
| **Linux / WSL2** | **bubblewrap + socat**（namespaces + seccomp-bpf） | 需 `apt install bubblewrap socat`；Ubuntu 24.04+ 可能需 AppArmor 放行 user namespace |
| **Windows 原生** | **不支持** | 需 WSL2，且 WSL2 内沙箱命令不能启动 Windows 二进制 |

- **文件系统**：默认读全盘、写限 CWD + 临时目录；`sandbox.filesystem.allowWrite` 扩展、`denyRead` 屏蔽、`credentials.files/envVars` 遮蔽凭据；可整体禁用（`disabled: true`，仅保留网络层）
- **网络**：所有出站经**沙箱外的 SOCKS5 代理**，域名 allow/deny list（`sandbox.network.allowedDomains`），默认封私有 CIDR；`curl`/`wget` 默认阻止；默认不终止 TLS，`network.tlsTerminate` 为实验性
- **模式**：`autoAllowBashIfSandboxed`（自动放行）、普通权限模式、Strict（`allowUnsandboxedCommands: false`，封死 `dangerouslyDisableSandbox` 逃生门）、`sandbox.failIfUnavailable`

**复杂度/粒度**：中。粒度是"进程级 + FS 规则 + 域名规则"，比 worktree 粗但覆盖面广。

**★ 关键点**：沙箱与权限系统是**两条独立防线**——`--dangerously-skip-permissions` 只跳过审批，**不绕过沙箱**。

---

## 3. 权限系统 —— 工具调用的"门"

**隔离对象**：工具调用（操作/能力）。是"门"而非"墙"。

**实现方式**：
- settings 里的 `permissions.allow` / `deny` / `ask` 规则（支持 `Bash(git *)`、`Write(src/**)`、`mcp__server__tool`、`Agent(Explore)` 模式）
- **决策流水线**：PreToolUse 钩子 → deny → ask → 工具自身检查 → 安全检查 → 模式 → allow → 默认询问
- **权限模式**：`default`（询问）、`acceptEdits`（自动批准文件编辑）、`plan`（只读）、`dontAsk`（不询问，未预批的一律拒绝）、`auto`（后台分类器审批，research preview）、`bypassPermissions`（全部跳过审批）

**要点**：deny 优先于 allow；bypass 模式下 deny 规则、hooks、部分安全路径检查仍生效；`bypassPermissions` 要求 `allowDangerouslySkipPermissions: true`；拒绝以 root 运行。

**复杂度/粒度**：低到中。粒度是"单次工具调用"。

---

## 4. CWD / workspace roots 限制 —— 文件工具的作用域

**隔离对象**：结构化文件工具（Read/Write/Edit/Glob/Grep）的文件系统作用域。

**实现方式**：harness 层约束——**只能写启动目录及其子目录**；读可越界但会提示审批。用 `additionalDirectories` / `--add-dir` / `/add-dir` 扩展根集合。**Bash 不受此文件门限制**（`cd` 跨调用保持）。

**复杂度/粒度**：低。粒度是"目录根集合"。

**已知限制**：子代理不继承 `additionalDirectories`（#32034）；`bypassPermissions` 实际隐式受 CWD 作用域限制（#51180，文档未写）。

**★ 易混点**：worktree 和 CWD 限制**不是一回事**——CWD 限制是"只能写启动目录内"，worktree 是"换个独立工作副本"。二者可叠加。

---

## 5. 子代理（subagent）隔离 —— 上下文级隔离

**隔离对象**：上下文窗口、系统提示词、工具集、模型、工作目录、transcript。

**实现方式**：
- 独立的 LLM 推理循环 + **全新 context window**（不继承父会话历史）
- 自带 system prompt、CLAUDE.md、工具集、权限
- 只把最终总结返回父会话，中间过程不污染主上下文
- transcript 独立存于 `~/.claude/projects/{project}/{sessionId}/subagents/`（默认 30 天清理）
- **可选 FS 隔离**：`isolation: "worktree"`（每子代理独立工作副本）；`cwd` 参数（与 worktree 互斥）
- **内置只读子代理**：`Explore`（Haiku，只读）、`Plan`（只读）——本身是"模型行为层"的只读隔离

**复杂度/粒度**：中。粒度可配（上下文 100% 隔离 + 可选 FS 隔离）。

---

## 6. 会话隔离（session）—— 时间维度的上下文管理

**隔离对象**：对话历史/上下文（时间维度，非安全维度）。

**实现方式**：每个会话一条 JSONL transcript，存 `~/.claude/projects/<encoded-cwd>/`；支持 continue（最近会话）、resume（按 UUID）、fork（从历史分叉出新会话）。**按 cwd 分组而非 session id**（#24864 提议改造成真正隔离）。

**复杂度/粒度**：低。是"上下文可恢复/管理"手段，不是安全边界。

---

## 7. 网络访问控制 —— 出站管控

**隔离对象**：网络出站。分三层：

| 层 | 技术 | 示例 |
|----|------|------|
| **工具层** | `disallowedTools: ["WebSearch","WebFetch"]`（工具不出现在模型面前）或 `permissions.deny`（调用即报错） | 禁用搜索/抓取工具 |
| **Bash 层** | `permissions.deny: ["Bash(curl:*)", "Bash(wget:*)"]` | 禁用 curl/wget 命令 |
| **沙箱层** | `sandbox.network.allowedDomains` 域名白/黑名单 + SOCKS5 代理 + 默认封私有 CIDR；`allowUnixSockets` 可放行 Docker/SSH socket（也是风险面） | 只允许访问指定域名 |

**复杂度/粒度**：低到中。粒度从"单个工具"到"域名/端口"。

---

## 8. Hooks（PreToolUse 等）—— 策略强制层

**隔离对象**：工具调用本身（可拦截/改写）。是"策略强制"，不是隔离本身，但常被当作沙箱/权限之外的第三道防线。

**实现方式**：
- PreToolUse 可返回 `permissionDecision: "deny"` / `allow` / `ask` / `defer` / **修改工具入参**（如改写 `file_path` 到沙箱目录）
- shell hook 以 exit code 2 阻断
- PostToolUse 用于审计/反馈

**复杂度/粒度**：中 / 每事件。

---

## 9. 企业 managed settings / IAM —— 组织级隔离

**隔离对象**：组织级策略（用户不可覆盖）。

**实现方式**：`managed-settings.json`（macOS `/Library/Application Support/ClaudeCode/`、Linux `/etc/claude-code/`、Windows `C:\ProgramData\ClaudeCode\`），含 `allowManagedHooksOnly`、`enforceSandbox`、`disablePermissionBypass`、`disallowedCommands`、`requiredMinimumVersion`——组织级、用户不可覆盖。

**复杂度/粒度**：中 / 组织级。

---

## 10. 容器 / Devcontainer / VM —— 隔离强度最高

**隔离对象**：进程、文件系统、网络、整个 OS。

**实现方式**：官方提供 devcontainer feature（`ghcr.io/anthropics/devcontainer-features/claude-code`），文档明确"降低但**不消除**风险"。社区封装：claudeman（Podman）、mirabilis（Docker + 认证代理）、viwo（worktree + 容器 + harness）、trusty-cage（容器内无 remote 仓库，只能本地 commit）等。这是**部署方式**，不是内建机制，但隔离强度最高（进程/FS/网络全隔离）。

**复杂度/粒度**：高 / 整机级。

---

## 总结：完整对比清单

| # | 机制 | 隔离对象 | 实现方式 | 复杂度/粒度 | 产品内置 |
|---|------|---------|---------|------------|---------|
| 1 | Git worktree | 文件系统、git 分支/改动 | git worktree；`--worktree` / EnterWorktree / Agent `isolation:"worktree"` | 低 / 整棵工作副本 | ✅（依赖 git） |
| 2 | 原生沙箱 | Bash 进程、FS 读写、网络 | macOS **Seatbelt**；Linux **bubblewrap + seccomp**；Windows 无原生（WSL2） | 中 / 进程级+FS+域名 | ✅ v2.1.0+ |
| 3 | 权限系统 | 工具调用 | allow/deny/ask 规则 + 6 种权限模式 + 决策流水线 | 低–中 / 单次调用 | ✅ |
| 4 | CWD / workspace roots | 结构化文件工具作用域 | harness 层限制写 CWD 内；`additionalDirectories` 扩展 | 低 / 目录根集合 | ✅ |
| 5 | 子代理隔离 | 上下文、系统提示、工具集、模型、transcript | 独立推理循环 + 全新 context window；`isolation:"worktree"` / `cwd` 可选 | 中 / 每子代理 | ✅ |
| 6 | 会话隔离 | 对话历史 | 每会话 JSONL transcript，continue/resume/fork | 低 / 每会话 | ✅（非安全边界） |
| 7 | 网络访问控制 | 网络出站 | 工具层 disallowedTools + Bash 层 deny + 沙箱层域名代理 | 低–中 / 工具→域名→端口 | ✅（TLS 终止实验性） |
| 8 | Hooks | 策略强制（拦截/改写工具调用） | PreToolUse `permissionDecision` / shell exit code 2 阻断 | 中 / 每事件 | ✅ |
| 9 | 企业 managed settings | 组织级策略 | `managed-settings.json` + `enforceSandbox` 等 | 中 / 组织级 | ✅（Enterprise） |
| 10 | 容器/Devcontainer/VM | 进程、FS、网络、OS | 官方 devcontainer feature；社区 claudeman/mirabilis/viwo | 高 / 整机级 | ⚠️ feature 官方，完整方案多为社区 |

---

## 核心问题回答：worktree 是不是最简单的一种？

**在"文件系统/工作副本"这个维度上——是。** 它是全部机制里依赖最少的一个：

| 维度 | worktree | 原生沙箱 | 容器/VM |
|------|----------|---------|---------|
| 依赖 | 仅 git | macOS Seatbelt 内建；Linux 需 bwrap+socat；Windows 原生无 | Docker/Podman + 镜像 |
| 隔离面 | 仅工作副本+分支 | 进程+FS+网络 | 进程+FS+网络+OS |
| 性能开销 | 接近零 | 1–2% CPU | 中 |

**但它的"轻"来自"只管一件事"**——只隔离工作副本和分支，不隔离进程、网络、凭据、上下文。所以：

- 目标是"并行子代理改代码不打架 / 实验性改动不碰主干" → **worktree 最简单，且够用**
- 目标是"命令不能乱跑、不能写系统目录、不能外联" → 需要**沙箱**（或容器）
- 目标是"完全隔离主机、防恶意项目" → 需要**容器/VM**

---

## 技术实现速查（只记关键词）

| 机制 | 核心技术 |
|------|---------|
| worktree | `git worktree` 多工作目录共享 `.git` |
| 沙箱 (macOS) | **Seatbelt**（内核 syscall 过滤） |
| 沙箱 (Linux) | **bubblewrap**（namespaces）+ **seccomp-bpf**（syscall 过滤）+ socat |
| 沙箱 (Windows) | 无原生，靠 WSL2 |
| 沙箱网络 | **SOCKS5 代理** + 域名 allow/deny list |
| 权限 | allow/deny/ask 规则 + 决策流水线 |
| 子代理 | 独立 context window + 独立推理循环 |
| Hooks | 生命周期钩子 + exit code 2 阻断 |

---

## 关联阅读

- `s18_worktree_isolation/README.md` — worktree 基础 + s18 如何编排（隔离机制 #1 的展开）
- `学习记录/15-Agent 工程五层递进全景.md` — ③ Harness 层：权限/沙箱/MCP 属于 Harness Engineering
- `学习记录/03-Harness 核心概念.md` — Harness 五要素中的"权限"维度

**文档生成时间：** 2026-08-06
