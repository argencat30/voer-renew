#!/usr/bin/env python3
"""
Voer.host 免费服务器会话续期（Playwright 版）

原理：免费档会话制（默认 4h），续期需看完 3 个 Google 激励广告 -> +4h。
按钮在跨进程 iframe（wormies.voer.host / googleads.g.doubleclick.net）里，
必须用 Playwright（原生支持 OOPIF）才能点到，Selenium/JS 无法穿透。

限制：每 UTC 日最多 4 次、每会话最多 4 次（每次 +4h）。

优先读取环境变量（适合 GitHub Actions / Docker / cron）：
    VOER_SERVER_ID        服务器 UUID（必须）
    VOER_TOKEN            Cookie 里的 token JWT（必须）
    TELEGRAM_BOT_TOKEN    Telegram Bot Token（可选，用于通知）
    TELEGRAM_CHAT_ID      Telegram Chat ID（可选，用于通知）

也支持本地 config.json（环境变量优先级更高）。

VPS / CI 无图形界面时必须用虚拟显示：
    xvfb-run -a python3 voer_renew.py

用法：
    python3 voer_renew.py            自动续期一次（3 个广告，约 3 分钟）
    python3 voer_renew.py --status   只看当前状态，不看广告
"""
import json
import os
import sys
import time
import pathlib
import urllib.request
import urllib.error
import urllib.parse
import base64
import mimetypes
import re
from datetime import datetime, timezone, timedelta

BASE = pathlib.Path(__file__).resolve().parent
CONFIG_PATH = BASE / "config.json"
UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/153.0.0.0 Safari/537.36"
)

DEFAULT_CONFIG = {
    "server_id": "在这里填服务器 UUID（面板地址 /panel/server/ 后面那串）",
    "token": "在这里填浏览器 Cookie 里 voer.host 的 token 值（JWT）",
    "ads_per_extension": 3,
    "ad_duration_sec": 32,
    "extensions_per_run": 4,   # 单次运行内最多连续续期几次（受平台每日/每会话 4 次上限约束）
    "headless": False,
    "use_system_chrome": False,
    "telegram_bot_token": "",
    "telegram_chat_id": "",
    "tg_title": "Godlike 续期通知",   # TG 通知标题，可用环境变量 TG_TITLE 覆盖
}

from playwright.sync_api import sync_playwright


def log(*a):
    print(f"[{time.strftime('%H:%M:%S')}]", *a, flush=True)


# ---------------------------------------------------------------------------
# Telegram 通知
# ---------------------------------------------------------------------------
def _tg_enabled(cfg) -> bool:
    return bool(cfg.get("telegram_bot_token") and cfg.get("telegram_chat_id"))


