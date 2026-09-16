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
    python3 voer_renew.py            自动续期（关机则先重启，再按可续期次数连续续满）
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
    "restart_if_stopped": True,  # 关机/离线时先重启，就绪后再续期
    "headless": False,
    "use_system_chrome": False,
    "telegram_bot_token": "",
    "telegram_chat_id": "",
    "tg_title": "Godlike 续期通知",   # TG 通知标题，可用环境变量 TG_TITLE 覆盖
}

from playwright.sync_api import sync_playwright

MAX_DAILY_EXTENSIONS = 4
MAX_SESSION_EXTENSIONS = 4

RUNNING_STATUSES = {"running", "online"}
STOPPED_STATUSES = {"stopped", "offline"}
CRASHED_STATUSES = {"crashed", "error", "provisioning_error", "supervisor_error"}
STARTING_STATUSES = {
    "starting",
    "provisioning",
    "pending",
    "starting_node",
    "restarting",
    "migrating",
}
STOPPING_STATUSES = {"stopping"}


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


def notify_godlike(cfg, account, server_id, result, uptime_sec, status, photo=None, remaining_text=""):
    """按固定模板发送续期通知；若传入 photo 则附带真实面板截图。"""
    lines = [
        f"⏰运行时间: {fmt_local_time()}",
        f"🖥️账号: {account or '—'}",
        f"🖥️服务器: {server_id}",
        f"🔢下次可续期: {fmt_duration(uptime_sec)}",
        f"🔢可续期次数: {remaining_text or '—'}",
        f"📊续期结果: {result}",
        f"📊开机状态: {status_text(status)}",
    ]
    shot = None
    if photo is not None:
        shot = photo if isinstance(photo, pathlib.Path) else pathlib.Path(photo)
        if not shot.exists():
            log(f"通知截图不存在: {shot}")
            shot = None
    notify(cfg, cfg.get("tg_title") or "Godlike 续期通知", lines, photo=shot)


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


def quota(server: dict) -> dict:
    """计算可续期次数。

    平台同时限制：每 UTC 日最多 4 次、每个会话最多 4 次。
    可续期次数 = min(今日剩余, 本会话剩余)。
    必须用 today_used() 结合 sessionExtensionsDate，跨日时当日计数会过期。
    """
    used_today = today_used(server)
    try:
        session_ext = int(server.get("sessionExtensions") or 0)
    except Exception:
        session_ext = 0
    session_ext = max(0, session_ext)
    daily_left = max(0, MAX_DAILY_EXTENSIONS - used_today)
    session_left = max(0, MAX_SESSION_EXTENSIONS - session_ext)
    remaining = min(daily_left, session_left)
    return {
        "used_today": used_today,
        "session_ext": session_ext,
        "daily_left": daily_left,
        "session_left": session_left,
        "remaining": remaining,
        "daily_max": MAX_DAILY_EXTENSIONS,
        "session_max": MAX_SESSION_EXTENSIONS,
    }


def fmt_quota(q: dict) -> str:
    return (
        f"可续期次数: {q['remaining']} 次"
        f"（今日剩余 {q['daily_left']}/{q['daily_max']}"
        f" · 本会话剩余 {q['session_left']}/{q['session_max']}"
        f" | 今日已用 {q['used_today']}/{q['daily_max']}"
        f"，会话已用 {q['session_ext']}/{q['session_max']}）"
    )


def log_quota(server: dict, prefix: str = "") -> dict:
    q = quota(server)
    head = prefix if prefix else "检查"
    log(f"{head}{fmt_quota(q)}")
    return q


def server_status_key(server) -> str:
    if not server:
        return ""
    return str(server.get("status") or "").strip().lower()


def is_running(server) -> bool:
    return server_status_key(server) in RUNNING_STATUSES


def is_stopped(server) -> bool:
    return server_status_key(server) in STOPPED_STATUSES


def is_crashed(server) -> bool:
    return server_status_key(server) in CRASHED_STATUSES


def is_starting(server) -> bool:
    return server_status_key(server) in STARTING_STATUSES


