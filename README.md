# Voer.host 免费服务器会话续期

免费档服务器是**会话制**（默认约 4 小时），续期必须看完 **3 个 Google 激励广告**，每次成功 **+4 小时**。

本版流程：

```
关机检查 → 先重启/开机 → 等到 running
        → 读取可续期次数（今日 / 本会话）
        → 有剩余：看广告续期，每轮成功后实时打印剩余次数
        → 4 次已用完：跳过看广告，只做关机检查 + 重启
```

剩余次数一律以面板 API 为准，保证下次运行能接着续。

**平台限制：**

| 限制 | 说明 |
|------|------|
| 每 UTC 日最多 | **4 次** |
| 每个会话最多 | **4 次** |
| 每次成功 | **+4 小时** |
| 广告播放 | 必须真实播放（`headless: false` + 虚拟显示 xvfb），否则不发奖励 |

---

## 需要的环境变量

| 环境变量 | 是否必须 | 说明 |
|----------|----------|------|
| `VOER_SERVER_ID` | **必须** | 服务器 UUID。多台用英文逗号分隔 |
| `VOER_TOKEN` | **必须** | 登录 Cookie 中的 JWT |
| `TELEGRAM_BOT_TOKEN` | 可选 | Telegram 机器人 Token |
| `TELEGRAM_CHAT_ID` | 可选 | Telegram 聊天 / 群组 ID |
| `VOER_ADS_PER_EXTENSION` | 可选 | 每次需要的广告数，默认 `3` |
| `VOER_AD_DURATION_SEC` | 可选 | 单个广告等待秒数，默认 `32` |
| `VOER_EXTENSIONS_PER_RUN` | 可选 | 单次运行内连续续期几次，默认 `4` |
| `VOER_AUTO_RESTART` | 可选 | 关机后是否自动重启，默认 `1` |
| `VOER_POWER_WAIT_SEC` | 可选 | 等待开机进入 running 的秒数，默认 `240` |
| `TG_TITLE` | 可选 | 自定义 TG 通知标题 |

本地也可用 `config.json`（由 `config.example.json` 复制），环境变量优先级更高。

### 如何获取 VOER_SERVER_ID 和 VOER_TOKEN

1. 登录 [https://voer.host](https://voer.host)，打开服务器面板
2. 地址栏 `/panel/server/` 后面的 UUID → `VOER_SERVER_ID`
3. F12 → Application → Cookies → `token` 的完整 Value → `VOER_TOKEN`（约 7 天有效）

---

## 运行

```bash
# 只看状态（不看广告、不碰电源）
python3 voer_renew.py --status

# 真正执行：关机则先重启，再按剩余次数续期
xvfb-run -a python3 voer_renew.py
```

成功日志大致类似：

```text
[xx:xx:xx] 阶段 1/2：关机检查 → 重启
[xx:xx:xx] 电源检查: status=stopped
[xx:xx:xx] 服务器已关机/异常 (stopped)，先执行 start，重启完成后再进入续期
[xx:xx:xx] 重启完成 → running，接下来检查可续期次数
[xx:xx:xx] 阶段 2/2：检查可续期次数
[xx:xx:xx] 重启后可续期次数: 综合剩余 4 次 | 今日 0/4（剩余 4） | 本会话 0/4（剩余 4）
[xx:xx:xx] 第 1/4 轮：今日 0/4 | 本会话累计 0/4 | 剩余可续期 4 次
[xx:xx:xx] 第 1 轮续期成功 -> 新到期: … | 累计: 1 | 今日: 1
[xx:xx:xx] 实时可续期次数: 综合剩余 3 次 | 今日 1/4（剩余 3） | 本会话 1/4（剩余 3）
```

若 4 次已用完：

```text
[xx:xx:xx] 4 次续期已用完（今日 4/4，本会话 4/4），跳过看广告，仅检查关机/重启
⏭️ 跳过（4 次已用完，仅检查关机/重启）
```

Telegram 通知会多两行：

- `🔢可续期次数: 今日剩余 x/4 · 本会话剩余 y/4 · 综合 z 次`
- `🔌电源操作: stopped → running（start）`

---

## GitHub Actions

仓库设为 Private。Secrets：

| Name | 必须 | 说明 |
|------|------|------|
| `VOER_SERVER_ID` | 是 | 服务器 UUID |
| `VOER_TOKEN` | 是 | JWT |
| `TELEGRAM_BOT_TOKEN` | 否 | TG 通知 |
| `TELEGRAM_CHAT_ID` | 否 | TG 通知 |

工作流默认每天 UTC `00:00 / 08:00 / 16:00`。手动触发可选 `status` 或 `renew`。

想关掉自动重启：Secret / 环境变量 `VOER_AUTO_RESTART=0`。

---

## 安全

- 真实 token 只放环境变量 / GitHub Secrets / 本地 `config.json`，不要提交到 git
- token 约 7 天过期；401 时重新从浏览器复制
