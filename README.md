# Voer.host 免费服务器会话续期

免费档服务器是**会话制**（默认约 4 小时），续期必须看完 **3 个 Google 激励广告**，每次成功 **+4 小时**。

**平台限制：**

| 限制 | 说明 |
|------|------|
| 每 UTC 日最多 | **4 次** |
| 每个会话最多 | **4 次** |
| 每次成功 | **+4 小时** |
| 广告播放 | 必须真实播放（`headless: false` + 虚拟显示 xvfb），否则不发奖励 |

**脚本核心规则：**

1. **登录**：优先 `VOER_EMAIL` + `VOER_PASSWORD`（绕过 Cloudflare Turnstile）；失败再使用 `VOER_TOKEN`  
2. **关机**：检测到 `stopped` / `offline` 时**优先开机**（必要时看开机广告）  
3. **续期**：有可续期次数 → **执行续期**；无次数 → **跳过、不报错**（TG 通知 + 截图）  
4. **次数显示**：每轮成功后实时打印可续期次数与下次续期准确时间  
5. **通知**：Telegram 文本 + 面板截图（可选）

本项目提供：

1. **本地 / VPS** 直接运行的 Python 脚本（Playwright + SeleniumBase）  
2. **GitHub Actions** 定时/手动续期（推荐）  
3. **Telegram** 续期结果 / 跳过通知 + 面板截图（可选）

---

## 目录

