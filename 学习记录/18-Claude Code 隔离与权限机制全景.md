# Claude Code 隔离与权限机制全景：墙、门、上下文

> 本文讲 **Claude Code 实际提供的全部机制**，按三个维度组织：**墙（物理隔离）、门（授权控制）、上下文（模型看到什么）**。与 `学习记录/19`（沙箱通用方案）的关系：**19 讲"墙"怎么造（跨工具的抽象模型），本文讲 Claude Code 里所有机制——包括墙、门、上下文**。读完你会分清：哪些是"碰不到"（墙），哪些是"不让做"（门），哪些是"看不见"（上下文）。

---

## 0. 先立框架：墙、门、上下文

| 维度 | 管什么 | 比喻 | 包含机制 |
|------|--------|------|---------|
| **墙（隔离）** | 命令**物理上能碰什么** | 房间的门锁 | worktree / 原生沙箱 / 容器 / VM |
| **门（控制）** | **能不能做**这件事 | 门口的保安 | 权限系统 / CWD / 网络控制 / hooks / managed settings |
| **上下文** | 模型**看到什么** | 给你看什么材料 | 子代理隔离 / 会话隔离 |

三者互补，可叠加：

```
墙：就算放行了，也碰不到（rm -rf 被拦）
门：放行不放行（要不要问你、批不批）
上下文：模型能不能看到那些信息（独立窗口 / 独立历史）
```

---

## 第一部分：墙（物理隔离）

> 这部分的"通用模型"见 `学习记录/19-Agent 沙箱全景.md`。本文给 Claude Code 的具体实现细节。

### 1. Git worktree 隔离 —— 文件系统隔离

**隔离对象**：文件系统（源码工作副本）、git 分支 / 未提交改动。

**实现方式**：git 原生 `worktree`——每个会话/子代理获得独立工作目录 + 独立分支，共享同一份 `.git`。三条入口：

| 入口 | 用法 |
|------|------|
| CLI | `claude --worktree <name>`（或 `-w`），建在 `.claude/worktrees/<name>/`，分支 `worktree-<name>`；会话中也可让 Claude 调 **EnterWorktree** 工具 |
| 子代理 | `Agent` 工具的 `isolation: "worktree"`；或 `.claude/agents/` frontmatter 声明。子代理获临时 worktree `agent-<hex>`，无改动自动清理 |
| 配套 | `worktree.baseRef`（`"fresh"` 从默认分支 / `"head"` 从当前 HEAD）、`.worktreeinclude`（把 `.env` 等 gitignored 文件复制进新 worktree） |

**复杂度/粒度**：最低。只依赖 git。粒度是"整棵工作副本 + 分支"。

**已知限制**（GitHub issues）：CWD 漂移致静默退回直改（#70456）、`EnterWorktree` 是"会话级共享 cwd 槽"致并发子代理互串（#76250）、Windows 误报"不在 git 仓库"（#69885）。**非 git 项目不可用**。

**为什么"轻"**：只隔离工作副本和分支，**不隔离**进程、网络、凭据、上下文。

### 2. 原生沙箱（sandboxing，v2.1.0+）—— 进程级隔离

**隔离对象**：Bash 命令及全部子进程、文件系统读写、网络出站。

**实现方式**（OS 原生原语）：

| 平台 | 技术 | 说明 |
|------|------|------|
| **macOS** | **Seatbelt**（内核级 syscall 过滤） | 开箱即用，约 1–2% CPU 开销 |
| **Linux / WSL2** | **bubblewrap + socat**（namespaces + seccomp-bpf） | 需 `apt install bubblewrap socat`；Ubuntu 24.04+ 可能需 AppArmor 放行 user namespace |
| **Windows 原生** | **不支持** | 需 WSL2，且 WSL2 内沙箱命令不能启动 Windows 二进制 |

