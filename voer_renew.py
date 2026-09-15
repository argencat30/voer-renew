#!/usr/bin/env python3
"""
Voer.host 免费服务器会话续期（Playwright 版）

原理：免费档会话制（默认 4h），续期需看完 3 个 Google 激励广告 -> +4h。
按钮在跨进程 iframe（wormies.voer.host / googleads.g.doubleclick.net）里，
必须用 Playwright（原生支持 OOPIF）才能点到，Selenium/JS 无法穿透。

限制：每 UTC 日最多 4 次、每会话最多 4 次（每次 +4h）。

流程（本版）：
    1. 先检查电源：关机 / 异常则先重启（开机），等到 running
    2. 若 API 开机返回 403（需要广告）：必须打开面板点 Start 并看广告开机
       （旧会话已死时「本会话 0/4」不能阻止开机；开机成功会开新会话并重置本会话计数）
    3. 开机后再读可续期次数（今日 / 本会话），再决定是否看广告续期
    4. 每轮续期成功后立刻重查 API，实时打印剩余可续期次数
    5. 仅当「已在运行」且今日/本会话次数都用完时，才跳过看广告
    6. 剩余次数以面板 API 为准，保证下次运行能接着续

优先读取环境变量（适合 GitHub Actions / Docker / cron）：
    VOER_SERVER_ID        服务器 UUID（必须）
    VOER_TOKEN            Cookie 里的 token JWT（必须）
    TELEGRAM_BOT_TOKEN    Telegram Bot Token（可选，用于通知）
    TELEGRAM_CHAT_ID      Telegram Chat ID（可选，用于通知）
    VOER_AUTO_RESTART     关机后是否自动重启，默认 1
    VOER_POWER_WAIT_SEC   等待开机进入 running 的秒数，默认 240

也支持本地 config.json（环境变量优先级更高）。

VPS / CI 无图形界面时必须用虚拟显示：
    xvfb-run -a python3 voer_renew.py

用法：
    python3 voer_renew.py            先重启（如关机）再自动续期
    python3 voer_renew.py --status   只看当前状态，不看广告、不碰电源
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
from datetime import datetime, timezone

BASE = pathlib.Path(__file__).resolve().parent
CONFIG_PATH = BASE / "config.json"
UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/153.0.0.0 Safari/537.36"
)

DAILY_LIMIT = 4
SESSION_LIMIT = 4

RUNNING_STATUSES = {"running", "online"}
STOPPED_STATUSES = {"stopped", "offline"}
CRASHED_STATUSES = {"crashed", "error", "provisioning_error", "supervisor_error"}
TRANSITIONAL_STATUSES = {
    "starting",
    "provisioning",
    "pending",
    "starting_node",
    "restarting",
    "migrating",
    "stopping",
}

DEFAULT_CONFIG = {
    "server_id": "在这里填服务器 UUID（面板地址 /panel/server/ 后面那串）",
    "token": "在这里填浏览器 Cookie 里 voer.host 的 token 值（JWT）",
    "ads_per_extension": 3,
    "ad_duration_sec": 32,
    "extensions_per_run": 4,   # 单次运行内最多连续续期几次（受平台每日/每会话 4 次上限约束）
    "auto_restart": True,      # 关机 / 异常时先重启，再进入续期
    "power_wait_sec": 240,     # 等待开机进入 running 的秒数
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
    parts.append(
        f"--{boundary}\r\n"
        f'Content-Disposition: form-data; name="chat_id"\r\n\r\n'
        f"{chat_id}\r\n".encode()
    )
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
            "Authorization": f"Bearer {cfg['token']}",
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


def quota_line(quota) -> str:
    if not quota:
        return "—"
    return (
        f"今日剩余 {quota['remain_today']}/{quota['daily_limit']} · "
        f"本会话剩余 {quota['remain_session']}/{quota['session_limit']} · "
        f"综合 {quota['remain']} 次"
    )


def notify_godlike(
    cfg,
    account,
    server_id,
    result,
    uptime_sec,
    status,
    photo=None,
    quota=None,
    power_note=None,
):
    """按固定模板发送续期通知；若传入 photo 则附带真实面板截图。"""
    lines = [
        f"⏰运行时间: {fmt_local_time()}",
        f"🖥️账号: {account or '—'}",
        f"🖥️服务器: {server_id}",
        f"🔢下次可续期: {fmt_duration(uptime_sec)}",
        f"🔢可续期次数: {quota_line(quota)}",
        f"📊续期结果: {result}",
        f"📊开机状态: {status_text(status)}",
    ]
    if power_note:
        lines.append(f"🔌电源操作: {power_note}")
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
                exp_dt = datetime.utcfromtimestamp(exp)
                now = datetime.utcnow()
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
    """
    raw = server.get("sessionExtensionsToday") or 0
    try:
        raw = int(raw)
    except Exception:
        raw = 0
    date_val = server.get("sessionExtensionsDate")
    if not date_val:
        return 0
    try:
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        if str(date_val)[:10] == today:
            return max(0, raw)
        return 0
    except Exception:
        return 0


