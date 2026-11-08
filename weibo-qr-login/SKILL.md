---
name: "weibo-qr-login"
title: "Weibo QR Login on a Cloud Container"
description: "需要在云端容器登录微博时，用手机扫码完成登录并保持登录态可观测，输出登录成功的浏览器会话与监控状态。"
type: skill
tags: [weibo, playwright, qr-login, cloud-container, session]
created: 2026-11-08
updated: 2026-11-08
author: scriptsmay
status: active
---

# Weibo QR Login on a Cloud Container

在没有本地浏览器的云端 Linux 容器/虚拟机上，用手机扫码登录微博，并让登录态常驻、可观测。Playwright + 系统 Chromium + 持久化浏览器 profile；登录二维码只发给账号本人，登录后每 30 分钟自查一次登录态。

Log into Weibo from a cloud Linux container/VM with no local browser: scan a QR code with the phone app, keep the session alive in a persistent browser profile, and self-check the login state every 30 minutes. Playwright + system Chromium.

> **本技能不含任何真实账号、Cookie、二维码与机器信息。** 登录二维码是登录凭证，只通过私密通道发给账号本人，扫完即删；`status.json` / `monitor.log` 含你的主页链接，不要贴进公开频道；脚本写出的全部文件都只留在你自己的机器上，**不要进仓库、不要进群聊、不要进日志分享**。
> This skill contains no real account, cookie, QR code or machine identity. A QR code is a login credential: send it only to the account owner over a private channel and delete it after the login. All files the script writes stay on your own machine and must never enter a repository, a group chat or a shared log.

<!-- 技能设计至少明确六项：名称、WHEN、WHAT、HOW、REFERENCE、LIMITS。 -->

## 适用场景与触发条件（WHEN）

- **触发场景**：需要在云端 Linux 容器/虚拟机上登录微博做自动化（发博、互动、数据抓取），但容器没有本地浏览器可操作，账号本人只能用手机配合扫码。
- **典型请求**：“在云容器上登一下微博，扫码登录，登录态要能保持、能查到在不在线。”
- **不适用情形**：
  - 账号本人不在场、10 分钟内没人能扫码并在 App 里点确认——不要启动登录流程。
  - 容器没有系统 Chromium，或访问不了 `weibo.com`。
  - 需要批量登录多个账号——本技能一次只登一个号，换号要换工作目录与 profile。
  - 想用密码/短信码登录——本技能只覆盖扫码路径。

## 目标、输入与输出（WHAT）

- **目标**：在云端容器里完成一次微博扫码登录，不依赖本地电脑、不输密码、不收短信码；登录态可持久、可验证：知道它是什么时候登上的、现在还在不在、什么时候掉的。
- **必需输入**：
  - 一台 Linux 云容器/虚拟机，已装系统 Chromium，能直接访问 `weibo.com`。
  - Python 3.11+，`pip install playwright`（不需要 `playwright install` 下载浏览器，用系统 Chromium 作 `executable_path`）。
  - 容器里可写的持久目录（示例 `$HOME/weibo-login`）；会话 profile 与日志都落在这里。
  - 一部装了微博 App 的手机 + 要登录的账号；一个**私密通道**（自己与自己的 DM、加密笔记）用来接收二维码图片。
  - 账号本人在场：扫码与 App 内“确认登录”必须本人做，agent 不能代扫。
- **可选输入 / 默认值**：

  | 占位符 / 环境变量 | 含义 | 说明 |
  |---|---|---|
  | `WB_LOGIN_DIR` | 工作目录，默认 `$HOME/weibo-login` | profile、二维码、状态与日志全落在这里；**不要指向仓库目录** |
  | `CHROMIUM_PATH` | 系统 Chromium 路径，默认 `/usr/bin/chromium` | `which chromium` 核实；不要另下浏览器 |
  | `WB_CHECK_INTERVAL_SEC` | 登录态自查间隔，默认 `1800`（30 分钟） | 与微博会话续期节奏对齐；不要设得过密 |
  | `WB_SCAN_TIMEOUT_SEC` | 等扫码的最长等待，默认 `1800`（30 分钟） | 二维码本身约 10 分钟过期，脚本会自动刷新 |
  | `WB_USER_AGENT` | 浏览器 UA（可选） | `Chrome/<major>` 与已装 Chromium 大版本对齐 |