1. [需要的环境变量](#一需要的环境变量最重要)
2. [如何获取 VOER_SERVER_ID 和 VOER_TOKEN](#如何获取-voer_server_id-和-voer_token)
3. [邮箱密码登录（优先）](#邮箱密码登录优先)
4. [Telegram 通知 + 截图（可选）](#二telegram-通知--截图可选)
5. [GitHub Actions 自动续期](#三github-actions-自动续期推荐)
6. [本地 / VPS 直接运行](#四本地--vps-直接运行)
7. [运行逻辑说明](#五运行逻辑说明)
8. [定时任务示例](#六定时任务示例)
9. [常见问题排查](#七常见问题排查)
10. [文件说明](#八文件说明)
11. [安全建议](#九安全建议)

---

## 一、需要的环境变量（最重要）

脚本**优先读取环境变量**，没有时才读本地 `config.json`。

| 环境变量 | 是否必须 | 说明 |
|----------|----------|------|
| `VOER_SERVER_ID` | **必须** | 服务器 UUID。**支持多台**：用英文逗号分隔，如 `uuid1,uuid2`（同一账号） |
| `VOER_EMAIL` | 优先 | 登录邮箱（**优先**用邮箱密码登录） |
| `VOER_PASSWORD` | 优先 | 登录密码 |
| `VOER_TOKEN` | 回退 | Cookie 中的 JWT；**邮箱登录失败时**再使用 |
| `TELEGRAM_BOT_TOKEN` | 可选 | Telegram 机器人 Token，用于通知 |
| `TELEGRAM_CHAT_ID` | 可选 | Telegram 聊天 / 群组 ID |
| `VOER_ADS_PER_EXTENSION` | 可选 | 每次需要的广告数，默认 `3` |
| `VOER_AD_DURATION_SEC` | 可选 | 单个广告等待秒数，默认 `32` |
| `VOER_EXTENSIONS_PER_RUN` | 可选 | 单次运行内连续续期几次，默认 `4`；设 `1` 则每次只续 1 次 |
| `VOER_SKIP_RESTART` | 可选 | 设为 `1` / `true` 时不自动开机 |
| `TG_TITLE` | 可选 | 自定义 TG 通知标题，默认 `Godlike 续期通知` |
| `TG_NOTIFY_STATUS` | 可选 | 设为 `1` 时，`--status` 模式也会发 TG |

本地也可用 `config.json`（由 `config.example.json` 复制），但**环境变量优先级更高**。

> **认证要求**：至少配置 `VOER_EMAIL`+`VOER_PASSWORD`，**或** `VOER_TOKEN`（可同时配置，登录顺序为邮箱优先）。

---

### 如何获取 VOER_SERVER_ID 和 VOER_TOKEN

#### 1. 获取 `VOER_SERVER_ID`

1. 浏览器打开并登录 [https://voer.host](https://voer.host)
2. 进入你的服务器面板
3. 看地址栏，类似：

   ```text
   https://voer.host/panel/server/58d72957-xxxx-xxxx-xxxx-xxxxxxxxxxxx
   ```

4. **复制 `/panel/server/` 后面那一整串 UUID** → 这就是 `VOER_SERVER_ID`

   **多台服务器**：UUID 用英文逗号拼接。token / 邮箱是账号级，多台共用。

#### 2. 获取 `VOER_TOKEN`（可选回退）

1. 在已登录的 voer.host 页面按 **F12**
2. **Application（应用）** → **Cookies** → `https://voer.host`
3. 找到 **Name = `token`**，完整复制 **Value**（以 `eyJ` 开头）

> JWT 约 7 天有效。建议同时配置邮箱密码，登录失败时才用到 token。

---

### 邮箱密码登录（优先）

每次运行认证顺序：

1. **优先**使用 `VOER_EMAIL` + `VOER_PASSWORD`  
   - SeleniumBase UC 模式打开登录页  
   - `uc_gui_click_captcha()` 绕过 Cloudflare Turnstile  
   - 登录成功后从 Cookie 读取新 `token` 并自动写回（内存 / `config.json` / `GITHUB_ENV`）  
2. 邮箱登录失败或未配置 → **回退**使用 `VOER_TOKEN`  
3. 续期过程中若 API 返回 401/403，会再尝试邮箱登录一次  

GitHub Secrets 建议配置：

| Secret | 说明 |
|--------|------|
| `VOER_EMAIL` | 登录邮箱 |
| `VOER_PASSWORD` | 登录密码 |
| `VOER_TOKEN` | 可选回退 |

---

## 二、Telegram 通知 + 截图（可选）

配置 `TELEGRAM_BOT_TOKEN` + `TELEGRAM_CHAT_ID` 后：

| 场景 | 说明 |
|------|------|
| 续期成功 | 次数、到期时间、下次续期时间 + 截图 |
| 续期失败 / 异常 | 原因 + 调试截图 |
| 跳过（无可用次数） | 原因 + 截图，**不报错** |
| `--status` 且 `TG_NOTIFY_STATUS=1` | 状态摘要 |

### 创建 Bot

1. [@BotFather](https://t.me/BotFather) → `/newbot` → 得到 `TELEGRAM_BOT_TOKEN`  
2. 与 Bot 私聊或拉进群后访问：

   ```text
   https://api.telegram.org/bot<TOKEN>/getUpdates
   ```

3. 取 `"chat":{"id": ...}` 为 `TELEGRAM_CHAT_ID`

---

## 三、GitHub Actions 自动续期（推荐）

### 1. 推送项目文件

- `voer_renew.py`
- `requirements.txt`（`playwright`、`seleniumbase`）
- `.github/workflows/voer-renew.yml`
- `config.example.json`、`README.md`（可选）

### 2. 配置 Secrets

| Name | 是否必须 | Value |
|------|----------|--------|
| `VOER_SERVER_ID` | **必须** | 服务器 UUID（多台逗号分隔） |
| `VOER_EMAIL` | 优先 | 登录邮箱 |
| `VOER_PASSWORD` | 优先 | 登录密码 |
| `VOER_TOKEN` | 回退 | Cookie JWT |
| `TELEGRAM_BOT_TOKEN` | 可选 | TG Bot Token |
| `TELEGRAM_CHAT_ID` | 可选 | TG Chat ID |

### 3. 运行

- **手动**：Actions → **Voer.host 会话续期** → `renew` / `status`  
- **定时**：默认 UTC `0:00 / 8:00 / 16:00`（可在 workflow 中修改 `cron`）

使用 `xvfb-run` + 非 headless，保证广告真实播放。

---

## 四、本地 / VPS 直接运行

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
playwright install chromium

# 无图形界面时
sudo apt-get install -y xvfb

export VOER_SERVER_ID="你的UUID"
export VOER_EMAIL="you@example.com"
export VOER_PASSWORD="你的密码"
# export VOER_TOKEN="eyJ..."   # 可选回退

xvfb-run -a python3 voer_renew.py
python3 voer_renew.py --status
```

或复制 `config.example.json` → `config.json` 填写后运行。

---

## 五、运行逻辑说明

```text
开始
  │
  ├─ 认证
  │     ├─ 优先邮箱密码登录（Turnstile UC 绕过）→ 更新 token
  │     └─ 失败则使用 VOER_TOKEN
  │
  ├─ 预检
  │     ├─ 关机 → 标记优先开机
  │     ├─ 无续期次数且非关机 → 跳过（不报错，TG+截图）
  │     └─ 有续期次数 → 继续
  │
  ├─ 面板
  │     ├─ stopped/offline → 优先开机（API / 面板 Start + 广告）
  │     ├─ 开机后刷新次数（sessionExtensionsDate 非今日则今日已用按 0）
  │     ├─ 仍无次数 → 跳过（不报错）
  │     └─ 有次数 → 进入续期
  │
  ├─ 续期循环（最多 extensions_per_run）
  │     └─ 延伸 → 3 广告 → 校验 +4h → 实时打印剩余次数
  │
  └─ TG 通知 + 截图
```

**可续期次数：**

```text
remaining = min(4 - 今日已用, 4 - 本会话已续期)
```

今日已用结合 `sessionExtensionsDate`（非今天 UTC 则按 0）。

---

## 六、定时任务示例

```yaml
# GitHub Actions
schedule:
  - cron: "0 0,8,16 * * *"
```

```cron
# 本地 cron
0 8,20 * * * cd /path/to/voer-renew && xvfb-run -a python3 voer_renew.py >> /var/log/voer-renew.log 2>&1
```

---

## 七、常见问题排查

| 现象 | 处理 |
|------|------|
| 邮箱登录失败 | 检查账号密码；看 `login_failed.png`；确保 xvfb + 非 headless |
| 回退 token 也失败 | 重新复制 Cookie，或修好邮箱登录 |
| 开机 Ad requirement | 脚本会自动面板广告 + `adsCompleted` 再 start |
| 跳过（无可用次数） | 正常，不报错；等 UTC 日切或新会话 |
| 广告点不到 | 保持 `headless: false`；可增大 `VOER_AD_DURATION_SEC` |

```bash
python3 voer_renew.py --status
```

---

## 八、文件说明

| 路径 | 说明 |
|------|------|
| `voer_renew.py` | 主脚本 |
| `requirements.txt` | playwright、seleniumbase |
| `config.example.json` | 配置模板 |
| `.github/workflows/voer-renew.yml` | Actions 工作流 |
| `README.md` | 本说明 |

---

## 九、安全建议

1. 不要把 token / 密码提交到 git  
2. 优先用 Secrets；日志不打印完整密钥  
3. token 泄露后重新登录使旧 token 失效  
4. 建议配置邮箱密码，减少手工更新 token  

---

## 快速检查清单

- [ ] `VOER_SERVER_ID`
- [ ] `VOER_EMAIL` + `VOER_PASSWORD`（优先）和/或 `VOER_TOKEN`（回退）
- [ ] （可选）Telegram 两个 Secret
- [ ] 推送代码并先跑 `status`，再跑 `renew`
- [ ] 确认：关机先开机；有次数续期；无次数跳过不报错