def tg_send_message(cfg, text: str) -> bool:
    """发送纯文本消息到 Telegram。"""
    if not _tg_enabled(cfg):
        return False
    token = cfg["telegram_bot_token"]
    chat_id = cfg["telegram_chat_id"]
    api = f"https://api.telegram.org/bot{token}/sendMessage"
    body = urllib.parse.urlencode(
        {
            "chat_id": chat_id,
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": "true",
        }
    ).encode()
    req = urllib.request.Request(
        api,
        data=body,
        headers={"Content-Type": "application/x-www-form-urlencoded", "User-Agent": UA},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            data = json.loads(r.read().decode())
            if data.get("ok"):
                log("Telegram 文本通知已发送")
                return True
            log(f"Telegram 发送失败: {data}")
            return False
    except Exception as e:
        log(f"Telegram 发送异常: {e}")
        return False


def tg_send_photo(cfg, photo_path: pathlib.Path, caption: str = "") -> bool:
    """发送图片（截图）到 Telegram。"""
    if not _tg_enabled(cfg):
        return False
    if not photo_path.exists():
        log(f"截图不存在，跳过发图: {photo_path}")
        return False
    token = cfg["telegram_bot_token"]
    chat_id = str(cfg["telegram_chat_id"])
    api = f"https://api.telegram.org/bot{token}/sendPhoto"

    boundary = f"----VoerBoundary{int(time.time())}"
    filename = photo_path.name
    file_data = photo_path.read_bytes()
    mime = mimetypes.guess_type(filename)[0] or "image/png"

    parts = []
    # chat_id
    parts.append(
        f"--{boundary}\r\n"
        f'Content-Disposition: form-data; name="chat_id"\r\n\r\n'
        f"{chat_id}\r\n".encode()
    )
    # caption
    if caption:
        parts.append(
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="caption"\r\n\r\n'
            f"{caption}\r\n".encode()
        )
        parts.append(
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="parse_mode"\r\n\r\n'
            f"HTML\r\n".encode()
        )
    # photo
    parts.append(
        f"--{boundary}\r\n"
        f'Content-Disposition: form-data; name="photo"; filename="{filename}"\r\n'
        f"Content-Type: {mime}\r\n\r\n".encode()
        + file_data
        + b"\r\n"
    )
    parts.append(f"--{boundary}--\r\n".encode())
    body = b"".join(parts)

    req = urllib.request.Request(
        api,
        data=body,
        headers={
            "Content-Type": f"multipart/form-data; boundary={boundary}",
            "User-Agent": UA,
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            data = json.loads(r.read().decode())
            if data.get("ok"):
                log("Telegram 截图已发送")
                return True
            log(f"Telegram 发图失败: {data}")
            return False
    except Exception as e:
        log(f"Telegram 发图异常: {e}")
        return False


def notify(cfg, title: str, lines: list, photo: pathlib.Path | None = None):
    """统一通知入口：有 TG 配置就发，没有就只打日志。"""
    text = f"<b>{title}</b>\n" + "\n".join(lines)
    log("通知内容:\n" + text.replace("<b>", "").replace("</b>", ""))
    if not _tg_enabled(cfg):
        log("未配置 TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID，跳过 TG 通知")
        return
    if photo and photo.exists():
        # 图片 caption 最长约 1024，超长则先发图再发文字
        if len(text) <= 1000:
            tg_send_photo(cfg, photo, caption=text)
        else:
            tg_send_photo(cfg, photo, caption=title)
            tg_send_message(cfg, text)
    else:
        tg_send_message(cfg, text)


# ---------------------------------------------------------------------------
# 通知内容格式化（Godlike 风格）
# ---------------------------------------------------------------------------
def fmt_local_time(dt=None) -> str:
    """本地时间，格式 2026-09-14 11:10:00。"""
    return (dt or datetime.now()).strftime("%Y-%m-%d %H:%M:%S")


def fmt_duration(seconds) -> str:
    """把秒数格式化成 23h 59m / 3h 05m。"""
    if seconds is None:
        return "—"
    s = max(0, int(seconds))
    h, rem = divmod(s, 3600)
    m = rem // 60
    return f"{h}h {m:02d}m"


def seconds_until(iso_str) -> int | None:
    """距离某个 ISO 时间还有多少秒（已过期返回 0，解析失败返回 None）。"""
    if not iso_str:
        return None
    try:
        t = str(iso_str).strip().replace("Z", "+00:00")
        dt = datetime.fromisoformat(t)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return max(0, int((dt - datetime.now(timezone.utc)).total_seconds()))
    except Exception:
        return None


def status_text(status) -> str:
    """把服务器 status 映射成中文说明。"""
    s = (status or "").lower()
    if s in ("running", "online"):
        return "✅ 服务器已在运行中，无需开机"
    if s in ("stopped", "offline"):
        return "⏹️ 服务器已停止"
    if s in ("starting", "provisioning", "pending", "starting_node"):
        return "🔄 服务器启动中"
    if s in ("restarting", "migrating"):
        return "🔄 服务器重启中"
    if s == "maintenance":
        return "🛠️ 系统维护中"
    if s in ("crashed", "error", "provisioning_error", "supervisor_error"):
        return "❌ 服务器异常"
    return f"ℹ️ {status or '未知'}"


def account_email_from_server(server) -> str:
    """从服务器信息里取账号邮箱（server.access.owner.email）。"""
    if not server:
        return ""
    owner = (server.get("access") or {}).get("owner") or {}
    if isinstance(owner, dict):
        for k in ("email", "displayName", "username"):
            if owner.get(k):
                return str(owner[k])
    for k in ("ownerEmail", "email"):
        if server.get(k):
            return str(server[k])
    return ""


def fetch_account_email(cfg) -> str:
    """调用 /api/auth/me 取账号邮箱（失败不影响续期）。"""
    req = urllib.request.Request(
        "https://voer.host/api/auth/me",
        headers={
            "Cookie": f"token={cfg['token']}",
            "User-Agent": UA,
            "Accept": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            data = json.loads(r.read().decode())
        user = data.get("user") or {}
        return str(
            user.get("email") or user.get("displayName") or user.get("username") or ""
        )
    except Exception as e:
        log(f"获取账号信息失败（不影响续期）: {e}")
        return ""


def notify_godlike(cfg, account, server_id, result, uptime_sec, status):
    """按固定模板发送续期通知（纯文本）。"""
    lines = [
        f"⏰运行时间: {fmt_local_time()}",
        f"🖥️账号: {account or '—'}",
        f"🖥️服务器: {server_id}",
        f"🔢下次可续期: {fmt_duration(uptime_sec)}",
        f"📊续期结果: {result}",
        f"📊开机状态: {status_text(status)}",
    ]
    notify(cfg, cfg.get("tg_title") or "Godlike 续期通知", lines)


# ---------------------------------------------------------------------------
# 配置
# ---------------------------------------------------------------------------
def _jwt_hint(token: str) -> str:
    t = (token or "").strip()
    if not t:
        return "空"
    parts = t.split(".")
    hint = f"长度={len(t)}, 段数={len(parts)}, 开头={t[:8]}..., 结尾=...{t[-6:]}"
    if len(parts) != 3:
        hint += "  【警告：标准 JWT 应有 3 段用 . 分隔，可能复制不完整】"
    if not t.startswith("eyJ"):
        hint += "  【警告：正常 JWT 一般以 eyJ 开头】"
    try:
        if len(parts) >= 2:
            pad = parts[1] + "=" * (-len(parts[1]) % 4)
            payload = json.loads(base64.urlsafe_b64decode(pad))
            exp = payload.get("exp")
            if exp:
                import datetime

                exp_dt = datetime.datetime.utcfromtimestamp(exp)
                now = datetime.datetime.utcnow()
                if exp_dt < now:
                    hint += f"  【已过期！过期时间 UTC {exp_dt.isoformat()}Z】"
                else:
                    left = exp_dt - now
                    hours = int(left.total_seconds() // 3600)
                    hint += f"  【未过期，剩余约 {hours} 小时，过期 UTC {exp_dt.isoformat()}Z】"
    except Exception:
        pass
    return hint


def today_used(server: dict) -> int:
    """返回「今日（UTC）已续期次数」。

    注意：API 里的 sessionExtensionsToday 是「上次记录时」的当日次数，
    必须配合 sessionExtensionsDate 判断是否属于今天（UTC）。
    日期不匹配时它已过期，应视为 0。
    这与 voer.host 前端逻辑一致：
        F = (String(sessionExtensionsDate).slice(0,10) === todayUTC) ? sessionExtensionsToday : 0
    旧脚本只看 sessionExtensionsToday，跨 UTC 日会读到过期值（如 4）而误判「今日已满」。
    """
    import datetime

    raw = server.get("sessionExtensionsToday") or 0
    try:
        raw = int(raw)
    except Exception:
        raw = 0
    date_val = server.get("sessionExtensionsDate")
    if not date_val:
        # 没有日期字段时无法确认是否属于今天：保守返回 0，宁可尝试续期也不误跳过
        return 0
    try:
        today = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d")
        if str(date_val)[:10] == today:
            return max(0, raw)
        return 0
    except Exception:
        return 0


def load_config():
    cfg = dict(DEFAULT_CONFIG)

    if CONFIG_PATH.exists():
        try:
            file_cfg = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
            cfg.update(file_cfg)
        except Exception as e:
            log(f"读取 config.json 失败: {e}")

    env_sid = os.environ.get("VOER_SERVER_ID", "").strip().strip('"').strip("'")
    env_token = os.environ.get("VOER_TOKEN", "").strip().strip('"').strip("'")
    if env_sid:
        cfg["server_id"] = env_sid
    if env_token:
        cfg["token"] = env_token

    # Telegram（环境变量优先）
    env_tg_token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip().strip('"').strip("'")
    env_tg_chat = os.environ.get("TELEGRAM_CHAT_ID", "").strip().strip('"').strip("'")
    if env_tg_token:
        cfg["telegram_bot_token"] = env_tg_token
    if env_tg_chat:
        cfg["telegram_chat_id"] = env_tg_chat

    if os.environ.get("VOER_ADS_PER_EXTENSION"):
        cfg["ads_per_extension"] = int(os.environ["VOER_ADS_PER_EXTENSION"])
    if os.environ.get("VOER_AD_DURATION_SEC"):
        cfg["ad_duration_sec"] = int(os.environ["VOER_AD_DURATION_SEC"])
    if os.environ.get("VOER_EXTENSIONS_PER_RUN"):
        cfg["extensions_per_run"] = int(os.environ["VOER_EXTENSIONS_PER_RUN"])
    env_tg_title = os.environ.get("TG_TITLE", "").strip().strip('"').strip("'")
    if env_tg_title:
        cfg["tg_title"] = env_tg_title

    sid = cfg.get("server_id", "")
    token = cfg.get("token", "")
    if not sid or "在这里填" in sid or not token or "在这里填" in token:
        log("=" * 60)
        log("缺少必要配置！请设置 VOER_SERVER_ID 和 VOER_TOKEN")
        log("可选：TELEGRAM_BOT_TOKEN + TELEGRAM_CHAT_ID 用于通知")
        log("=" * 60)
        sys.exit(1)

    # 多服务器支持：VOER_SERVER_ID / server_id 可填多个 UUID，
    # 用逗号、分号或空白分隔（同一账号的 token 对所有服务器通用）
    ids = [x for x in re.split(r"[,;\s]+", sid.strip()) if x]
    seen = set()
    server_ids = []
    for x in ids:
        if x not in seen:
            seen.add(x)
            server_ids.append(x)
    cfg["server_ids"] = server_ids

    log(f"server_id 数量={len(server_ids)}")
    for x in server_ids:
        log(f"  - {x[:8]}…")
    log(f"token 诊断: {_jwt_hint(token)}")
    # 诊断环境变量是否真正传入（不打印完整 secret）
    raw_tg_t = os.environ.get("TELEGRAM_BOT_TOKEN")
    raw_tg_c = os.environ.get("TELEGRAM_CHAT_ID")
    log(
        f"环境变量探测: TELEGRAM_BOT_TOKEN={'已设置 len='+str(len(raw_tg_t)) if raw_tg_t else '空/未传入'}, "
        f"TELEGRAM_CHAT_ID={'已设置 len='+str(len(raw_tg_c)) if raw_tg_c else '空/未传入'}"
    )
    if _tg_enabled(cfg):
        log("Telegram 通知: 已启用")
    else:
        log("Telegram 通知: 未配置（需同时设置 TELEGRAM_BOT_TOKEN + TELEGRAM_CHAT_ID）")
        log("  GitHub: Settings → Secrets and variables → Actions")
        log("  名称必须一字不差：TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID")
        log("  并确保仓库里的 .github/workflows/voer-renew.yml 已更新（会把 secrets 注入 env）")
    if os.environ.get("VOER_SERVER_ID"):
        log("配置来源: 环境变量")
    elif CONFIG_PATH.exists():
        log("配置来源: 本地 config.json")

    return cfg


def api_state(cfg, server_id=None):
    sid = server_id or cfg["server_id"]
    url = f"https://voer.host/api/servers/{sid}"
    req = urllib.request.Request(
        url,
        headers={
            "Cookie": f"token={cfg['token']}",
            "User-Agent": UA,
            "Accept": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            data = json.loads(r.read().decode())
            if "server" not in data:
                raise RuntimeError(f"API 返回格式异常: {list(data.keys())}")
            return data["server"]
    except urllib.error.HTTPError as e:
        body = ""
        try:
            body = e.read().decode(errors="replace")[:300]
        except Exception:
            pass
        log("=" * 60)
        log(f"API 请求失败: HTTP {e.code} {e.reason}")
        log(f"请求地址: {url}")
        if body:
            log(f"响应内容: {body}")
        if e.code in (401, 403):
            log("")
            log("【401/403】token 过期或错误，请重新从浏览器复制 VOER_TOKEN")
            log(f"当前 token 诊断: {_jwt_hint(cfg['token'])}")
        log("=" * 60)
        raise SystemExit(1) from e
    except urllib.error.URLError as e:
        log(f"网络错误: {e.reason}")
        raise SystemExit(1) from e


def click_anywhere(page, texts, timeout_ms, exact=True, force=False, prefer_ad_frames=False):
    """在所有 frame 查找并点击。prefer_ad_frames=True 时优先 wormies/googleads。"""
    deadline = time.time() + timeout_ms / 1000
    while time.time() < deadline:
        frames = list(page.frames)
        if prefer_ad_frames:
            def _score(fr):
                u = (fr.url or "").lower()
                if "wormies" in u:
                    return 0
                if any(k in u for k in ("googleads", "doubleclick", "pagead", "voer-ads")):
                    return 1
                if "voer.host/panel" in u:
                    return 3
                return 2
            frames = sorted(frames, key=_score)
        for frame in frames:
            for t in texts:
                makers = [
                    lambda t=t, f=frame: f.get_by_role("button", name=t, exact=exact).first,
                    lambda t=t, f=frame: f.get_by_text(t, exact=exact).first,
                    lambda t=t, f=frame: f.get_by_role("button", name=t, exact=False).first,
                    lambda t=t, f=frame: f.get_by_text(t, exact=False).first,
                    lambda t=t, f=frame: f.locator(f"button:has-text('{t}')").first,
                    lambda t=t, f=frame: f.locator(f"[role=button]:has-text('{t}')").first,
                    lambda t=t, f=frame: f.locator(f"div:has-text('{t}')").first,
                    lambda t=t, f=frame: f.locator(f"text={t}").first,
                ]
                for maker in makers:
                    try:
                        loc = maker()
                        if not loc.count():
                            continue
                        try:
                            visible = loc.is_visible()
                        except Exception:
                            visible = False
                        if not (visible or force):
                            continue
                        try:
                            loc.click(timeout=4000, force=force)
                        except Exception:
                            try:
                                loc.evaluate("el => el.click()")
                            except Exception:
                                continue
                        return f"{t}@{(frame.url or '')[:70]}"
                    except Exception:
                        pass
        time.sleep(1.0)
    return None


def _shot_name(base: str) -> str:
    """多服务器时给截图文件名加编号后缀：renew_screenshot.png -> renew_screenshot_2.png"""
    tag = os.environ.get("VOER_SHOT_TAG", "1")
    if tag == "1":
        return base
    p = pathlib.Path(base)
    return f"{p.stem}_{tag}{p.suffix}"


def take_screenshot(page, name="screenshot.png") -> pathlib.Path:
    path = pathlib.Path(_shot_name(name))
    try:
        page.screenshot(path=str(path), full_page=True)
        log(f"截图已保存: {path.resolve()}")
    except Exception as e:
        log(f"截图失败: {e}")
    return path


def dump_page_debug(page, tag="debug"):
    log(f"----- 页面诊断 ({tag}) -----")
    log(f"URL: {page.url}")
    try:
        log(f"Title: {page.title()}")
    except Exception:
        pass
    texts = []
    try:
        for frame in page.frames:
            for role in ("button", "link"):
                try:
                    for loc in frame.get_by_role(role).all()[:40]:
                        try:
                            if loc.is_visible():
                                t = (loc.inner_text(timeout=500) or "").strip()
                                if t and t not in texts:
                                    texts.append(t)
                        except Exception:
                            pass
                except Exception:
                    pass
    except Exception as e:
        log(f"收集按钮失败: {e}")
    if texts:
        log("可见按钮/链接文字:")
        for t in texts[:50]:
            log(f"  - {t!r}")
    else:
        log("未收集到可见按钮文字")
    take_screenshot(page, "debug_screenshot.png")
    log("----- 诊断结束 -----")


def run_server(cfg, server_id, account=""):
    """对单台服务器执行一次完整续期流程。返回 True=成功，False/抛异常=失败。"""
    url = f"https://voer.host/panel/server/{server_id}"
    short_id = server_id[:8] + "…"

    if "--status" in sys.argv:
        s = api_state(cfg, server_id)
        for k in (
            "status",
            "sessionExpiresAt",
            "sessionExtensions",
            "sessionExtensionsToday",
            "sessionExtensionsDate",
            "sessionDuration",
            "adsWatched",
        ):
            print(f"{k} = {s.get(k)}")
        print(f"今日已续期(UTC, 计算值) = {today_used(s)} / 4")
        # status 也可发一条简短通知（可选）
        if os.environ.get("TG_NOTIFY_STATUS") == "1":
            notify(
                cfg,
                "📊 Voer 状态查询",
                [
                    f"服务器: <code>{short_id}</code>",
                    f"状态: {s.get('status')}",
                    f"到期: {s.get('sessionExpiresAt')}",
                    f"累计续期: {s.get('sessionExtensions')}",
                    f"今日续期: {today_used(s)} / 4 (UTC)",
                ],
            )
        return True

    success = False
    before = {}
    now = {}
    last_shot = pathlib.Path(_shot_name("renew_screenshot.png"))
    # 单次运行内最多连续续期几次（受平台每日 4 次 / 每会话 4 次上限约束）
    max_ext = max(1, int(cfg.get("extensions_per_run", 4)))
    rounds_ok = 0
    stop_reason = ""

    watch_labels = [
        "觀看廣告",
        "观看广告",
        "Watch ad",
        "Watch Ad",
        "Watch ads",
        "Watch Ads",
        "Watch",
        "开始",
        "開始",
    ]
    extend_labels = [
        "延伸",
        "延长",
        "延長",
        "续期",
        "續期",
        "Extend",
        "Extend session",
        "Extend Session",
        "Renew",
        "Watch ads",
        "Watch Ads",
    ]

    with sync_playwright() as p:
        launch = dict(
            headless=cfg["headless"],
            args=[
                "--disable-blink-features=AutomationControlled",
                "--window-size=1400,1000",
                "--no-sandbox",
                "--disable-dev-shm-usage",
            ],
        )
        if cfg["use_system_chrome"]:
            launch["channel"] = "chrome"
        browser = p.chromium.launch(**launch)
        ctx = browser.new_context(viewport={"width": 1400, "height": 1000})
        ctx.add_cookies(
            [
                {
                    "name": "token",
                    "value": cfg["token"],
                    "domain": "voer.host",
                    "path": "/",
                    "secure": True,
                }
            ]
        )
        page = ctx.new_page()
        try:
            log(f"打开页面: {url}")
            page.goto(url, wait_until="domcontentloaded", timeout=60000)
            try:
                page.wait_for_load_state("networkidle", timeout=15000)
            except Exception:
                pass
            page.wait_for_timeout(5000)

            before = api_state(cfg, server_id)
            log(
                "当前到期:",
                before.get("sessionExpiresAt"),
                "| 已续期:",
                before.get("sessionExtensions"),
                "| 今日:",
                before.get("sessionExtensionsToday"),
                f"(sessionExtensionsDate={before.get('sessionExtensionsDate')})",
            )

            # 今日已达上限时面板会隐藏续期入口，直接跳过（不算失败，不浪费搜索时间）
            # 返回 None 表示「跳过」，区别于 True=成功 / False=失败
            #
            # 重要：必须用 today_used() 结合 sessionExtensionsDate 判断，
            # 否则跨 UTC 日会读到过期的 sessionExtensionsToday（例如 4）而误判「今日已满」。
            used_today = today_used(before)
            if used_today >= 4:
                log(f"今日续期次数已满（{used_today}/4，UTC 当日），平台已隐藏续期入口，跳过该服务器")
                return None
            if int(before.get("sessionExtensionsToday") or 0) >= 4 and used_today == 0:
                log(
                    "注意：sessionExtensionsToday="
                    f"{before.get('sessionExtensionsToday')} 但 sessionExtensionsDate="
                    f"{before.get('sessionExtensionsDate')} 不是今天（UTC），"
                    "该计数已过期，按 0 处理，继续尝试续期"
                )

            for accept_txt in ("Accept", "Accept all", "同意", "接受", "I agree", "OK"):
                hit = click_anywhere(page, [accept_txt], 3000)
                if hit:
                    log(f"已点同意弹窗: {hit}")
                    break

            # ===== 单次运行内连续续期：每轮 = 点延伸 + 看 3 个广告 + 验证 +4h =====
            for round_no in range(1, max_ext + 1):
                cur = api_state(cfg, server_id)
                used_today = today_used(cur)
                session_ext = int(cur.get("sessionExtensions") or 0)
                log("-" * 60)
                log(
                    f"第 {round_no}/{max_ext} 轮：今日 {used_today}/4 | 本会话累计 {session_ext}/4 "
                    f"| 到期 {cur.get('sessionExpiresAt')}"
                )
                if used_today >= 4:
                    stop_reason = f"今日已达上限（{used_today}/4）"
                    log(f"到达平台限制：{stop_reason}，停止续期")
                    break
                if session_ext >= 4:
                    stop_reason = f"本会话已达上限（{session_ext}/4）"
                    log(f"到达平台限制：{stop_reason}，停止续期")
                    break

                # 点「延伸 / Extend」
                log("正在寻找「续期/延伸」按钮…")
                hit = click_anywhere(page, extend_labels, 45000)
                if not hit:
                    page.wait_for_timeout(5000)
                    # 弹窗（Cookie/公告）可能中途弹出挡住按钮，再点一次
                    for accept_txt in ("Accept", "Accept all", "同意", "接受", "OK"):
                        if click_anywhere(page, [accept_txt], 2000):
                            log(f"再次点掉弹窗: {accept_txt}")
                    hit = click_anywhere(page, extend_labels, 30000, exact=False)
                entered_direct = False
                if not hit:
                    # 部分版本面板没有「延伸」入口，续期入口就是 Watch ad 按钮本身
                    log("未找到「延伸」入口，尝试直接点击 Watch ad…")
                    hit2 = click_anywhere(page, watch_labels, 30000) or click_anywhere(
                        page, watch_labels, 15000, exact=False
                    )
                    if hit2:
                        log(f"已直接点击 Watch ad 作为续期入口: {hit2}")
                        hit = "Watch ad(直入)"
                        entered_direct = True
                if not hit:
                    log("未找到续期入口按钮")
                    if round_no == 1:
                        dump_page_debug(page, "找不到延伸按钮")
                        notify(
                            cfg,
                            "❌ Voer 续期失败",
                            [
                                f"服务器: <code>{short_id}</code>",
                                "原因: 未找到「延伸/续期」按钮",
                                "请查看 Actions 日志或 debug 截图",
                            ],
                            photo=pathlib.Path(_shot_name("debug_screenshot.png")),
                        )
                        return False
                    stop_reason = "找不到「延伸/续期」按钮"
                    break
                log(f"已点击续期入口: {hit}")
                page.wait_for_timeout(3000)

                if not entered_direct:
                    hit2 = click_anywhere(page, watch_labels, 30000)
                    if not hit2:
                        hit2 = click_anywhere(page, watch_labels, 20000, exact=False)
                    if not hit2:
                        log("未找到「观看广告」按钮（可能已直接进入广告流程）")
                    else:
                        log(f"已点击观看广告: {hit2}")
                log("已打开广告流程，等待 Ad ready…")
                # 等 Ad ready / Watch ad 文案出现（wormies 可能慢加载）
                ready_deadline = time.time() + 45
                while time.time() < ready_deadline:
                    found_ready = False
                    for fr in page.frames:
                        for t in ("Ad ready", "Watch ad", "Rewarded ad", "觀看廣告"):
                            try:
                                loc = fr.get_by_text(t, exact=False).first
                                if loc.count() and loc.is_visible():
                                    log(f"检测到广告界面: {t!r} @ {(fr.url or '')[:60]}")
                                    found_ready = True
                                    break
                            except Exception:
                                pass
                        if found_ready:
                            break
                    if found_ready:
                        break
                    time.sleep(1.2)
                else:
                    log("未在限时内看到 Ad ready，仍继续尝试点 Watch ad")

                total = int(cfg["ads_per_extension"])
                ad_labels = ["Watch ad", "Watch Ad", "觀看廣告", "观看广告"]
                for i in range(1, total + 1):
                    # 优先在 wormies / 广告 iframe 点 Watch ad
                    hit = click_anywhere(
                        page, ad_labels, 60000, exact=False, prefer_ad_frames=True
                    )
                    if not hit:
                        hit = click_anywhere(
                            page, ad_labels, 30000, exact=False, force=True, prefer_ad_frames=True
                        )
                    if not hit:
                        hit = click_anywhere(page, ad_labels, 20000, exact=False, force=True)
                    if not hit:
                        log(f"第 {i} 个 Watch ad 未找到，停止")
                        try:
                            log(f"当前 frames={len(page.frames)}")
                            for fr in page.frames[:12]:
                                log(f"  frame: {(fr.url or '')[:100]}")
                        except Exception:
                            pass
                        break
                    log(f"已点击第 {i}/{total} 个 Watch ad（{hit}），播放中…")
                    page.wait_for_timeout(int(cfg["ad_duration_sec"]) * 1000)
                    # Close 优先广告帧，避免关掉主弹窗
                    closed = click_anywhere(
                        page,
                        ["Close", "關閉", "关闭", "Close ad"],
                        45000,
                        exact=False,
                        prefer_ad_frames=True,
                    )
                    if not closed:
                        closed = click_anywhere(
                            page, ["Close", "關閉", "关闭"], 15000, exact=False
                        )
                    log(
                        f"第 {i} 个广告:",
                        f"已关闭（{closed}）" if closed else "未找到 Close（可能自动关闭）",
                    )
                    page.wait_for_timeout(6000)

                # 等待本轮 +4h 生效
                end = time.time() + 180
                round_ok = False
                while time.time() < end:
                    try:
                        now = api_state(cfg, server_id)
                    except SystemExit:
                        now = None
                    except Exception:
                        now = None
                    if now and (
                        now.get("sessionExtensions", 0) > cur.get("sessionExtensions", 0)
                        or now.get("sessionExpiresAt") != cur.get("sessionExpiresAt")
                    ):
                        rounds_ok += 1
                        round_ok = True
                        success = True
                        log(
                            f"第 {round_no} 轮续期成功 -> 新到期: {now.get('sessionExpiresAt')}"
                            f" | 累计: {now.get('sessionExtensions')} | 今日: {today_used(now)}"
                        )
                        break
                    time.sleep(10)
                if not round_ok:
                    log(f"第 {round_no} 轮未检测到续期生效（广告未播完 / 页面卡住 / 已达上限），停止后续轮次")
                    stop_reason = stop_reason or "本轮未检测到续期生效"
                    now = now or cur
                    break

                # 给页面一点时间回到可再次「延伸」的状态
                page.wait_for_timeout(3000)

            # 结束前截一张最终画面
            last_shot = take_screenshot(page, "renew_screenshot.png")

        except SystemExit:
            raise
        except Exception as e:
            log(f"运行异常: {e}")
            try:
                dump_page_debug(page, "异常")
            except Exception:
                pass
            notify(
                cfg,
                "❌ Voer 续期异常",
                [
                    f"服务器: <code>{short_id}</code>",
                    f"错误: <code>{e}</code>",
                ],
                photo=pathlib.Path(_shot_name("debug_screenshot.png")),
            )
            return False
        finally:
            page.wait_for_timeout(1500)
            browser.close()

    # 结束后发通知
    if success:
        final_state = now or before
        # 下次可续期 = 本会话到期 − 现在
        uptime = seconds_until(final_state.get("sessionExpiresAt"))
        if uptime is None:
            uptime = seconds_until(before.get("sessionExpiresAt"))
        result = f"✅续期成功（+{rounds_ok * 4}h，共 {rounds_ok} 次）"
        notify_godlike(cfg, account, short_id, result, uptime, final_state.get("status"))
        return True
    else:
        final_state = now or before
        uptime = seconds_until(final_state.get("sessionExpiresAt"))
        reason = stop_reason or "未检测到续期生效"
        notify_godlike(
            cfg, account, short_id, f"⚠️续期未生效（{reason}）", uptime, final_state.get("status")
        )
        return False


def main():
    cfg = load_config()
    server_ids = cfg.get("server_ids") or [cfg["server_id"]]

    # 取账号邮箱（用于通知；失败不影响续期）
    account = ""
    if "--status" not in sys.argv:
        account = fetch_account_email(cfg)
        if account:
            log(f"账号: {account}")
    else:
        try:
            account = account_email_from_server(api_state(cfg, server_ids[0]))
        except Exception:
            account = ""

    total = len(server_ids)
    results = []
    for idx, sid in enumerate(server_ids, 1):
        log("=" * 60)
        log(f"[{idx}/{total}] 开始处理服务器 {sid[:8]}…")
        log("=" * 60)
        # 每台服务器用独立截图文件名，避免互相覆盖
        os.environ["VOER_SHOT_TAG"] = str(idx)
        try:
            ok = run_server(cfg, sid, account=account)
        except SystemExit as e:
            # api_state 里 401/403 会 SystemExit(1)：token 失效对所有服务器一样，直接终止
            log(f"服务器 {sid[:8]}… 触发致命错误（exit={e.code}），停止全部任务")
            raise
        except Exception as e:
            log(f"服务器 {sid[:8]}… 发生未预期异常: {e}")
            ok = False
        results.append((sid, ok))

    log("=" * 60)
    log("全部服务器处理完毕，结果汇总:")
    fail = 0
    for sid, ok in results:
        if ok is True:
            mark = "✅ 成功"
        elif ok is None:
            mark = "⏭️ 跳过（今日已满 4 次，UTC 当日）"
        else:
            mark = "❌ 失败"
        if ok is False:
            fail += 1
        log(f"  {mark}  {sid[:8]}…")
    log(f"合计: 成功/跳过 {total - fail}/{total} 台（失败 {fail} 台）")
    log("=" * 60)
    if fail:
        sys.exit(3)


if __name__ == "__main__":
    main()