def session_used(server: dict) -> int:
    try:
        return max(0, int(server.get("sessionExtensions") or 0))
    except Exception:
        return 0


def session_is_dead(server: dict) -> bool:
    """会话是否已结束：已关机 / 已到期（无法再「延伸」，只能重新 Start 开新会话）。"""
    if not server:
        return True
    st = (server.get("status") or "").lower()
    if st in STOPPED_STATUSES or st in CRASHED_STATUSES:
        return True
    left = seconds_until(server.get("sessionExpiresAt"))
    if left is not None and left <= 0:
        return True
    return False


def quota_from(server: dict) -> dict:
    """以面板 API 实时值为准，计算今日 / 本会话剩余可续期次数。

    注意：本会话已满且服务器已停机时，旧会话已死；重新 Start 会开新会话，
    sessionExtensions 会重置。此时「本会话 0/4」不应阻止开机看广告。
    """
    used_today = today_used(server)
    used_session = session_used(server)
    remain_today = max(0, DAILY_LIMIT - used_today)
    remain_session = max(0, SESSION_LIMIT - used_session)
    remain = min(remain_today, remain_session)
    dead = session_is_dead(server)
    return {
        "used_today": used_today,
        "used_session": used_session,
        "remain_today": remain_today,
        "remain_session": remain_session,
        "remain": remain,
        "daily_limit": DAILY_LIMIT,
        "session_limit": SESSION_LIMIT,
        "exhausted": remain <= 0,
        "daily_exhausted": remain_today <= 0,
        "session_exhausted": remain_session <= 0,
        "session_dead": dead,
        "expires_at": server.get("sessionExpiresAt"),
        "status": server.get("status"),
    }


def log_quota(quota, tag="可续期次数"):
    log(
        f"{tag}: 综合剩余 {quota['remain']} 次"
        f" | 今日 {quota['used_today']}/{quota['daily_limit']}（剩余 {quota['remain_today']}）"
        f" | 本会话 {quota['used_session']}/{quota['session_limit']}（剩余 {quota['remain_session']}）"
        f" | 到期 {quota.get('expires_at')}"
    )


def _truthy(v, default=True) -> bool:
    if v is None or v == "":
        return default
    if isinstance(v, bool):
        return v
    return str(v).strip().lower() in ("1", "true", "yes", "on")


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
    if os.environ.get("VOER_POWER_WAIT_SEC"):
        cfg["power_wait_sec"] = int(os.environ["VOER_POWER_WAIT_SEC"])
    if os.environ.get("VOER_AUTO_RESTART") is not None:
        cfg["auto_restart"] = _truthy(os.environ.get("VOER_AUTO_RESTART"), True)
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

    ids = [x for x in re.split(r"[,;\s]+", sid.strip()) if x]
    seen = set()
    server_ids = []
    for x in ids:
        if x not in seen:
            seen.add(x)
            server_ids.append(x)
    cfg["server_ids"] = server_ids
    cfg["auto_restart"] = _truthy(cfg.get("auto_restart"), True)
    try:
        cfg["power_wait_sec"] = max(30, int(cfg.get("power_wait_sec") or 240))
    except Exception:
        cfg["power_wait_sec"] = 240

    log(f"server_id 数量={len(server_ids)}")
    for x in server_ids:
        log(f"  - {x[:8]}…")
    log(f"token 诊断: {_jwt_hint(token)}")
    log(f"关机自动重启: {'开' if cfg['auto_restart'] else '关'} | 等待 {cfg['power_wait_sec']}s")
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


def _auth_headers(cfg, json_body=False):
    h = {
        "Cookie": f"token={cfg['token']}",
        "Authorization": f"Bearer {cfg['token']}",
        "User-Agent": UA,
        "Accept": "application/json",
    }
    if json_body:
        h["Content-Type"] = "application/json"
    return h


def api_state(cfg, server_id=None):
    sid = server_id or cfg["server_id"]
    url = f"https://voer.host/api/servers/{sid}"
    req = urllib.request.Request(url, headers=_auth_headers(cfg))
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