- **输出形式**：登录成功的持久化浏览器会话（`$WB_LOGIN_DIR/profile`）+ `state.json`；`status.json`（`stage` / `login` / `last_check`）；`monitor.log`（每次自查一行）；`logged-in.jpg`（登录后首页截图，供账号本人验明身份）。
- **验收标准**：
  - `status.json` 为 `stage=monitoring`、`login=true`；`monitor.log` 有 `LOGGED_IN` 一行。
  - 登录后首页截图已发账号本人，确认账号无误。
  - 脚本已托管（nohup/tmux/systemd），终端退出后仍在跑。

## 执行流程（HOW）

执行约定（另一个 agent 按本技能执行时，不要跳步）：先确认人在场再启动；二维码只发私密通道，每次发带时间戳的新文件；全程不碰密码与 Cookie；用 DOM 判断登录态；登录成功先让账号本人验明身份；登录态丢失只报、不自救。

1. **收集并检查输入**：核实系统 Chromium 路径、持久工作目录、账号本人在场且 10 分钟内能扫码。缺任何一项就停下来，不要猜、不要先启动再等人。

2. **按步骤处理**：

   ### 2.1 准备环境

   ```bash
   python3 -m venv venv
   ./venv/bin/python -m pip install playwright
   which chromium  # 记下路径，填 CHROMIUM_PATH；没有就先装系统 Chromium
   ```

   ### 2.2 把脚本传到容器

   脚本在 `references/weibo_qr_login.py`。如果本机与容器是两台机器，用 base64 传文件，不要手抄、不要 heredoc：

   ```bash
   # 在本机：取 base64 原文
   base64 -w0 references/weibo_qr_login.py
   # 在容器：把上一步的输出原样粘贴进 <b64> 位置
   echo <b64> | base64 -d > "$WB_LOGIN_DIR/weibo_qr_login.py"
   ```

   两个教训：手抄 base64 会传坏文件；heredoc 会吞单引号导致 `SyntaxError`。另外**不要在工作目录里放 `inspect.py`**——它会遮住 Python 标准库 `inspect` 模块，任何 `import asyncio` 的脚本启动即 `SyntaxError`；启动脚本一律加 `-P`（`python -P`，safe_path）绕过。

   ### 2.3 启动登录脚本

   ```bash
   export WB_LOGIN_DIR="$HOME/weibo-login" CHROMIUM_PATH="/usr/bin/chromium"
   mkdir -p "$WB_LOGIN_DIR"
   ./venv/bin/python -P "$WB_LOGIN_DIR/weibo_qr_login.py"
   ```

   脚本先以有头（headed）模式启动持久化上下文，失败再回退无头；`user_data_dir` 是持久化 profile，登录态存在这里。跑在前台时用 `nohup` / `tmux` / `systemd` 托管，不要让它随终端退出而死。

   ### 2.4 打开登录弹窗

   脚本自动完成：点 weibo.com 首页的「登录/注册」按钮，弹出的不是模态层，**而是一个新窗口**（`passport.weibo.com/sso/signin?...&disp=popup`）。注意：

   - 普通 `.click()` 和 `openLoginLayer=1` 参数都打不开这个弹窗，必须用 `page.mouse.click()` 点按钮中心坐标；
   - 第一次点击经常不生效，脚本做最多 3 次重试，每次等 10 秒扫一遍 `ctx.pages` 找弹窗。

   ### 2.5 取码、发码、扫码

   脚本在弹窗里找二维码（`img[src*='qr.weibo.cn']`），并做方形尺寸校验（80–400 px、宽高差 < 40），否则会误中信息流里 URL 带 `qr` 的小图。截图一律 JPEG：整页 PNG 再 base64 会超输出上限被截断。

   - 二维码约 **10 分钟过期**，过期后弹窗出现「二维码错误，点击刷新」覆盖层，脚本点「点击刷新」换新码。
   - 发码必须用**带时间戳的新文件名**（`qr-<HHMMSS>.jpg`），同路径重发会被客户端缓存成旧码——实测中第二张码就是这样“刚发就失效”的。
   - 账号本人在手机微博 App 里扫码，并在 App 内点确认登录。**扫码到确认之间不要关闭弹窗。**

   ### 2.6 判断登录成功（DOM 法）

   ```python
   login_btn = page.get_by_role("button", name="登录/注册").count()
   composer = page.locator('[placeholder*="新鲜事"]').count()
   logged_in = (login_btn == 0 and composer > 0)
   ```

   两个已踩过的坑，不要再试：

   | 错误做法 | 现象 |
   |---|---|
   | 页内 `fetch("https://m.weibo.cn/api/config")` 判断登录 | 被跨域拦截（`TypeError: Failed to fetch`），永远返回未登录 |
   | `text=` 匹配“有什么新鲜事想分享给大家？” | 那是发博框的 `placeholder` 属性，不是文本内容，`text=` 永远匹配不到 |

   注意反直觉的一点：已登录的首页**没有**「登录/注册」按钮，所以“按钮找不到”本身就是登录成功的信号，不要把它当故障反复重试。登录成功后脚本保存 `state.json`、写 `status.json`（`stage=logged_in`）、截 `logged-in.jpg`，把首页截图发给账号本人验明身份。

   ### 2.7 进入监控模式

   脚本此后每 30 分钟做一次 DOM 检查，追加写 `monitor.log`、更新 `status.json`（`stage=monitoring`、`login`、`last_check`）。掉线时截 `logged-out.jpg` 并继续守望，不自动重登。

   容器生命周期会影响监控，要心里有数（以一次实测的免费容器为例，不同厂商策略不同）：闲置 30 分钟自动暂停、每轮运行有时长上限、超限后有逐级加长的冷却期。暂停会冻结机内进程（内存与进程保留），恢复后监控继续跑，但暂停期间没有心跳；所以要从容器外再做一层巡检——每 2 小时从外部唤醒容器、读 `status.json` / `monitor.log`、确认监控进程存活，登录丢失才发消息。巡检本身只读：绝不碰浏览器、绝不重新登录、绝不重启脚本。

