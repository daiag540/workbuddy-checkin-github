# WorkBuddy 每日签到 + 派猫猫 · GitHub Actions 版

无常开设备也能每天自动签到、自动派猫猫，并把结果推到飞书。运行在 GitHub 免费 runner（云端），token 从仓库 Secret 读取，绝不进仓库或日志。

## 做了什么
- 每天定时跑一次，一次完成：签到 + 派猫猫旅行闭环。
- 派猫猫：先领已到家的旅行积分，再判断是否派新一趟；今天已派过（`daily_limit_reached`）或正在旅行（`traveling`）不派。
- 结果推飞书，签到 / 猫猫分两节写清楚。
- 猫猫失败不影响签到结论：签到成功即整体成功。

## 目录
- `checkin.py` —— 云端运行主脚本（纯标准库，无第三方依赖）。
- `.github/workflows/checkin.yml` —— 定时任务（UTC 00:00 = 北京 08:00）+ 手动触发。
- `refresh_token.py` —— 本机脚本：从桌面客户端日志自动提取最新有效令牌，校验后写入仓库 Secret `WB_TOKEN`。
- `export_secret.py` —— 兼容入口，转调 `refresh_token.py`。
- `.gitignore` —— 已忽略 `.wb_token` 等本地敏感文件。

## 接口（以客户端实际请求为准）
- 签到：`POST https://www.codebuddy.cn/v2/billing/meter/checkin-activity-status`（查）、`.../daily-checkin`（领）
- 猫猫：`https://www.workbuddy.cn/activity/growth/buddy/travel/{status,config,claim,depart}`
- 鉴权头：`Authorization: Bearer <token>`

## 取令牌 & 自动刷新（关键）

GitHub Actions 跑在云端，读不到本机登录态；而可用令牌以**明文 `Bearer eyJ...`** 形式留在 Windows 桌面客户端的日志里：
`%USERPROFILE%\.workbuddy\logs\...\workbuddyMainThread__*.log`

> 实测：新版客户端把登录态文件（`workbuddy-desktop.info`）里的 accessToken / refreshToken 都加密了，网页端也没有「Buddy 加油站」、且请求不带 `Authorization` 头，桌面端 DevTools 被禁用——**唯一稳定可取令牌的来源就是上述日志**。

用 `refresh_token.py`（在本机运行）自动提取并推送：

```bash
# 1) 先 dry-run 看看能不能取到（不写文件、不推送）
python refresh_token.py --dry-run

# 2) 写入本地 .wb_token 并推送仓库 Secret WB_TOKEN
GH_REPO=你的名/你的仓库 python refresh_token.py
# 或用 PAT 走 REST API
GH_TOKEN=ghp_xxx GH_REPO=你的名/你的仓库 python refresh_token.py

# 3) 只写本地、不推送（先本地用 checkin.py 验证）
python refresh_token.py --local-only
```

脚本会：扫描日志 → 取「最新且未过期」的 JWT（按 `exp` 选活得最久的，减少刷新频率）→ 调一次签到状态接口验证 → 写本地 `.wb_token` + 推送 `WB_TOKEN` Secret。全程只对令牌脱敏输出。

### 部署到 GitHub
1. 把本目录推到一个 GitHub 仓库。
2. 仓库 → Settings → Secrets → Actions，添加：
   - `WB_TOKEN`：用上面 `refresh_token.py` 推送（或手动粘贴 `.wb_token` 内容）。
   - `FEISHU_WEBHOOK`：飞书自定义机器人 webhook 地址。
   - 可选：`WB_DOMAIN`（默认 `www.codebuddy.cn`）、`WB_UID`、`TRAVEL_LOCATION`（1-4 指定地点）。
3. 在 Actions 页点 `Run workflow` 验收；之后每天自动跑。

### 令牌自动刷新的边界（务必看清）
- 刷新动作必须**在本机**执行（云端读不到本地日志），所以这不是 7×24 无人值守刷新。
- 令牌有效期约 **11 个月**（实测最新一条 `exp≈2027-06-18`），日常几乎不用管；到期前在本机跑一条 `refresh_token.py` 即可续期。
- 想让本机「偶尔开着就自动续期」，可用 Windows 任务计划程序每天跑一次（仅当 PC 开机时才执行）：

```powershell
# 以管理员 PowerShell 注册：每天 09:00 运行刷新（需先确保 gh 已登录 或 设好 GH_TOKEN/GH_REPO）
$py = (Get-Command python3).Source
$script = "C:\路径\到\workbuddy-checkin-github\refresh_token.py"
schtasks /create /tn "WorkBuddyTokenRefresh" /tr "`"$py`" `"$script`" --repo 你的名/你的仓库" /sc daily /st 09:00
```

## 安全
- 令牌只在环境变量里，由 Actions 自动脱敏；脚本任何输出都做脱敏（前 6 + 后 4）。
- `.wb_token`、本地登录态均不入库（见 `.gitignore`）。

## 验收
跑通后，飞书会收到类似：
```
📅 WorkBuddy 每日签到 · 2026-10-16 08:00:xx (UTC+8)

【签到】✅ 成功
  · 动作：checked_in
  · 说明：领取成功，+100 积分，连续第 3 天
  · 积分余额：1234

【派猫猫旅行】✅ 已处理
  · 状态：空闲（可派遣）
  · 地点：咖啡馆
  · 领取积分：+20
  · 明细：当前状态：空闲（可派遣）；已派出 Buddy 前往【咖啡馆】
```
