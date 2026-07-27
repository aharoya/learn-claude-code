# Claude Code Windows 中文环境快速配置指南

> 换新电脑后，按这个文档一步步配置，就能恢复和之前一样的使用环境。

---

## 一、前置安装

### 1.1 PowerShell 7+

```powershell
winget install Microsoft.PowerShell
```

验证安装：

```powershell
pwsh --version
# 输出示例：PowerShell 7.5.0
```

> 系统自带的 Windows PowerShell 5.1 是 GBK 编码，会有中文乱码问题。**必须装 PowerShell 7+。**

### 1.2 Windows Terminal（推荐）

```powershell
winget install --id Microsoft.WindowsTerminal -e
```

替代默认的蓝绿色控制台，原生支持 UTF-8 和中文渲染。

### 1.3 Git

```powershell
winget install Git.Git
```

Claude Code 依赖 Git（用于工作树、查看 git 状态等）。

### 1.4 Node.js（可选，用于 Web 项目）

```powershell
winget install OpenJS.NodeJS.LTS
```

---

## 二、安装 Claude Code

### 2.1 安装 npm 包

```powershell
npm install -g @anthropic-ai/claude-code
```

### 2.2 验证安装

```powershell
claude --version
```

---

## 三、一句 prompt 自动配置（推荐）

安装完 Claude Code 后，打开 `claude`，在首次对话中贴入以下 prompt，**它会自己把 settings.json 和 CLAUDE.md 配好**，不需要手动编辑文件。