def api_power(cfg, server_id, action, extra=None):
    """POST /api/servers/{id}/{start|restart|stop|kill}。返回 (ok, http_code, data)."""
    url = f"https://voer.host/api/servers/{server_id}/{action}"
    payload = {"adsCompleted": 0}
    if extra:
        payload.update(extra)
    body = json.dumps(payload).encode()
    req = urllib.request.Request(
        url, data=body, headers=_auth_headers(cfg, json_body=True), method="POST"
    )
    try:
        with urllib.request.urlopen(req, timeout=45) as r:
            raw = r.read().decode(errors="replace")
            data = json.loads(raw) if raw.strip() else {}
            return True, r.status, data
    except urllib.error.HTTPError as e:
        raw = ""
        try:
            raw = e.read().decode(errors="replace")
        except Exception:
            pass
        data = {}
        try:
            data = json.loads(raw) if raw else {}
        except Exception:
            data = {"raw": raw[:300]}
        return False, e.code, data
    except Exception as e:
        return False, 0, {"error": str(e)}


def _status_of(server) -> str:
    return (server.get("status") or "").lower() if server else ""


def wait_until_running(cfg, server_id, wait_sec) -> dict | None:
    deadline = time.time() + max(30, int(wait_sec))
    last = None
    while time.time() < deadline:
        try:
            last = api_state(cfg, server_id)
        except SystemExit:
            raise
        except Exception as e:
            log(f"等待开机时读状态失败: {e}")
            time.sleep(5)
            continue
        st = _status_of(last)
        left = int(deadline - time.time())
        log(f"电源等待: status={st} | 剩余 {left}s")
        if st in RUNNING_STATUSES:
            return last
        time.sleep(5)
    return last


def ensure_powered_on(cfg, server_id):
    """关机 / 异常则先重启（开机），等到 running。

    返回 dict:
        server, action (already_running|start|restart|waited|failed|ads_required|timeout|disabled),
        note, ads_required
    """
    server = api_state(cfg, server_id)
    st = _status_of(server)
    log(f"电源检查: status={st}")

    if not cfg.get("auto_restart", True):
        return {
            "server": server,
            "action": "disabled",
            "note": "已关闭自动重启",
            "ads_required": False,
        }

    if st in RUNNING_STATUSES:
        log("服务器已在运行中，无需开机/重启")
        return {
            "server": server,
            "action": "already_running",
            "note": "已在运行，无需开机",
            "ads_required": False,
        }

    if st in TRANSITIONAL_STATUSES:
        log(f"服务器处于过渡状态 ({st})，等待进入 running…")
        waited = wait_until_running(cfg, server_id, cfg.get("power_wait_sec", 240))
        if waited and _status_of(waited) in RUNNING_STATUSES:
            return {
                "server": waited,
                "action": "waited",
                "note": f"过渡状态 {st} → {_status_of(waited)}",
                "ads_required": False,
            }
        server = waited or api_state(cfg, server_id)
        st = _status_of(server)
        if st in RUNNING_STATUSES:
            return {
                "server": server,
                "action": "waited",
                "note": f"已进入 {st}",
                "ads_required": False,
            }

    action = "restart" if st in CRASHED_STATUSES else "start"
    log(f"服务器已关机/异常 ({st})，先执行 {action}，重启完成后再进入续期")
    ok, code, data = api_power(cfg, server_id, action)
    err_text = ""
    if isinstance(data, dict):
        err_text = str(data.get("error") or data.get("message") or "")
    ads_needed = (not ok) and (
        code == 403 or "Ad requirement" in err_text or "ads" in err_text.lower()
    )

    if not ok and action == "restart" and not ads_needed:
        log(f"restart 失败 HTTP {code} {err_text}，改试 start")
        ok, code, data = api_power(cfg, server_id, "start")
        action = "start"
        if isinstance(data, dict):
            err_text = str(data.get("error") or data.get("message") or "")
        ads_needed = (not ok) and (
            code == 403 or "Ad requirement" in err_text or "ads" in err_text.lower()
        )

    if ads_needed:
        log("开机接口要求观看广告（会话可能已过期）。若本轮还要续期，将在面板里点 Start 并看广告开机")
        return {
            "server": server,
            "action": "ads_required",
            "note": f"开机需要广告 (HTTP {code})",
            "ads_required": True,
        }

    if not ok:
        note = f"{action} 失败 HTTP {code}: {err_text or data}"
        log(note)
        return {
            "server": server,
            "action": "failed",
            "note": note,
            "ads_required": False,
        }

    if isinstance(data, dict) and data.get("server"):
        server = data["server"]
        log(f"已发出 {action}，当前 status={_status_of(server)}")
    else:
        log(f"已发出 {action}，等待进入 running…")

    waited = wait_until_running(cfg, server_id, cfg.get("power_wait_sec", 240))
    if waited and _status_of(waited) in RUNNING_STATUSES:
        log(f"重启完成 → {_status_of(waited)}，接下来检查可续期次数")
        return {
            "server": waited,
            "action": action,
            "note": f"{st or '?'} → {_status_of(waited)}（{action}）",
            "ads_required": False,
        }

    last = waited or api_state(cfg, server_id)
    note = f"等待 running 超时，当前 {_status_of(last)}"
    log(note)
    return {
        "server": last,
        "action": "timeout",
        "note": note,
        "ads_required": False,
    }


