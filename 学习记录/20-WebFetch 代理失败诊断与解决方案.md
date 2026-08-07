# WebFetch 代理失败诊断与解决方案

> 记录一次实战：Claude Code 的 WebFetch 工具抓取网页失败（`ECONNREFUSED`），最终定位为"代理配置读取"问题，并给出 4 个候选解决方案。读完你会明白：**WebFetch 只认环境变量代理、不读 Windows 系统代理**——这是它在你机器上失败的根因；以及如何在不影响国内网站访问的前提下解决。

---

## 1. 问题现象

使用 WebFetch 抓取 `https://code.claude.com/docs/zh-CN/sandbox-environments` 时失败：

```
connect ECONNREFUSED 202.53.137.209:443
```

**关键信息**：`ECONNREFUSED`（连接被拒绝）——说明走了直连，目标 IP 拒绝了连接。

**矛盾点**：浏览器能正常打开这个网页（你挂了代理），但 WebFetch 失败。

---

## 2. 诊断过程

检查机器上的代理配置，分两类：

```powershell
# ① 环境变量代理（WebFetch 认这个）
$env:HTTP_PROXY
$env:HTTPS_PROXY
$env:ALL_PROXY

# ② Windows 系统代理（浏览器、Invoke-WebRequest 认这个）
Get-ItemProperty -Path "HKCU:\Software\Microsoft\Windows\CurrentVersion\Internet Settings" |
    Select-Object ProxyEnable, ProxyServer
```

**诊断结果**：

```
=== 环境变量代理 ===    （空！）
=== Windows 系统代理 ===
ProxyEnable : 1
ProxyServer : 127.0.0.1:7890
```

- 系统代理已开启：`127.0.0.1:7890`（Clash 默认端口）
- **但环境变量 `HTTP_PROXY`/`HTTPS_PROXY` 是空的**

---

## 3. 根因分析

**WebFetch 工具只认环境变量代理（`HTTP_PROXY`/`HTTPS_PROXY`），不读 Windows 系统代理设置。**

- 你的浏览器能打开 → 浏览器读**系统代理**（127.0.0.1:7890）
- WebFetch 失败 → 它读**环境变量**，而环境变量为空 → 走了直连 → 被目标拒绝

一句话：**"工具读哪个配置源"决定了它走不走代理。**

| 访问方 | 读什么 | 结果 |
|--------|--------|------|
| 浏览器 | Windows 系统代理（127.0.0.1:7890） | ✅ 走代理，能打开 |
| WebFetch 工具 | 环境变量 HTTP_PROXY/HTTPS_PROXY（空） | ❌ 直连，被拒 |
| PowerShell Invoke-WebRequest | 系统代理（自动读） | ✅ 走代理，能打开 |

---

## 4. 临时解决方案（当前可用）

改用 PowerShell 的 `Invoke-WebRequest`（它会自动读 Windows 系统代理）：

```powershell
# 抓取网页内容
$r = Invoke-WebRequest -Uri "https://code.claude.com/docs/zh-CN/sandbox-environments" -UseBasicParsing -TimeoutSec 30
$r.StatusCode          # 200
$r.Content.Length      # 368021（内容大小）

# 存成文件供后续读取
$r.Content | Out-File -FilePath "D:\...\_tmp_page.html" -Encoding utf8
```

抓到的 HTML 是 Next.js 动态渲染的密集标签页，正文内嵌其中。提取纯文本：

```powershell
$html = Get-Content "path.html" -Raw
$html = [regex]::Replace($html, '<script[\s\S]*?</script>', ' ')  # 去 script
$html = [regex]::Replace($html, '<style[\s\S]*?</style>', ' ')    # 去 style
$text = [regex]::Replace($html, '<[^>]+>', ' ')                  # 去标签
$text = [System.Net.WebUtility]::HtmlDecode($text)                # 解实体
$text = [regex]::Replace($text, '\s+', ' ')                       # 压空白
```

> ⚠️ 注意：Next.js 页面正文有的直接内嵌在 HTML（可直接提取），有的需要 JS 执行（提取不到，需换法）。本案例属于前者，正文成功提取。

---

## 5. 候选解决方案对比（4 个方案）

用户约束：**不想动全局代理配置（怕影响国内网站直连）**。

### 方案 A：记忆规则 + PowerShell 重试（最轻）

不动任何配置，只把行为写进记忆，让 Claude "记得"这件事：