3. **按输出规范生成结果**：登录成功后交付 `status.json` 的关键字段、监控进入的确认、`logged-in.jpg`（仅发给账号本人）；二维码图片扫完即删，不作为交付物保留。

4. **对照验收标准核对事实、完整性和格式；不确定项明确标注**：见“执行后检查清单”。登录态是否在线一律以 DOM 检查为准，不要拿 Cookie 过期时间推断；容器暂停/冷却造成的观测空白要如实标注，不要说成“一直在线”。

### 失败处理 / Troubleshooting

| 现象 | 先查什么 |
|---|---|
| 点登录按钮没反应、弹窗不出 | 是否用了鼠标坐标点击 + 3 次重试；先看 `ctx.pages` 里有没有 `passport.weibo.com/sso/signin` 的页面 |
| 二维码“刚发就失效” | 发的是不是带时间戳的新文件；旧码是否超过约 10 分钟 |
| 登录检测永远 False | 是否还在用跨域 fetch 或 `text=` 匹配 placeholder；改 DOM 法（2.6） |
| 脚本一启动就 `SyntaxError`，提到 `asyncio` | 工作目录里是不是有个 `inspect.py` 在遮标准库；改名或用 `python -P` 启动 |
| 截图传不回来 / 内容截断 | 改 JPEG quality 60；不要传 PNG 的 base64 |
| 登录后不久就掉线 | 浏览器是不是被关了（关约 1 小时会话即失效）；检查脚本进程与容器暂停记录 |
| 容器巡检返回 409/冷却 | 这是容器在冷却期，把失败本身记一条观测就结束，不要重试、不要发消息 |

## 参考范例（REFERENCE）

- **参考来源**：2026-11-08 云容器 + 测试号扫码登录实测（脚本 `references/weibo_qr_login.py` 即本次实测的第 8 版守护脚本通用化后所得）；微博登录页 `https://weibo.com/`，登录弹窗 `https://passport.weibo.com/sso/signin`；Playwright 官方文档：Persistent context（`launch_persistent_context`）与 `locator` 选择器。
- **值得沿用**：DOM 登录检测（无登录按钮 + 有 `placeholder*="新鲜事"`）；鼠标坐标点击登录按钮 + 3 次重试；二维码方形尺寸校验；带时间戳的二维码文件名；JPEG 截图；`python -P` 启动；base64 传文件（先取原文再粘贴）。
- **应避免**：页内跨域 fetch 判断登录；`text=` 匹配 placeholder；手抄 base64 传文件；heredoc 传脚本；工作目录放 `inspect.py`；同路径重发二维码；把 `status.json` / `monitor.log` / 二维码贴进公开频道。