def print_status(server):
    for k in (
        "status",
        "sessionExpiresAt",
        "sessionExtensions",
        "sessionExtensionsToday",
        "sessionExtensionsDate",
        "sessionDuration",
        "adsWatched",
    ):
        print(f"{k} = {server.get(k)}")
    q = quota_from(server)
    print(f"今日已续期(UTC, 计算值) = {q['used_today']} / {q['daily_limit']}")
    print(f"本会话已续期 = {q['used_session']} / {q['session_limit']}")
    print(f"可续期次数 综合剩余 = {q['remain']}")
    print(f"可续期次数 今日剩余 = {q['remain_today']}")
    print(f"可续期次数 本会话剩余 = {q['remain_session']}")
    print(f"电源状态 = {server.get('status')}")
    live = server.get("live") or {}
    resume = (live.get("sessionResume") or {}).get("remainingMs")
    if resume:
        print(f"sessionResume.remainingMs = {resume}")


def click_anywhere(page, texts, timeout_ms, exact=True):
    """在主页面及所有 iframe（含 wormies / googleads OOPIF）中查找并点击。"""
    deadline = time.time() + timeout_ms / 1000
    while time.time() < deadline:
        frames = list(page.frames)
        for frame in frames:
            for t in texts:
                makers = [
                    lambda t=t, f=frame: f.get_by_role("button", name=t, exact=exact).first,
                    lambda t=t, f=frame: f.get_by_role("link", name=t, exact=exact).first,
                    lambda t=t, f=frame: f.get_by_text(t, exact=exact).first,
                    lambda t=t, f=frame: f.locator(f"button:has-text('{t}')").first,
                    lambda t=t, f=frame: f.locator(f"[role=button]:has-text('{t}')").first,
                    lambda t=t, f=frame: f.locator(f"a:has-text('{t}')").first,
                    lambda t=t, f=frame: f.locator(f"div[role='button']:has-text('{t}')").first,
                    # 部分激励广告按钮是可点击的 span / div
                    lambda t=t, f=frame: f.locator(f"span:text-is('{t}')").first,
                    lambda t=t, f=frame: f.locator(f"div:text-is('{t}')").first,
                ]
                for maker in makers:
                    try:
                        loc = maker()
                        if loc.count() and loc.is_visible():
                            # 滚动到可见再点，避免被遮挡
                            try:
                                loc.scroll_into_view_if_needed(timeout=2000)
                            except Exception:
                                pass
                            try:
                                loc.click(timeout=3000, force=False)
                            except Exception:
                                loc.click(timeout=3000, force=True)
                            return f"{t}@{frame.url[:80]}"
                    except Exception:
                        pass
            # aria-label / title 匹配（Close 按钮常用）
            for t in texts:
                try:
                    loc = frame.locator(
                        f"button[aria-label='{t}'], [aria-label='{t}'], "
                        f"button[title='{t}'], [title='{t}']"
                    ).first
                    if loc.count() and loc.is_visible():
                        loc.click(timeout=3000, force=True)
                        return f"aria:{t}@{frame.url[:80]}"
                except Exception:
                    pass
        time.sleep(1.0)
    return None


def click_close_ad(page, timeout_ms=60000):
    """关闭激励广告播放器。Google / 第三方广告关闭按钮样式很多。"""
    close_labels = [
        "Close",
        "關閉",
        "关闭",
        "关闭广告",
        "關閉廣告",
        "Skip",
        "Skip Ad",
        "Skip ad",
        "跳过",
        "跳過",
        "Done",
        "完成",
        "Continue",
        "继续",
        "繼續",
        "OK",
        "确定",
        "確定",
        "×",
        "✕",
        "X",
    ]
    hit = click_anywhere(page, close_labels, timeout_ms, exact=True)
    if hit:
        return hit
    # 尝试点右上角常见关闭图标（无精确文字时）
    deadline = time.time() + min(8, timeout_ms / 1000)
    while time.time() < deadline:
        for frame in page.frames:
            selectors = [
                "button[aria-label*='lose' i]",
                "button[aria-label*='kip' i]",
                "button[aria-label*='关闭']",
                "button[aria-label*='關閉']",
                "[class*='close' i][role='button']",
                "button.close",
                ".close-button",
                "#dismiss-button",
                ".videoAdUiSkipButton",
                ".ytp-ad-skip-button",
                "button[class*='skip' i]",
            ]
            for sel in selectors:
                try:
                    loc = frame.locator(sel).first
                    if loc.count() and loc.is_visible():
                        loc.click(timeout=2000, force=True)
                        return f"css:{sel}@{frame.url[:60]}"
                except Exception:
                    pass
        time.sleep(0.8)
    return None