def api_post(cfg, path: str, payload=None, timeout: int = 60):
    """POST voer.host API。返回 (status_code, body_dict)。失败不 sys.exit。"""
    url = path if path.startswith("http") else f"https://voer.host{path}"
    body = json.dumps(payload if payload is not None else {}).encode()
    req = urllib.request.Request(
        url,
        data=body,
        headers={
            "Cookie": f"token={cfg['token']}",
            "Authorization": f"Bearer {cfg['token']}",
            "User-Agent": UA,
            "Accept": "application/json",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read().decode(errors="replace")
            try:
                data = json.loads(raw) if raw.strip() else {}
            except Exception:
                data = {"raw": raw[:300]}
            return r.status, data
    except urllib.error.HTTPError as e:
        raw = ""
        try:
            raw = e.read().decode(errors="replace")[:800]
        except Exception:
            pass
        data = {}
        try:
            data = json.loads(raw) if raw else {}
        except Exception:
            data = {"error": raw or str(e.reason)}
        return e.code, data
    except Exception as e:
        log(f"API POST 异常 {url}: {e}")
        return 0, {"error": str(e)}


def api_power(cfg, server_id: str, action: str, extra=None):
    """电源操作：start / stop / restart / kill。"""
    payload = dict(extra or {})
    code, data = api_post(cfg, f"/api/servers/{server_id}/{action}", payload)
    err = ""
    if isinstance(data, dict):
        err = str(data.get("error") or data.get("message") or "")
    log(f"电源指令 {action}: HTTP {code}" + (f" {err}" if err else ""))
    return code, data


def wait_for_status(cfg, server_id: str, want, timeout: int = 300, poll: float = 5):
    """轮询直到 status 落入 want 集合，超时返回最后一次状态。"""
    want = {str(x).lower() for x in want}
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        try:
            last = api_state(cfg, server_id)
        except SystemExit:
            time.sleep(poll)
            continue
        except Exception as e:
            log(f"轮询状态失败: {e}")
            time.sleep(poll)
            continue
        st = server_status_key(last)
        log(f"等待开机: 当前状态={st or '未知'}")
        if st in want:
            return last
        time.sleep(poll)
    return last


def click_start_button(page) -> str | None:
    start_labels = [
        "Start",
        "启动",
        "开机",
        "開機",
        "開始",
        "Recover",
        "恢复",
        "恢復",
        "Start server",
    ]
    hit = click_anywhere(page, start_labels, 20000)
    if not hit:
        hit = click_anywhere(page, start_labels, 15000, exact=False)
    return hit


def watch_rewarded_ads(cfg, page, reason: str = "开机/续期") -> int:
    """点击 Watch ad 并等待播放，返回实际点击的广告数。"""
    watch_labels = [
        "Watch ad",
        "觀看廣告",
        "观看广告",
        "Watch Ad",
        "Watch ads",
        "Watch Ads",
        "Watch",
        "开始",
        "開始",
    ]
    page.wait_for_timeout(4000)
    hit = click_anywhere(page, watch_labels, 25000) or click_anywhere(
        page, watch_labels, 12000, exact=False
    )
    if hit:
        log(f"{reason}: 已进入广告流程（{hit}）")
    else:
        log(f"{reason}: 未找到 Watch ad（可能无需广告，或已自动开始）")
    page.wait_for_timeout(6000)
    total = int(cfg.get("ads_per_extension") or 3)
    watched = 0
    for i in range(1, total + 1):
        hit = click_anywhere(page, ["Watch ad", "觀看廣告", "观看广告"], 50000)
        if not hit:
            log(f"{reason}: 第 {i} 个 Watch ad 未找到，停止广告流程")
            break
        watched += 1
        log(f"{reason}: 已点击第 {i}/{total} 个 Watch ad（{hit}），播放中…")
        page.wait_for_timeout(int(cfg.get("ad_duration_sec") or 32) * 1000)
        closed = click_anywhere(page, ["Close", "關閉", "关闭"], 40000)
        log(
            f"{reason}: 第 {i} 个广告:",
            f"已关闭（{closed}）" if closed else "未找到 Close（可能自动关闭）",
        )
        page.wait_for_timeout(5000)
    return watched


def restart_via_panel(cfg, page, reason: str = "关机后重启") -> bool:
    """在面板点击 Start/开机，必要时看激励广告。"""
    if page is None:
        return False
    hit = click_start_button(page)
    if not hit:
        log(f"{reason}: 面板上未找到 Start/开机 按钮")
        return False
    log(f"{reason}: 已点击开机入口: {hit}")
    page.wait_for_timeout(2500)
    watch_rewarded_ads(cfg, page, reason=reason)
    return True


def ads_required_error(code, data) -> bool:
    if code == 403:
        return True
    blob = json.dumps(data, ensure_ascii=False) if isinstance(data, dict) else str(data or "")
    return "Ad requirement" in blob or "adsCompleted" in blob or "rewarded" in blob.lower()


def ensure_running(cfg, server_id: str, page=None):
    """关机/离线/崩溃时先重启，就绪后再让调用方进入续期。

    返回 (server, did_restart)。
    """
    skip = str(os.environ.get("VOER_SKIP_RESTART", "")).strip() in ("1", "true", "yes")
    if skip or not cfg.get("restart_if_stopped", True):
        return api_state(cfg, server_id), False

    server = api_state(cfg, server_id)
    st = server_status_key(server)
    log(f"开机状态: {st or '未知'} — {status_text(st)}")

    if is_running(server):
        log("服务器已在运行中，无需重启")
        return server, False

    if is_starting(server):
        log("服务器启动/重启中，等待就绪后再进入续期…")
        server = wait_for_status(cfg, server_id, RUNNING_STATUSES, timeout=360) or server
        if is_running(server):
            log("服务器已就绪")
            return server, True
        st = server_status_key(server)

    if st in STOPPING_STATUSES:
        log("服务器正在关机，等待停稳后再重启…")
        wait_for_status(cfg, server_id, STOPPED_STATUSES | CRASHED_STATUSES, timeout=120)
        server = api_state(cfg, server_id)
        st = server_status_key(server)

    if not (is_stopped(server) or is_crashed(server) or not is_running(server)):
        return server, False

    log("检测到关机/离线，按流程：先重启，重启完成后再进入续期")
    actions = ["restart"] if is_crashed(server) else ["start", "restart"]
    accepted = False
    need_ads = False
    for act in actions:
        log(f"关机后先重启：发送 {act}")
        extra = {"adsCompleted": 0} if act == "start" else {}
        code, data = api_power(cfg, server_id, act, extra)
        if code in (200, 201, 202, 204):
            accepted = True
            if isinstance(data, dict) and data.get("server"):
                server = data["server"]
            break
        if ads_required_error(code, data):
            need_ads = True
            log("开机需要先看激励广告，改为在面板点击 Start 并播放广告")
            break
        log(f"{act} 未成功，尝试下一指令")

    if need_ads or not accepted:
        if page is not None:
            restart_via_panel(cfg, page, reason="关机后重启")
        elif need_ads:
            log("开机需要看广告，但当前没有浏览器会话，无法完成重启")
            return server, False

    server = wait_for_status(cfg, server_id, RUNNING_STATUSES, timeout=360) or api_state(
        cfg, server_id
    )
    if not is_running(server):
        if page is not None and not need_ads:
            log("API 重启后仍未运行，尝试面板开机")
            restart_via_panel(cfg, page, reason="关机后重启（面板兜底）")
            server = wait_for_status(
                cfg, server_id, RUNNING_STATUSES, timeout=240
            ) or api_state(cfg, server_id)
    if not is_running(server):
        raise RuntimeError(
            f"重启后服务器仍未运行，当前状态: {server_status_key(server) or '未知'}"
        )
    log("重启完成，服务器已在运行")
    return server, True


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
    skip_restart = os.environ.get("VOER_SKIP_RESTART", "").strip().lower()
    if skip_restart in ("1", "true", "yes"):
        cfg["restart_if_stopped"] = False
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


def click_anywhere(page, texts, timeout_ms, exact=True):
    deadline = time.time() + timeout_ms / 1000
    while time.time() < deadline:
        for frame in page.frames:
            for t in texts:
                makers = [
                    lambda t=t, f=frame: f.get_by_role("button", name=t, exact=exact).first,
                    lambda t=t, f=frame: f.get_by_text(t, exact=exact).first,
                    lambda t=t, f=frame: f.locator(f"button:has-text('{t}')").first,
                    lambda t=t, f=frame: f.locator(f"[role=button]:has-text('{t}')").first,
                ]
                for maker in makers:
                    try:
                        loc = maker()
                        if loc.count() and loc.is_visible():
                            loc.click(timeout=3000)
                            return f"{t}@{frame.url[:60]}"
                    except Exception:
                        pass
        time.sleep(1.2)
    return None


def _shot_name(base: str) -> str:
    """多服务器时给截图文件名加编号后缀：renew_screenshot.png -> renew_screenshot_2.png"""
    tag = os.environ.get("VOER_SHOT_TAG", "1")
    if tag == "1":
        return base
    p = pathlib.Path(base)
    return f"{p.stem}_{tag}{p.suffix}"


def take_screenshot(page, name="screenshot.png") -> pathlib.Path:
    """截取当前浏览器真实页面（面板状态），供 Telegram 发送。"""
    path = pathlib.Path(_shot_name(name))
    try:
        try:
            page.wait_for_timeout(800)
        except Exception:
            pass
        try:
            page.screenshot(path=str(path), full_page=True, type="png")
        except Exception:
            page.screenshot(path=str(path), full_page=False, type="png")
        size = path.stat().st_size if path.exists() else 0
        log(f"截图已保存: {path.resolve()} ({size} bytes)")
        if size < 1000:
            log("警告: 截图文件过小，可能是空白页")
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
        print(f"今日已续期(UTC, 计算值) = {today_used(s)} / {MAX_DAILY_EXTENSIONS}")
        q = quota(s)
        print(f"本会话已续期 = {q['session_ext']} / {MAX_SESSION_EXTENSIONS}")
        print(fmt_quota(q))
        print(f"开机状态 = {s.get('status')} ({status_text(s.get('status'))})")
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
                    f"今日续期: {today_used(s)} / {MAX_DAILY_EXTENSIONS} (UTC)",
                    f"{fmt_quota(q)}",
                ],
            )
        return True

    success = False
    before = {}
    now = {}
    last_shot = None  # 结束时填入真实截图
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
            log(f"开机状态: {before.get('status')} — {status_text(before.get('status'))}")
            log_quota(before, prefix="重启前检查")

            for accept_txt in ("Accept", "Accept all", "同意", "接受", "I agree", "OK"):
                hit = click_anywhere(page, [accept_txt], 3000)
                if hit:
                    log(f"已点同意弹窗: {hit}")
                    break

            # 关机 / 离线 / 崩溃：先重启，重启完成后再进入续期
            try:
                before, did_restart = ensure_running(cfg, server_id, page=page)
            except RuntimeError as e:
                log(f"关机后重启失败: {e}")
                notify(
                    cfg,
                    "❌ Voer 重启失败",
                    [
                        f"服务器: <code>{short_id}</code>",
                        f"原因: {e}",
                        "关机后未能重启，已中止续期",
                    ],
                    photo=pathlib.Path(_shot_name("debug_screenshot.png")),
                )
                return False
            if did_restart:
                try:
                    page.reload(wait_until="domcontentloaded", timeout=60000)
                    page.wait_for_timeout(4000)
                except Exception:
                    pass
                log("重启之后检查可续期次数")
                log_quota(before, prefix="重启后检查")

            # 今日/本会话已达上限时面板会隐藏续期入口，直接跳过（不算失败）
            # 返回 None 表示「跳过」，区别于 True=成功 / False=失败
            q_now = quota(before)
            if q_now["remaining"] <= 0:
                log(
                    f"可续期次数为 0（今日 {q_now['used_today']}/{MAX_DAILY_EXTENSIONS}"
                    f" · 会话 {q_now['session_ext']}/{MAX_SESSION_EXTENSIONS}），"
                    "平台已隐藏续期入口，跳过该服务器"
                )
                return None
            if int(before.get("sessionExtensionsToday") or 0) >= MAX_DAILY_EXTENSIONS and q_now["used_today"] == 0:
                log(
                    "注意：sessionExtensionsToday="
                    f"{before.get('sessionExtensionsToday')} 但 sessionExtensionsDate="
                    f"{before.get('sessionExtensionsDate')} 不是今天（UTC），"
                    "该计数已过期，按 0 处理，继续尝试续期"
                )

            # ===== 单次运行内连续续期：每轮 = 点延伸 + 看 3 个广告 + 验证 +4h =====
            for round_no in range(1, max_ext + 1):
                cur = api_state(cfg, server_id)
                q = quota(cur)
                used_today = q["used_today"]
                session_ext = q["session_ext"]
                log("-" * 60)
                log(
                    f"第 {round_no}/{max_ext} 轮：{fmt_quota(q)} | 到期 {cur.get('sessionExpiresAt')}"
                )
                if q["remaining"] <= 0:
                    if used_today >= MAX_DAILY_EXTENSIONS:
                        stop_reason = f"今日已达上限（{used_today}/{MAX_DAILY_EXTENSIONS}）"
                    elif session_ext >= MAX_SESSION_EXTENSIONS:
                        stop_reason = f"本会话已达上限（{session_ext}/{MAX_SESSION_EXTENSIONS}）"
                    else:
                        stop_reason = "可续期次数为 0"
                    log(f"到达平台限制：{stop_reason}，停止续期")
                    log_quota(cur, prefix="当前")
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
                page.wait_for_timeout(8000)

                total = int(cfg["ads_per_extension"])
                for i in range(1, total + 1):
                    hit = click_anywhere(
                        page, ["Watch ad", "觀看廣告", "观看广告"], 75000
                    )
                    if not hit:
                        log(f"第 {i} 个 Watch ad 未找到，停止")
                        break
                    log(f"已点击第 {i}/{total} 个 Watch ad（{hit}），播放中…")
                    page.wait_for_timeout(int(cfg["ad_duration_sec"]) * 1000)
                    closed = click_anywhere(page, ["Close", "關閉", "关闭"], 60000)
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
                        q_after = quota(now)
                        log(
                            f"第 {round_no} 轮续期成功 -> 新到期: {now.get('sessionExpiresAt')}"
                            f" | 累计: {now.get('sessionExtensions')} | 今日: {today_used(now)}"
                        )
                        log(f"本轮完成后实时{fmt_quota(q_after)}")
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
        photo = last_shot if (last_shot is not None and last_shot.exists()) else None
        if photo is None:
            cand = pathlib.Path(_shot_name("renew_screenshot.png"))
            photo = cand if cand.exists() else None
        remaining_text = fmt_quota(quota(final_state))
        notify_godlike(
            cfg,
            account,
            short_id,
            result,
            uptime,
            final_state.get("status"),
            photo=photo,
            remaining_text=remaining_text,
        )
        return True
    else:
        final_state = now or before
        uptime = seconds_until(final_state.get("sessionExpiresAt"))
        reason = stop_reason or "未检测到续期生效"
        photo = last_shot if (last_shot is not None and last_shot.exists()) else None
        if photo is None:
            for name in ("renew_screenshot.png", "debug_screenshot.png"):
                cand = pathlib.Path(_shot_name(name))
                if cand.exists():
                    photo = cand
                    break
        remaining_text = fmt_quota(quota(final_state)) if final_state else ""
        notify_godlike(
            cfg,
            account,
            short_id,
            f"⚠️续期未生效（{reason}）",
            uptime,
            final_state.get("status"),
            photo=photo,
            remaining_text=remaining_text,
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