- **文件系统**：默认读全盘、写限 CWD + 临时目录；`sandbox.filesystem.allowWrite` 扩展、`denyRead` 屏蔽、`credentials` 遮蔽凭据
- **网络**：所有出站经**沙箱外 SOCKS5 代理**，域名 allow/deny list；默认封私有 CIDR；`curl`/`wget` 默认阻止
- **模式**：`autoAllowBashIfSandboxed`（自动放行）、Strict（`allowUnsandboxedCommands: false`）、`failIfUnavailable`

**★ 关键边界**：只约束 **Bash** 工具。内置文件工具、MCP server、hooks **默认不受此沙箱约束**（要一起罩，用下面的容器/VM 或 sandbox runtime）。

> 具体配置（`/sandbox`、settings 键、凭据遮蔽）见官方 [Sandboxing](https://code.claude.com/docs/zh-CN/sandboxing) 文档。

### 3. 容器 / Devcontainer / VM —— 整机级隔离

**隔离对象**：进程、文件系统、网络、整个 OS。

**实现方式**：
- **Dev container**：VS Code 管理的 Docker 容器，项目挂载其中；官方提供带默认拒绝 iptables 防火墙的 example，可配合 `--dangerously-skip-permissions` 无人值守
- **Custom container**：任意 Docker/OCI 镜像，自定义网络/卷/seccomp；已有容器基础设施的组织最常见
- **VM**：独立内核（云实例 / 本地 hypervisor / microVM 如 Firecracker），最强分离，适合评估不可信代码
- **Claude Code on the web**：Anthropic 托管整机沙箱，凭据（git/签名密钥）**永不进沙箱**，git 走自定义代理注入 scoped 凭据

**复杂度/粒度**：高 / 整机级。

---

## 第二部分：门（授权控制）

> 这些机制不改变"命令能碰什么"，而是决定**放行不放行**——是"门"不是"墙"。

### 4. 权限系统 —— 工具调用的门

**管什么**：Claude 可以用哪些工具（工具级门禁）。

**实现方式**：
- `permissions.allow` / `deny` / `ask` 规则（支持 `Bash(git *)`、`Write(src/**)`、`mcp__server__tool`、`Agent(Explore)`）
- **决策流水线**：PreToolUse 钩子 → deny → ask → 工具自身检查 → 安全检查 → 模式 → allow → 默认询问
- **权限模式**：`default`（询问）、`acceptEdits`（自动批准编辑）、`plan`（只读）、`dontAsk`（未预批一律拒绝）、`auto`（后台分类器审批）、`bypassPermissions`（全部跳过）

**要点**：deny 优先于 allow；bypass 下 deny 规则、hooks 仍生效；拒绝以 root 运行。

### 5. CWD / workspace roots 限制 —— 文件工具的门

**管什么**：结构化文件工具（Read/Write/Edit/Glob/Grep）的文件系统作用域。

**实现方式**：harness 层约束——**只能写启动目录及其子目录**；读可越界但会提示审批。用 `additionalDirectories` / `--add-dir` 扩展。**Bash 不受此门限制**（`cd` 跨调用保持）。

**★ 易混点**：CWD 限制 ≠ worktree。CWD 是"只能写启动目录内"（门），worktree 是"换个独立工作副本"（墙）。二者可叠加。

### 6. 网络访问控制 —— 出站的门

**管什么**：网络出站。分三层：

| 层 | 技术 | 示例 |
|----|------|------|
| **工具层** | `disallowedTools`（工具不出现）或 `permissions.deny`（调用即报错） | 禁用 WebSearch/WebFetch |
| **Bash 层** | `permissions.deny: ["Bash(curl:*)"]` | 禁用 curl/wget |
| **沙箱层** | `sandbox.network.allowedDomains` 域名白名单 + SOCKS5 代理 | 只允许指定域名 |

### 7. Hooks（PreToolUse 等）—— 策略强制

**管什么**：工具调用本身（可拦截/改写）。是"门"的自动化，不是隔离。

**实现方式**：PreToolUse 返回 `permissionDecision: "deny"/"allow"/"ask"/"defer"` / **修改工具入参**（如改写 `file_path` 到沙箱目录）；shell hook 以 exit code 2 阻断；PostToolUse 用于审计。

### 8. 企业 managed settings / IAM —— 组织级的门

**管什么**：组织级策略（用户不可覆盖）。

**实现方式**：`managed-settings.json`（macOS `/Library/Application Support/ClaudeCode/`、Linux `/etc/claude-code/`、Windows `C:\ProgramData\ClaudeCode\`），含 `allowManagedHooksOnly`、`enforceSandbox`、`disablePermissionBypass`、`disallowedCommands`、`requiredMinimumVersion`。

---

## 第三部分：上下文管理（模型看到什么）

### 9. 子代理隔离

**隔离对象**：上下文窗口、系统提示词、工具集、模型、工作目录、transcript。

**实现方式**：独立 LLM 推理循环 + **全新 context window**（不继承父会话历史）；自带 system prompt/工具集/权限；只把最终总结返回父会话；transcript 独立存于 `~/.claude/projects/{project}/{sessionId}/subagents/`。可选 FS 隔离（`isolation: "worktree"` / `cwd`）。内置只读子代理：`Explore`、`Plan`。

### 10. 会话隔离

**隔离对象**：对话历史/上下文（时间维度，非安全维度）。

**实现方式**：每个会话一条 JSONL transcript，存 `~/.claude/projects/<encoded-cwd>/`；continue/resume/fork。按 cwd 分组而非 session id（#24864 提议改造）。

---

## 总结：三者关系与叠加

```
墙（隔离）──决定"碰不到"──→ 碰不到
门（控制）──决定"不让做"──→ 放行不放行
上下文  ──决定"看不见"──→ 模型看到什么
```

**真实系统三者叠加**：
- 一个工具调用要过**门**（权限：要不要问）→ 放行后受**墙**（沙箱：碰不到越界物）约束
- 子代理/会话**上下文**独立，互不污染
- `--dangerously-skip-permissions` 只跳过**门**，**不绕过墙**（沙箱仍生效）

**与 19 号文档的关系**：19 讲墙的通用模型（沙箱目标 → 进程沙箱/容器/VM 实现 → 业界路线 → Claude Code 六档）；本文讲 Claude Code 全部机制，把墙（第 1-3 节）之外的**门**和**上下文**补全。

---

## 技术实现速查（只记关键词）

| 机制 | 属于 | 核心技术 |
|------|------|---------|
| worktree | 墙 | `git worktree` 多目录共享 `.git` |
| 沙箱 (macOS) | 墙 | **Seatbelt**（内核 syscall 过滤） |
| 沙箱 (Linux) | 墙 | **bubblewrap** + **seccomp-bpf** + socat |
| 沙箱网络 | 墙 | **SOCKS5 代理** + 域名白名单 |
| 容器/VM | 墙 | Docker/Podman / QEMU / Firecracker |
| 权限 | 门 | allow/deny/ask 规则 + 决策流水线 + 6 模式 |
| CWD 限制 | 门 | harness 层约束写启动目录内 |
| Hooks | 门 | 生命周期钩子 + exit code 2 阻断 |
| managed settings | 门 | `managed-settings.json` 组织级 |
| 子代理 | 上下文 | 独立 context window + 独立推理循环 |
| 会话 | 上下文 | JSONL transcript + continue/resume/fork |

---

## 关联阅读

- `学习记录/19-Agent 沙箱全景.md` — 墙的通用模型（沙箱目标/进程沙箱/容器/VM/业界路线）
- `学习记录/16-Git 常用命令之外的实用命令.md` — git 命令基础（worktree 用到）
- `s18_worktree_isolation/README.md` — worktree 隔离详解（墙 #1 的展开）

**文档生成时间：** 2026-08-07