def _shot_name(base: str) -> str:
    """多服务器时给截图文件名加编号后缀。"""
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


WATCH_AD_LABELS = [
    "Watch ad",
    "Watch Ad",
    "Watch ads",
    "Watch Ads",
    "觀看廣告",
    "观看广告",
    "觀看廣告以繼續",
    "观看广告以继续",
]


def watch_ads_round(page, cfg, total, watch_labels=None):
    """看完一轮激励广告（开机或续期共用）。返回成功看完的个数。

    流程每条广告：
      1. 点击「Watch ad」
      2. 等待播放时长（默认 ~32s，可配置）
      3. 点击 Close / Skip / X 关闭播放器
      4. 等待 UI 回到进度弹窗，再点下一条
    """
    labels = list(watch_labels or WATCH_AD_LABELS)
    # 去重保持顺序
    seen = set()
    labels = [x for x in labels if not (x in seen or seen.add(x))]
    watched = 0
    duration_ms = max(15, int(cfg.get("ad_duration_sec") or 32)) * 1000

    for i in range(1, total + 1):
        log(f"等待第 {i}/{total} 个「Watch ad」按钮出现…")
        hit = click_anywhere(page, labels, 90000, exact=True)
        if not hit:
            # 宽松匹配再试一次
            hit = click_anywhere(page, labels, 20000, exact=False)
        if not hit:
            log(f"第 {i} 个 Watch ad 未找到，停止（已看 {watched}/{total}）")
            try:
                dump_page_debug(page, f"missing_watch_ad_{i}")
            except Exception:
                pass
            break

        log(f"已点击第 {i}/{total} 个 Watch ad（{hit}），等待广告播放 {duration_ms // 1000}s…")
        page.wait_for_timeout(duration_ms)

        closed = click_close_ad(page, timeout_ms=45000)
        if closed:
            log(f"第 {i} 个广告已关闭（{closed}）")
        else:
            log(f"第 {i} 个广告未找到 Close，再等 8s 后继续（可能已自动关闭）")
            page.wait_for_timeout(8000)
            closed = click_close_ad(page, timeout_ms=10000)
            if closed:
                log(f"第 {i} 个广告延迟关闭成功（{closed}）")

        # 关闭后给进度弹窗一点时间刷新（0/3 → 1/3 → …）
        page.wait_for_timeout(5000)
        watched += 1
        log(f"广告进度: 已完成 {watched}/{total}")

    return watched