> 记忆规则：**WebFetch 抓取失败（`ECONNREFUSED` 等）时，改用 PowerShell `Invoke-WebRequest`（会自动读系统代理）重试，并把结果存成文件再读取。**

| | |
|---|---|
| ✅ 优点 | 零配置、即时生效、不动系统环境变量（国内访问不受影响） |
| ❌ 缺点 | 不是"工具自动重试"，靠 Claude 记住并主动切换；每次要现写命令 |

### 方案 B：代理 fetch 脚本（复用性好，推荐）

项目里放一个可复用脚本 `scripts/fetch_via_proxy.ps1`：

```powershell
# scripts/fetch_via_proxy.ps1 <url> <output_file>
$r = Invoke-WebRequest -Uri $args[0] -UseBasicParsing -TimeoutSec 30
$r.Content | Out-File $args[1] -Encoding utf8
Write-Output "OK: $($r.StatusCode) -> $($args[1])"
```

配合方案 A 的记忆规则使用。

| | |
|---|---|
| ✅ 优点 | 命令短、可复用、可加参数（超时/header）；比方案 A 每次手写干净 |
| ❌ 缺点 | 仍是"Claude 主动调用"的 fallback，不是工具层自动重试 |

### 方案 C：环境变量 + NO_PROXY（让 WebFetch 真正自动）

如果**愿意**加环境变量，可用 `NO_PROXY` 排除国内域名，同时解决"国内直连"顾虑：

```powershell
[Environment]::SetEnvironmentVariable("HTTP_PROXY", "http://127.0.0.1:7890", "User")
[Environment]::SetEnvironmentVariable("HTTPS_PROXY", "http://127.0.0.1:7890", "User")
[Environment]::SetEnvironmentVariable("NO_PROXY", "localhost,127.0.0.1,.cn,.baidu.com,.aliyun.com,...", "User")
```

| | |
|---|---|
| ✅ 优点 | WebFetch 工具本身（如果读环境变量代理）就能自动工作，不需手动 fallback |
| ⚠️ 注意 | ① 需验证 WebFetch 是否读环境变量代理（不确定）；② `NO_PROXY` 列表要维护；③ **用户已明确"不加"**，是"以后想彻底解决"的选项 |

### 方案 D：MCP 代理工具（最正式，最重）

写一个小的 MCP server，提供 `proxy_fetch` 工具（内部用系统代理抓网页），配置进 `.mcp.json`。变成一个正式工具，Claude 可像调 WebFetch 一样自动调它。

| | |
|---|---|
| ✅ 优点 | 真正的"工具级"方案，可加"失败自动 fallback"逻辑 |
| ❌ 缺点 | 工程量大（写 MCP server + 配置）；对学习项目偏重；s19（MCP）还没学 |

### 对比速览

| 方案 | 改动 | 影响国内访问 | 自动程度 | 工程量 |
|------|------|------------|---------|--------|
| A 记忆规则 | 无（只记规则） | 无影响 | Claude 主动 fallback | 零 |
| B 脚本 + 记忆 | 加一个脚本 | 无影响 | Claude 主动调脚本 | 极小 |
| C 环境变量 + NO_PROXY | 加全局变量 | 需维护 NO_PROXY | WebFetch 自动 | 小 |
| D MCP 工具 | 写 MCP server | 无影响 | 工具级自动 | 大 |

---

## 6. 建议结论

**A + B 组合**最贴合"不动全局配置、不影响国内访问"的约束：

1. 写一条记忆规则（WebFetch 失败 → PowerShell Invoke-WebRequest 重试）
2. 建一个 `scripts/fetch_via_proxy.ps1` 复用脚本

C 适合"愿意维护 NO_PROXY 列表"时再考虑；D 适合学完 s19 MCP 后再做。

---

## 7. 一句话总结

> **WebFetch 只认环境变量代理、不读 Windows 系统代理**——这就是它在你机器上 `ECONNREFUSED` 的根因。临时方案用 `Invoke-WebRequest`（读系统代理）绕开；长效方案选"记忆规则 + 复用脚本"，既不动全局配置、也不影响国内直连。

---

## 关联阅读

- `学习记录/04-Claude Code Windows 中文环境配置指南.md` — Windows 环境配置相关
- Claude Code 官方 [network-config](https://code.claude.com/docs/zh-CN/network-config) — 企业网络代理配置

**文档生成时间：** 2026-08-07