## 能力边界与安全约束（LIMITS）

- **不得**：向用户索要微博密码；把 Cookie 值、二维码、账号 UID、主页链接贴进聊天记录、日志或报告；把脚本产物（profile、`state.json`、`status.json`、`monitor.log`、二维码图）提交进任何仓库；登录丢失后自动重新登录或重新发码。
- **信息不足或过期时**：二维码过期（约 10 分钟）由脚本点「点击刷新」换新码；无人值守时不要启动登录；登录态是否在线以 DOM 检查为准，Cookie 过期时间不能作为登录态证据。
- **涉及重要决策 / 外部操作时**：登录丢失只记录时间点并通知账号本人，等本人决定是否重登；登录成功必须由账号本人看首页截图确认账号无误，agent 不自行认定身份；换号、换机器时换工作目录与 profile，不要复用。

## 执行后检查清单

- [ ] 输入与前提已核实；缺失信息未被臆造。
- [ ] 结果符合目标、输出格式和验收标准。
- [ ] 事实、不确定性、过期信息与待确认事项已清楚区分。
- [ ] 未越过能力边界；需用户确认的事项已停下并说明。
- [ ] 系统 Chromium 路径已核实，`CHROMIUM_PATH` 指向它。
- [ ] 工作目录在持久盘上，不在仓库里；目录里没有 `inspect.py`。
- [ ] 二维码发的是带时间戳的新文件，走私密通道；账号本人已扫码并在 App 内确认。
- [ ] 登录后首页截图已发账号本人，确认账号无误。
- [ ] `status.json` 为 `stage=monitoring`、`login=true`；`monitor.log` 有 `LOGGED_IN` 一行。
- [ ] 脚本已托管（nohup/tmux/systemd），终端退出后仍在跑。
- [ ] 容器外巡检已建：每 2 小时唤醒一次，登录丢失才报警；平时安静。
- [ ] 二维码图片已删除；`status.json` / `monitor.log` 没进过任何公开渠道。

## 实际使用案例

- 日期：2026-11-08
  - 场景 / 输入：云容器 + 测试号扫码登录；Playwright 1.64 + 系统 Chromium；账号本人在场配合扫码。
  - 结果：第一张码约 10 分钟过期、第二张疑似缓存旧图失效，第三张码扫码 + App 内确认后登录成功；登录检测最初两次方法全错（跨域 fetch、placeholder 的 `text=` 匹配），改 DOM 法后确认。登录后进入 30 分钟自查监控，容器外每 2 小时巡检一次；机器在凌晨闲置 30 分钟后自动暂停，外部 exec 唤醒成功、无冷却失败，监控进程存活、登录态保持。观测窗口约 20 小时（登录约 01:57 至当晚 22:00 收尾）。
  - 后续改进：登录检测改为 DOM 法已固化进脚本；发码改为带时间戳的新文件名已固化；容器外巡检从 02:00 收尾改为 22:00（账号本人凌晨要睡觉），观测窗口如实记为约 20 小时、不凑 24 小时。
- 日期：2026-11-08（同一次观测）
  - 场景 / 输入：会话 Cookie 寿命观测。
  - 结果：`.weibo.com` 下 `SUB`/`SUBP` 约一年、`WBPSESS` 约 24 小时、`ALF` 约 30 天。Cookie 长命不等于服务端不踢人——是否在线一律以 DOM 检查为准。
  - 后续改进：无，结论已写进 LIMITS。
- 日期：2026-11-08 之前（既往经验）
  - 场景 / 输入：同一批微博自动化（wb-bot）的登录态维持：常驻浏览器 + 每 30 分钟心跳续期。
  - 结果：浏览器关掉约 1 小时会话即失效、只能重扫码；这是本技能要求脚本常驻、容器外再巡检一层的原因。
  - 后续改进：无，结论已写进 WHY 与 HOW。

## 参考 / References

- 本仓库模板：`references/weibo_qr_login.py`（登录 + 监控一体脚本，环境变量见文件头注释）
- Playwright 官方文档：Persistent context（`launch_persistent_context`）与 `locator` 选择器
- 微博登录页：`https://weibo.com/`，登录弹窗 `https://passport.weibo.com/sso/signin`