def playwright_start(page, cfg, server_id, watch_labels=None):
    """面板里点 Start / 开机，必要时看完 3 条广告。返回是否已 running。"""
    labels = list(watch_labels or WATCH_AD_LABELS)
    start_labels = [
        "Start server",
        "Start Server",
        "Start",
        "开机",
        "启动",
        "啟動",
        "開始",
        "開機",
        "Power on",
        "Boot",
    ]
    log("正在寻找「Start / 开机」按钮…")
    hit = click_anywhere(page, start_labels, 25000) or click_anywhere(
        page, start_labels, 15000, exact=False
    )
    if not hit:
        log("未找到开机按钮")
        try:
            dump_page_debug(page, "no_start_button")
        except Exception:
            pass
        return False
    log(f"已点击开机入口: {hit}")
    page.wait_for_timeout(5000)

    # 检测是否出现「Watch 3 ads to start」弹窗 / Watch ad 按钮
    # 注意：不要提前点掉第一个 Watch ad，全部交给 watch_ads_round 计数
    need_ads = False
    probe = click_anywhere(page, labels, 12000, exact=True)
    if probe:
        # 点到了第一个 — 算作第 1 条已点，接着播完并关，再继续 2、3
        need_ads = True
        log(f"检测到开机广告弹窗，已点第 1 个 Watch ad（{probe}）")
        duration_ms = max(15, int(cfg.get("ad_duration_sec") or 32)) * 1000
        log(f"等待第 1 条广告播放 {duration_ms // 1000}s…")
        page.wait_for_timeout(duration_ms)
        closed = click_close_ad(page, timeout_ms=45000)
        log(f"第 1 个广告: {'已关闭 (' + closed + ')' if closed else '未找到 Close，继续'}")
        page.wait_for_timeout(5000)
        rest = max(0, int(cfg.get("ads_per_extension") or 3) - 1)
        if rest > 0:
            log(f"继续观看剩余 {rest} 条广告…")
            more = watch_ads_round(page, cfg, rest, labels)
            watched = 1 + more
        else:
            watched = 1
        log(f"开机广告合计观看: {watched}/{cfg.get('ads_per_extension', 3)}")
    else:
        # 可能不需要广告，或弹窗文案不同 — 再扫一次页面文字
        try:
            body = ""
            for fr in page.frames:
                try:
                    body += (fr.inner_text("body", timeout=1500) or "") + "\n"
                except Exception:
                    pass
            if any(
                k in body
                for k in (
                    "Watch 3 ads",
                    "Watch ad",
                    "觀看廣告",
                    "观看广告",
                    "Rewarded ad",
                    "start your free server",
                )
            ):
                need_ads = True
                log("页面文案显示需要看广告，但按钮暂未点到，重试完整一轮…")
                watched = watch_ads_round(
                    page, cfg, int(cfg.get("ads_per_extension") or 3), labels
                )
                log(f"开机广告合计观看: {watched}/{cfg.get('ads_per_extension', 3)}")
            else:
                log("未检测到广告弹窗，可能无需看广告即可开机")
        except Exception as e:
            log(f"探测广告弹窗异常: {e}")

    # 看完广告后可能还要再点一次确认 / Start
    page.wait_for_timeout(3000)
    for confirm in ("Start", "Continue", "继续", "繼續", "OK", "确定", "確定", "完成", "Done"):
        if click_anywhere(page, [confirm], 3000):
            log(f"开机后确认点击: {confirm}")
            page.wait_for_timeout(2000)
            break

    waited = wait_until_running(cfg, server_id, cfg.get("power_wait_sec", 240))
    if waited and _status_of(waited) in RUNNING_STATUSES:
        log("面板开机完成，服务器已 running")
        return True
    log(f"面板开机后仍未 running，当前 {(waited or {}).get('status')}")
    try:
        dump_page_debug(page, "boot_still_stopped")
    except Exception:
        pass
    return False