参考来源：[femoon.top — Claude Code 强制使用 PowerShell](https://femoon.top/blog/claude-code-force-powershell)

### 完整 prompt

把下面内容一次性粘贴到 Claude Code 的对话窗口：

> 把下面两处配置落地到我本地，保留原有内容不要覆盖，合并而不是替换：
>
> **第一处：CLAUDE.md（三段 Markdown）**
>
> 在 `~/.claude/CLAUDE.md` 顶部追加以下三段 markdown：
>
> **第一段 — Shell Preference**
>
> 默认禁止调用 Bash Tool，所有 shell 命令只能用 PowerShell Tool（pwsh.exe）——这是工具层的硬约束。Unix 惯用语法（`ls | head`、`grep`、`curl`、`find`、`cat`）不是借口，改用对应 cmdlet（`Get-ChildItem | Select-Object -First N`、`Select-String`、`Invoke-WebRequest`、`Get-ChildItem -Recurse`、`Get-Content`）。列文件用 Glob，读文件用 Read，搜内容用 Grep——dedicated tool 优先级仍然高于 shell。路径一律使用 Windows 原生风格（`C:\` 或 `$env:USERPROFILE`），只有在调用 git/POSIX 工具时可临时接受 `/c/...`。
>
> **第二段 — Bash Tool 例外清单**
>
> 仅在以下场景允许使用 Bash Tool，且调用时需一句话说明原因：
> 1. 用户明确指令（"用 bash"、"跑 sh 脚本"等）
> 2. 必须依赖 POSIX 行为的脚本（仓库已有的 `.sh` 文件、`Makefile` 目标、`configure` 脚本）
> 3. Git hooks / pre-commit 框架
> 4. 跨平台 CI 脚本本地复现
> 5. MINGW-only 二进制（`ssh-agent`、`gpg`、`openssl` 等）
>
> 不在上述清单一律走 PowerShell，模糊地带先问用户。
>
> **第三段 — PowerShell 实战注意**
>
> - 文件写出默认 UTF-8 无 BOM；用 `Out-File`/`Set-Content` 时不要手动加 `-Encoding utf8BOM`
> - 调用带空格路径的原生 exe 用 call 操作符：`& "C:\Program Files\App\app.exe" arg1`
> - 给原生命令传 `-`、`@`、`--` 开头的参数时用 stop-parsing token：`git log --% --format=%H`
> - 多行字符串用单引号 here-string，闭合 `'@` 必须顶格
> - `-ErrorAction SilentlyContinue` 只压制输出不改退出码；要真正吞错用 `try { ... -ErrorAction Stop } catch {}`
> - 严禁 `Invoke-Expression` 拼接用户输入
> - 严禁 `New-Item -Force` 用在已存在的文件上
>
> **第二处：settings.json（环境变量 + 默认 shell）**
>
> 把以下两项合并进 `~/.claude/settings.json`（保留其他字段）：
>
> ```json
> {
>   "env": {
>     "CLAUDE_CODE_USE_POWERSHELL_TOOL": "1",
>     "POWERSHELL_TELEMETRY_OPTOUT": "1"
>   },
>   "defaultShell": "powershell"
> }
> ```
>
> **第三处：settings.json permissions**
>
> 合并进 `~/.claude/settings.json` 的 permissions（保留已有其他元素）：
>
> ```json
> {
>   "permissions": {
>     "allow": ["PowerShell", "Read", "Write", "Edit", "MultiEdit", "Glob", "WebFetch", "WebSearch", "NotebookRead", "NotebookEdit"],
>     "deny": ["Bash"]
>   }
> }
> ```
>
> 改完列出具体改动点。

> ⚠️ 首次运行 Claude Code 会提示输入 API Key，可以先输入一个临时 key 完成对话，等配置完成后改回正式的。

### 原理

这个 prompt 让 Claude Code 自己完成三件事：

| 配置位置 | 作用 |
|---------|------|
| `~/.claude/CLAUDE.md` | 用户级全局指令，每次会话自动加载 |
| `~/.claude/settings.json` `env` 段 | 环境变量在 pwsh 启动前生效 |
| `~/.claude/settings.json` `permissions.deny` | 工具层硬约束——Bash 直接被排除，模型连选项都看不到 |

---

## 四、手动配置（如果不使用上面的自动方法）

### 4.1 创建全局 settings.json

路径：`C:\Users\<你的用户名>\.claude\settings.json`

创建 `.claude` 目录：

```powershell
mkdir $env:USERPROFILE\.claude -Force
```

写入配置（将以下内容保存为 `settings.json`）：

```json
{
  "env": {
    "ANTHROPIC_BASE_URL": "https://api.deepseek.com/anthropic",
    "ANTHROPIC_MODEL": "deepseek-v4-flash",
    "ANTHROPIC_DEFAULT_OPUS_MODEL": "deepseek-v4-pro",
    "ANTHROPIC_DEFAULT_SONNET_MODEL": "deepseek-v4-pro",
    "ANTHROPIC_DEFAULT_HAIKU_MODEL": "deepseek-v4-flash",
    "CLAUDE_CODE_SUBAGENT_MODEL": "deepseek-v4-flash",
    "CLAUDE_CODE_MAX_OUTPUT_TOKENS": "32000",
    "CLAUDE_CODE_USE_POWERSHELL_TOOL": "1",
    "POWERSHELL_TELEMETRY_OPTOUT": "1"
  },
  "permissions": {
    "allow": [
      "PowerShell",
      "Read",
      "Write",
      "Edit",
      "Glob",
      "Grep",
      "WebFetch",
      "WebSearch",
      "NotebookRead",
      "NotebookEdit"
    ],
    "deny": ["Bash"]
  },
  "defaultShell": "powershell",
  "model": "deepseek-chat",
  "language": "zh-CN",
  "theme": "dark"
}
```

> 如果你用 Anthropic 官方 API，把 `ANTHROPIC_BASE_URL` 和模型 ID 换成对应的值。

### 4.2 配置 API Key

```powershell
# 方式一：写入 settings.json 的 env 字段（如上）
# 方式二：设置环境变量（适用于 CI/CD）
[Environment]::SetEnvironmentVariable("ANTHROPIC_AUTH_TOKEN", "sk-xxx", "User")
```

---

## 五、PowerShell 编码配置（解决中文乱码）

### 4.1 配置 $PROFILE

打开配置文件：

```powershell
notepad $PROFILE
```

添加以下内容：

```powershell
# ── UTF-8 编码设置 ──
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
[Console]::InputEncoding = [System.Text.Encoding]::UTF8
$PSDefaultParameterValues['*:Encoding'] = 'utf8'
chcp 65001 > $null
```

重新加载配置：

```powershell
. $PROFILE
```

### 4.2 检查执行策略

如果 `$PROFILE` 不生效，检查执行策略：

```powershell
Set-ExecutionPolicy -Scope CurrentUser RemoteSigned -Force
```

### 4.3 验证中文显示

```powershell
Write-Host "中文测试：你好 PowerShell！"
# 应正常显示，不乱码
```

---

## 六、Git 配置

### 6.1 全局用户配置（个人账号）

```powershell
git config --global user.name "你的GitHub用户名"
git config --global user.email "你的个人邮箱@example.com"
```

### 6.2 项目级用户配置（工作/项目账号）

当某个项目需要**不同的用户名和邮箱**（比如公司项目用企业邮箱），在项目目录下设置局部配置，覆盖全局：

```powershell
cd D:\projects\company-project
git config --local user.name "你的企业用户名"
git config --local user.email "你的企业邮箱@company.com"
```

查看各级配置：

```powershell
git config --global --list   # 全局
git config --local --list    # 当前项目（需在项目目录下执行）
git config --list            # 合并视图（local > global > system）
```

### 6.3 凭据管理

```powershell
# 配置 Windows 凭据管理器存储 GitHub token
git config --global credential.helper wincred

# 如果多个 GitHub 账号需要不同 token，可以用有条件配置
# 在项目级 .git/config 中单独指定凭据
git config --local credential.helper ""
git config --local credential.username "your-company-bot"
```

---

## 七、项目级配置（以本教程项目为例）

### 7.1 克隆项目

```powershell
git clone https://github.com/aharoya/learn-claude-code.git
cd learn-claude-code
```

### 7.2 环境变量

复制并编辑 `.env`：

```powershell
cp .env.example .env
notepad .env
```

配置项：

```ini
ANTHROPIC_API_KEY=sk-你的key
MODEL_ID=deepseek-v4-flash
ANTHROPIC_BASE_URL=https://api.deepseek.com/anthropic
```

### 7.3 Python 依赖

```powershell
pip install -r requirements.txt
```

---

## 八、项目 CLAUDE.md（跨机器同步）

本教程项目的 `CLAUDE.md` 已经包含 Windows PowerShell 规则，放在版本控制中，clone 后自动生效。核心规则摘要：

```markdown
# Windows + pwsh 7+ 规则
- 默认禁止 Bash Tool，只能用 PowerShell
- 路径用 Windows 风格（C:\xxx）
- 多行字符串用单引号 here-string，闭合 '@ 必须顶格
- 调用带空格路径的原生 exe 用 & 操作符
- 给原生命令传 - @ 开头的参数用 --% stop-parsing token
- 子进程 subprocess.run 加 encoding="utf-8"，报错时回退 gbk
```

> 如果自己开新项目，直接把这份规则复制到项目的 `CLAUDE.md` 里。

---

## 九、验证清单

新电脑配置完后，逐项检查：

| # | 检查项 | 验证命令 | 期望结果 |
|---|--------|---------|---------|
| 1 | PowerShell 7+ | `pwsh --version` | 7.x |
| 2 | Claude Code | `claude --version` | 显示版本号 |
| 3 | 全局 settings.json | `cat $env:USERPROFILE\.claude\settings.json \| Select-String "CLAUDE_CODE_USE_POWERSHELL_TOOL"` | 存在且为 "1" |
| 4 | 编码配置 | `chcp` | 65001（UTF-8） |
| 5 | 中文显示 | `Write-Host "你好"` | 正常显示 |
| 6 | Git 凭据 | `git config --global credential.helper` | wincred |
| 7 | Python 依赖 | `pip list \| Select-String anthropic` | anthropic 已安装 |
| 8 | API Key | `cat $env:USERPROFILE\.claude\settings.json \| Select-String ANTHROPIC_AUTH_TOKEN` | 已配置 |

---

## 十、常见问题排查

### 10.1 中文显示为 □□□

```
原因：系统编码是 GBK，但输出是 UTF-8
解决：
  1. 确认安装了 pwsh 7+ 而非 Windows PowerShell 5.1
  2. 确认 $PROFILE 中有 [Console]::OutputEncoding = [Encoding]::UTF8
  3. 确认终端是 Windows Terminal 而非旧版 conhost
```

### 10.2 Bash Tool 被拒绝

```
原因：settings.json 中 deny: ["Bash"]
解决：这是故意配置的——Windows 下应使用 PowerShell。
     如果确需 Bash，移除 deny 列表中的 "Bash"。
```

### 10.3 subprocess.run 编码报错

```
原因：子进程输出是 GBK，但 Python 默认用 UTF-8 解码
解决：subprocess.run(..., encoding="gbk")
     或 try utf-8 → fallback gbk
```

### 10.4 项目里 demo_code.py 连不上 API

```
原因：.env 文件未配置或配置错误
解决：
  1. 确认 .env 文件存在
  2. 确认 ANTHROPIC_API_KEY 正确
  3. 确认 MODEL_ID 和 ANTHROPIC_BASE_URL 匹配
```

---

## 十一、快速恢复脚本

把以下内容保存为 `setup-claude-code.ps1`，新机器上以管理员身份运行：

```powershell
# Claude Code Windows 环境快速配置脚本

# 安装软件
Write-Host "=== 安装 PowerShell 7+ ===" -ForegroundColor Cyan
winget install Microsoft.PowerShell

Write-Host "=== 安装 Windows Terminal ===" -ForegroundColor Cyan
winget install --id Microsoft.WindowsTerminal -e

Write-Host "=== 安装 Git ===" -ForegroundColor Cyan
winget install Git.Git

# 创建 .claude 目录
$configDir = "$env:USERPROFILE\.claude"
New-Item -ItemType Directory -Path $configDir -Force | Out-Null

# 写入 settings.json
$settings = @'
{
  "env": {
    "CLAUDE_CODE_USE_POWERSHELL_TOOL": "1",
    "CLAUDE_CODE_MAX_OUTPUT_TOKENS": "32000",
    "POWERSHELL_TELEMETRY_OPTOUT": "1"
  },
  "permissions": {
    "allow": ["PowerShell", "Read", "Write", "Edit", "Glob", "Grep"],
    "deny": ["Bash"]
  },
  "defaultShell": "powershell",
  "language": "zh-CN",
  "theme": "dark"
}
'@
$settings | Out-File -FilePath "$configDir\settings.json" -Encoding utf8

# 配置 $PROFILE 编码
$profileContent = @'
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
[Console]::InputEncoding = [System.Text.Encoding]::UTF8
$PSDefaultParameterValues['*:Encoding'] = 'utf8'
chcp 65001 > $null
'@
$profileContent | Out-File -FilePath $PROFILE -Encoding utf8 -Append

# 设置执行策略
Set-ExecutionPolicy -Scope CurrentUser RemoteSigned -Force

# 安装 Claude Code
npm install -g @anthropic-ai/claude-code

Write-Host "=== 配置完成！请重启终端 ===" -ForegroundColor Green
Write-Host "然后执行 claude --version 验证安装" -ForegroundColor Yellow
```

---

## 参考来源

- [Claude Code 官方文档 — Extend Claude Code](https://code.claude.com/docs/en/features-overview)
- [Claude Code 官方文档 — How Claude Code works](https://code.claude.com/docs/en/how-claude-code-works)
- [Claude Code Hooks 参考](https://code.claude.com/docs/en/hooks)
- [fEmOON — Claude Code 强制使用 PowerShell](https://femoon.top/blog/claude-code-force-powershell)