def run_server(cfg, server_id, account=""):
    """对单台服务器：先关机检查/重启，再按剩余次数续期。

    返回 True=成功，False=失败，None=跳过（4 次已用完，仅做了关机/重启）。
    """
    url = f"https://voer.host/panel/server/{server_id}"
    short_id = server_id[:8] + "…"

    if "--status" in sys.argv:
        s = api_state(cfg, server_id)
        print_status(s)
        if os.environ.get("TG_NOTIFY_STATUS") == "1":
            q = quota_from(s)
            notify(
                cfg,
                "📊 Voer 状态查询",
                [
                    f"服务器: <code>{short_id}</code>",
                    f"状态: {s.get('status')}",
                    f"到期: {s.get('sessionExpiresAt')}",
                    f"可续期次数: {quota_line(q)}",
                    f"累计续期: {s.get('sessionExtensions')}",
                    f"今日续期: {today_used(s)} / 4 (UTC)",
                ],
            )
        return True

    success = False
    before = {}
    now = {}
    last_shot = pathlib.Path(_shot_name("renew_screenshot.png"))
    max_ext = max(1, int(cfg.get("extensions_per_run", 4)))
    rounds_ok = 0
    stop_reason = ""
    skip_renew = False
    power_info = {
        "action": "unchecked",
        "note": "",
        "ads_required": False,
        "server": None,
    }

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

    # ===== 1) 先检查关机并重启 =====
    log("-" * 60)
    log("阶段 1/2：关机检查 → 重启")
    power_info = ensure_powered_on(cfg, server_id)
    before = power_info.get("server") or api_state(cfg, server_id)
    log(
        "当前到期:",
        before.get("sessionExpiresAt"),
        "| 已续期:",
        before.get("sessionExtensions"),
        "| 今日:",
        before.get("sessionExtensionsToday"),
        f"(sessionExtensionsDate={before.get('sessionExtensionsDate')})",
        "| status:",
        before.get("status"),
    )

    # 重启之后必须重新拉 API，避免用关机前的过期计数导致下次无法续期
    before = api_state(cfg, server_id)
    quota = quota_from(before)
    log("-" * 60)
    log("阶段 2/2：检查可续期次数")
    log_quota(quota, "重启后可续期次数")

    if int(before.get("sessionExtensionsToday") or 0) >= 4 and today_used(before) == 0:
        log(
            "注意：sessionExtensionsToday="
            f"{before.get('sessionExtensionsToday')} 但 sessionExtensionsDate="
            f"{before.get('sessionExtensionsDate')} 不是今天（UTC），"
            "该计数已过期，按 0 处理"
        )

    # 是否必须进面板看广告开机（API 403 或仍未 running）
    need_boot = bool(
        power_info.get("ads_required")
        or _status_of(before) not in RUNNING_STATUSES
    )
    if need_boot:
        log(
            "服务器未运行 / 开机需要广告 → 必须打开面板点 Start 并看广告开机"
            "（旧会话已死时「本会话 0/4」不能阻止开机；开机成功会开新会话）"
        )

    # 仅当「已在运行」且次数用完时，才跳过看广告
    # 若需要开机：即使本会话 0/4，也要进面板 Start（新会话会重置计数）
    if quota["exhausted"] and not need_boot:
        skip_renew = True
        stop_reason = (
            f"次数已用完（今日 {quota['used_today']}/{quota['daily_limit']}，"
            f"本会话 {quota['used_session']}/{quota['session_limit']}），"
            f"服务器已在运行，跳过看广告"
        )
        log(stop_reason)
        now = before
        result = "⏭️ 次数已用完，已跳过（服务器运行中，无需开机）"
        uptime = seconds_until(before.get("sessionExpiresAt"))
        notify_godlike(
            cfg,
            account,
            short_id,
            result,
            uptime,
            before.get("status"),
            photo=None,
            quota=quota,
            power_note=power_info.get("note"),
        )
        return None

    if quota["daily_exhausted"] and need_boot:
        # 今日次数也用完，且还需要看广告开机 → 无法开机，只能跳过
        stop_reason = (
            f"今日次数已用完（{quota['used_today']}/{quota['daily_limit']}），"
            f"无法看广告开机，请等到 UTC 次日"
        )
        log(stop_reason)
        now = before
        notify_godlike(
            cfg,
            account,
            short_id,
            f"⚠️ 无法开机：{stop_reason}",
            seconds_until(before.get("sessionExpiresAt")),
            before.get("status"),
            photo=None,
            quota=quota,
            power_note=power_info.get("note"),
        )
        return False

    if not need_boot:
        max_ext = min(max_ext, max(1, quota["remain"]))
        log(f"本轮计划续期 {max_ext} 次（受剩余 {quota['remain']} 次约束）")
    else:
        # 开机后再根据新会话的剩余次数决定续几轮
        log("先完成面板开机，再根据开机后的可续期次数决定是否续期")

    if power_info.get("action") == "failed":
        log(f"电源操作失败，仍尝试打开面板: {power_info.get('note')}")

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

            for accept_txt in ("Accept", "Accept all", "同意", "接受", "I agree", "OK"):
                hit = click_anywhere(page, [accept_txt], 3000)
                if hit:
                    log(f"已点同意弹窗: {hit}")
                    break

            # 未 running / API 开机要广告 → 必须在面板点 Start 并看广告
            boot_ok = False
            if need_boot or _status_of(api_state(cfg, server_id)) not in RUNNING_STATUSES:
                log("服务器尚未 running，在面板执行开机（Start + 必要时看广告）")
                boot_ok = playwright_start(page, cfg, server_id, watch_labels)
                before = api_state(cfg, server_id)
                quota = quota_from(before)
                log_quota(quota, "开机后可续期次数（实时）")
                st_now = _status_of(before)
                power_info["note"] = (
                    power_info.get("note") or ""
                )
                if boot_ok or st_now in RUNNING_STATUSES:
                    power_info["note"] = (
                        f"{power_info.get('note') + ' · ' if power_info.get('note') else ''}"
                        f"面板开机成功 → {st_now}"
                    ).strip(" ·")
                    log(f"开机成功，status={st_now}")
                    # 新会话开启后本会话计数会重置；按最新剩余次数安排续期
                    if quota["remain"] > 0:
                        max_ext = min(max(1, int(cfg.get("extensions_per_run", 4))), quota["remain"])
                        log(f"开机后计划再续期 {max_ext} 次（剩余 {quota['remain']}）")
                        skip_renew = False
                    else:
                        skip_renew = True
                        stop_reason = (
                            f"开机成功，但次数已用完"
                            f"（今日 {quota['used_today']}/{quota['daily_limit']}，"
                            f"本会话 {quota['used_session']}/{quota['session_limit']}），不再续期"
                        )
                        log(stop_reason)
                        last_shot = take_screenshot(page, "renew_screenshot.png")
                        notify_godlike(
                            cfg,
                            account,
                            short_id,
                            f"✅ 开机成功，跳过续期（{stop_reason}）",
                            seconds_until(before.get("sessionExpiresAt")),
                            before.get("status"),
                            photo=last_shot if last_shot.exists() else None,
                            quota=quota,
                            power_note=power_info.get("note"),
                        )
                        return True  # 开机本身已成功
                else:
                    stop_reason = f"面板开机失败，当前 status={st_now}"
                    log(stop_reason)
                    last_shot = take_screenshot(page, "debug_screenshot.png")
                    notify_godlike(
                        cfg,
                        account,
                        short_id,
                        f"❌ 开机失败：{stop_reason}",
                        seconds_until(before.get("sessionExpiresAt")),
                        before.get("status"),
                        photo=last_shot if last_shot.exists() else None,
                        quota=quota,
                        power_note=power_info.get("note") or "面板开机失败",
                    )
                    return False
            else:
                max_ext = min(max_ext, max(1, quota["remain"]))

            if skip_renew:
                last_shot = take_screenshot(page, "renew_screenshot.png")
                return None

            # ===== 单次运行内连续续期 =====
            for round_no in range(1, max_ext + 1):
                cur = api_state(cfg, server_id)
                quota = quota_from(cur)
                log("-" * 60)
                log(
                    f"第 {round_no}/{max_ext} 轮：今日 {quota['used_today']}/{quota['daily_limit']} "
                    f"| 本会话累计 {quota['used_session']}/{quota['session_limit']} "
                    f"| 剩余可续期 {quota['remain']} 次 "
                    f"| 到期 {cur.get('sessionExpiresAt')}"
                )
                if quota["remain"] <= 0:
                    stop_reason = (
                        f"可续期次数为 0（今日 {quota['used_today']}/{quota['daily_limit']}，"
                        f"本会话 {quota['used_session']}/{quota['session_limit']}）"
                    )
                    log(f"到达平台限制：{stop_reason}，停止续期")
                    break

                log("正在寻找「续期/延伸」按钮…")
                hit = click_anywhere(page, extend_labels, 45000)
                if not hit:
                    page.wait_for_timeout(5000)
                    for accept_txt in ("Accept", "Accept all", "同意", "接受", "OK"):
                        if click_anywhere(page, [accept_txt], 2000):
                            log(f"再次点掉弹窗: {accept_txt}")
                    hit = click_anywhere(page, extend_labels, 30000, exact=False)
                entered_direct = False
                if not hit:
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
                                f"可续期次数: {quota_line(quota)}",
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
                watch_ads_round(page, cfg, total, watch_labels)

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
                        quota = quota_from(now)
                        log(
                            f"第 {round_no} 轮续期成功 -> 新到期: {now.get('sessionExpiresAt')}"
                            f" | 累计: {now.get('sessionExtensions')} | 今日: {today_used(now)}"
                        )
                        log_quota(quota, "实时可续期次数")
                        break
                    time.sleep(10)
                if not round_ok:
                    log(f"第 {round_no} 轮未检测到续期生效（广告未播完 / 页面卡住 / 已达上限），停止后续轮次")
                    stop_reason = stop_reason or "本轮未检测到续期生效"
                    now = now or cur
                    break

                page.wait_for_timeout(3000)

            last_shot = take_screenshot(page, "renew_screenshot.png")
            try:
                now = api_state(cfg, server_id) or now
            except Exception:
                pass

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

    final_state = now or before
    quota = quota_from(final_state) if final_state else None
    uptime = seconds_until(final_state.get("sessionExpiresAt")) if final_state else None
    if uptime is None:
        uptime = seconds_until(before.get("sessionExpiresAt"))
    photo = last_shot if (last_shot is not None and last_shot.exists()) else None
    if photo is None:
        for name in ("renew_screenshot.png", "debug_screenshot.png"):
            cand = pathlib.Path(_shot_name(name))
            if cand.exists():
                photo = cand
                break

    if success:
        result = f"✅续期成功（+{rounds_ok * 4}h，共 {rounds_ok} 次）"
        log_quota(quota, "本轮结束可续期次数")
        notify_godlike(
            cfg,
            account,
            short_id,
            result,
            uptime,
            final_state.get("status"),
            photo=photo,
            quota=quota,
            power_note=power_info.get("note"),
        )
        return True

    reason = stop_reason or "未检测到续期生效"
    notify_godlike(
        cfg,
        account,
        short_id,
        f"⚠️续期未生效（{reason}）",
        uptime,
        final_state.get("status") if final_state else None,
        photo=photo,
        quota=quota,
        power_note=power_info.get("note"),
    )
    return False


def main():
    cfg = load_config()
    server_ids = cfg.get("server_ids") or [cfg["server_id"]]

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
        os.environ["VOER_SHOT_TAG"] = str(idx)
        try:
            ok = run_server(cfg, sid, account=account)
        except SystemExit as e:
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
            mark = "⏭️ 跳过（4 次已用完，仅检查关机/重启）"
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
