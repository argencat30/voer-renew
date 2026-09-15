#!/usr/bin/env python3
"""
Voer.host 免费服务器会话续期（Playwright 版 - 深度增强重构版）

增强功能：
1. 运行首要任务：检查服务器开机状态。若关机/停止，先点击开机并阻塞等待运行成功。
2. 双重限制识别：精准计算【今日剩余次数】与【本会话剩余次数】。
3. 满 4 次静默守护：当 4 次续期耗尽时，直接跳过广告环节，仅执行关机重启检查与状态上报。
4. 实时动态播报：每次看广告成功后，实时拉取并打印剩余可续期次数，确保次日跨界数据绝对准确。
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
from playwright.sync_api import sync_playwright

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
    "extensions_per_run": 4,   
    "headless": False,
    "use_system_chrome": False,
    "telegram_bot_token": "",
    "telegram_chat_id": "",
    "tg_title": "Godlike 续期通知",
}

def log(*a):
    print(f"[{time.strftime('%H:%M:%S')}]", *a, flush=True)

# ---------------------------------------------------------------------------
# Telegram 通知及辅助函数 (维持原有健壮架构)
# ---------------------------------------------------------------------------
def _tg_enabled(cfg) -> bool:
    return bool(cfg.get("telegram_bot_token") and cfg.get("telegram_chat_id"))

def tg_send_message(cfg, text: str) -> bool:
    if not _tg_enabled(cfg):
        return False
    token = cfg["telegram_bot_token"]
    chat_id = cfg["telegram_chat_id"]
    api = f"https://api.telegram.org/bot{token}/sendMessage"
    body = urllib.parse.urlencode({
        "chat_id": chat_id, "text": text, "parse_mode": "HTML", "disable_web_page_preview": "true"
    }).encode()
    req = urllib.request.Request(
        api, data=body, headers={"Content-Type": "application/x-www-form-urlencoded", "User-Agent": UA}, method="POST"
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
    if not _tg_enabled(cfg) or not photo_path.exists():
        return False
    token = cfg["telegram_bot_token"]
    chat_id = str(cfg["telegram_chat_id"])
    api = f"https://api.telegram.org/bot{token}/sendPhoto"
    boundary = f"----VoerBoundary{int(time.time())}"
    filename = photo_path.name
    file_data = photo_path.read_bytes()
    mime = mimetypes.guess_type(filename)[0] or "image/png"

    parts = []
    parts.append(f"--{boundary}\r\nContent-Disposition: form-data; name=\"chat_id\"\r\n\r\n{chat_id}\r\n".encode())
    if caption:
        parts.append(f"--{boundary}\r\nContent-Disposition: form-data; name=\"caption\"\r\n\r\n{caption}\r\n".encode())
        parts.append(f"--{boundary}\r\nContent-Disposition: form-data; name=\"parse_mode\"\r\n\r\nHTML\r\n".encode())
    parts.append(f"--{boundary}\r\nContent-Disposition: form-data; name=\"photo\"; filename=\"{filename}\"\r\nContent-Type: {mime}\r\n\r\n".encode() + file_data + b"\r\n")
    parts.append(f"--{boundary}--\r\n".encode())
    
    req = urllib.request.Request(
        api, data=b"".join(parts), headers={"Content-Type": f"multipart/form-data; boundary={boundary}", "User-Agent": UA}, method="POST"
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            data = json.loads(r.read().decode())
            if data.get("ok"):
                log("Telegram 截图已发送")
                return True
    except Exception as e:
        log(f"Telegram 发图异常: {e}")
    return False

def notify(cfg, title: str, lines: list, photo: pathlib.Path | None = None):
    text = f"<b>{title}</b>\n" + "\n".join(lines)
    log("通知内容:\n" + text.replace("<b>", "").replace("</b>", ""))
    if not _tg_enabled(cfg):
        return
    if photo and photo.exists():
        if len(text) <= 1000:
            tg_send_photo(cfg, photo, caption=text)
        else:
            tg_send_photo(cfg, photo, caption=title)
            tg_send_message(cfg, text)
    else:
        tg_send_message(cfg, text)

def fmt_local_time(dt=None) -> str:
    return (dt or datetime.now()).strftime("%Y-%m-%d %H:%M:%S")

def fmt_duration(seconds) -> str:
    if seconds is None: return "—"
    s = max(0, int(seconds))
    h, rem = divmod(s, 3600)
    return f"{h}h {rem // 60:02d}m"

def seconds_until(iso_str) -> int | None:
    if not iso_str: return None
    try:
        t = str(iso_str).strip().replace("Z", "+00:00")
        dt = datetime.fromisoformat(t)
        if dt.tzinfo is None: dt = dt.replace(tzinfo=timezone.utc)
        return max(0, int((dt - datetime.now(timezone.utc)).total_seconds()))
    except Exception: return None

def status_text(status) -> str:
    s = (status or "").lower()
    if s in ("running", "online"): return "✅ 服务器已在运行中"
    if s in ("stopped", "offline"): return "⏹️ 服务器已停止"
    if s in ("starting", "provisioning", "pending", "starting_node"): return "🔄 服务器启动中"
    if s in ("restarting", "migrating"): return "🔄 服务器重启中"
    if s == "maintenance": return "🛠️ 系统维护中"
    if s in ("crashed", "error", "provisioning_error", "supervisor_error"): return "❌ 服务器异常"
    return f"ℹ️ {status or '未知'}"

def account_email_from_server(server) -> str:
    if not server: return ""
    owner = (server.get("access") or {}).get("owner") or {}
    if isinstance(owner, dict):
        for k in ("email", "displayName", "username"):
            if owner.get(k): return str(owner[k])
    for k in ("ownerEmail", "email"):
        if server.get(k): return str(server[k])
    return ""

def fetch_account_email(cfg) -> str:
    req = urllib.request.Request(
        "https://voer.host/api/auth/me", headers={"Cookie": f"token={cfg['token']}", "User-Agent": UA, "Accept": "application/json"}
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return str(json.loads(r.read().decode()).get("user", {}).get("email") or "")
    except Exception: return ""

def notify_godlike(cfg, account, server_id, result, uptime_sec, status, photo=None):
    lines = [
        f"⏰运行时间: {fmt_local_time()}",
        f"🖥️账号: {account or '—'}",
        f"🖥️服务器: {server_id}",
        f"🔢下次可续期: {fmt_duration(uptime_sec)}",
        f"📊续期结果: {result}",
        f"📊开机状态: {status_text(status)}",
    ]
    shot = photo if isinstance(photo, pathlib.Path) else pathlib.Path(photo) if photo else None
    notify(cfg, cfg.get("tg_title") or "Godlike 续期通知", lines, photo=shot)

def _jwt_hint(token: str) -> str:
    t = (token or "").strip()
    if not t: return "空"
    return f"长度={len(t)}, 段数={len(t.split('.'))}, 开头={t[:8]}..."

def today_used(server: dict) -> int:
    raw = int(server.get("sessionExtensionsToday") or 0)
    date_val = server.get("sessionExtensionsDate")
    if not date_val: return 0
    try:
        if str(date_val)[:10] == datetime.now(timezone.utc).strftime("%Y-%m-%d"):
            return max(0, raw)
        return 0
    except Exception: return 0

def load_config():
    cfg = dict(DEFAULT_CONFIG)
    if CONFIG_PATH.exists():
        try:
            cfg.update(json.loads(CONFIG_PATH.read_text(encoding="utf-8")))
        except Exception: pass

    for k, v in [("VOER_SERVER_ID", "server_id"), ("VOER_TOKEN", "token"), 
                 ("TELEGRAM_BOT_TOKEN", "telegram_bot_token"), ("TELEGRAM_CHAT_ID", "telegram_chat_id"),
                 ("TG_TITLE", "tg_title")]:
        env_val = os.environ.get(k, "").strip().strip('"').strip("'")
        if env_val: cfg[v] = env_val

    for k, v in [("VOER_ADS_PER_EXTENSION", "ads_per_extension"), 
                 ("VOER_AD_DURATION_SEC", "ad_duration_sec"), ("VOER_EXTENSIONS_PER_RUN", "extensions_per_run")]:
        if os.environ.get(k): cfg[v] = int(os.environ[k])

    if not cfg.get("server_id") or not cfg.get("token") or "在这里填" in cfg["server_id"]:
        log("缺少必要配置 VOER_SERVER_ID 或 VOER_TOKEN，即将退出。")
        sys.exit(1)

    cfg["server_ids"] = list(dict.fromkeys(x for x in re.split(r"[,;\s]+", cfg["server_id"].strip()) if x))
    return cfg

def api_state(cfg, server_id=None):
    url = f"https://voer.host/api/servers/{server_id or cfg['server_id']}"
    req = urllib.request.Request(
        url, headers={"Cookie": f"token={cfg['token']}", "User-Agent": UA, "Accept": "application/json"}
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.loads(r.read().decode())["server"]
    except urllib.error.HTTPError as e:
        log(f"API 请求失败 [HTTP {e.code}]: 可能是 Token 失效或 UUID 错误。")
        raise SystemExit(1) from e
    except Exception as e:
        log(f"网络异常: {e}")
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
                ]
                for maker in makers:
                    try:
                        loc = maker()
                        if loc.count() and loc.is_visible():
                            loc.click(timeout=3000)
                            return f"{t}@{frame.url[:60]}"
                    except Exception: pass
        time.sleep(1.2)
    return None

def _shot_name(base: str) -> str:
    tag = os.environ.get("VOER_SHOT_TAG", "1")
    return base if tag == "1" else f"{pathlib.Path(base).stem}_{tag}{pathlib.Path(base).suffix}"

def take_screenshot(page, name="screenshot.png") -> pathlib.Path:
    path = pathlib.Path(_shot_name(name))
    try:
        page.wait_for_timeout(800)
        page.screenshot(path=str(path), full_page=True, type="png")
    except Exception: pass
    return path

# ---------------------------------------------------------------------------
# 核心重构逻辑：重启控制与续期控制交火
# ---------------------------------------------------------------------------
def run_server(cfg, server_id, account=""):
    url = f"https://voer.host/panel/server/{server_id}"
    short_id = server_id[:8] + "…"

    if "--status" in sys.argv:
        s = api_state(cfg, server_id)
        log(f"状态: {s.get('status')} | 今日已续: {today_used(s)}/4 | 累计已续: {s.get('sessionExtensions')}")
        return True

    success = False
    before = {}
    now = {}
    last_shot = pathlib.Path(_shot_name("renew_screenshot.png"))
    rounds_ok = 0
    stop_reason = ""

    watch_labels = ["觀看廣告", "观看广告", "Watch ad", "Watch", "开始", "開始"]
    extend_labels = ["延伸", "延长", "延長", "续期", "續期", "Extend", "Renew", "Watch ads"]
    start_labels = ["Start", "起動", "启动", "開機", "开机", "Boot"] 

    with sync_playwright() as p:
        launch = dict(headless=cfg["headless"], args=["--disable-blink-features=AutomationControlled", "--window-size=1400,1000", "--no-sandbox"])
        if cfg["use_system_chrome"]: launch["channel"] = "chrome"
        browser = p.chromium.launch(**launch)
        ctx = browser.new_context(viewport={"width": 1400, "height": 1000})
        ctx.add_cookies([{"name": "token", "value": cfg["token"], "domain": "voer.host", "path": "/", "secure": True}])
        page = ctx.new_page()

        try:
            log(f"打开页面: {url}")
            page.goto(url, wait_until="domcontentloaded", timeout=60000)
            page.wait_for_timeout(5000)

            # ======== 步骤一：前置注入关机检查与重启守护 ========
            before = api_state(cfg, server_id)
            current_status = (before.get("status") or "").lower()
            
            if current_status in ("stopped", "offline", "crashed"):
                log(f"检测到服务器处于关机/异常状态 ({current_status})，准备执行开机...")
                start_hit = click_anywhere(page, start_labels, 30000, exact=False)
                
                if start_hit:
                    log(f"成功点击开机按钮: {start_hit}，正在等待系统响应...")
                    wait_end = time.time() + 300 # 最多等待 5 分钟
                    boot_success = False
                    while time.time() < wait_end:
                        temp_state = api_state(cfg, server_id)
                        temp_status = (temp_state.get("status") or "").lower()
                        if temp_status in ("running", "online"):
                            log(f"服务器已成功启动并进入运行状态！")
                            before = temp_state # 更新底座状态，保证后续数据新鲜度
                            boot_success = True
                            break
                        time.sleep(10)
                    if not boot_success:
                        log("开机等待超时，跳出等待池。")
                else:
                    log("未找到明确的开机按钮，继续向下执行流程。")
            else:
                log(f"服务器当前处于正常状态 ({current_status})，无需重启。")

            # ======== 步骤二：重置弹窗遮挡 ========
            for accept_txt in ("Accept", "Accept all", "同意", "接受", "I agree", "OK"):
                if click_anywhere(page, [accept_txt], 3000): break

            # ======== 步骤三：精准计算双重水位并执行跳过拦截 ========
            used_today = today_used(before)
            session_ext = int(before.get("sessionExtensions") or 0)
            
            available_today = max(0, 4 - used_today)
            available_session = max(0, 4 - session_ext)
            available_renewals = min(available_today, available_session)
            
            log(f"当前实时可用续期次数: {available_renewals} (今日剩余: {available_today}/4, 本会话剩余: {available_session}/4)")

            if available_renewals <= 0:
                log("提示：4次续期均已完成（触及当日或本会话上限），直接跳过续期，仅完成了关机与重启检查。")
                uptime = seconds_until(before.get("sessionExpiresAt"))
                notify_godlike(cfg, account, short_id, "✅已达上限，本次仅检查并确认运行状态", uptime, before.get("status"), None)
                return None # 优雅返回 None，标识外层状态为跳过

            # ======== 步骤四：执行剩余可用续期轮次 ========
            for round_no in range(1, available_renewals + 1):
                log("-" * 60)
                log(f"开始执行第 {round_no}/{available_renewals} 轮续期...")

                # 寻找入口
                hit = click_anywhere(page, extend_labels, 45000)
                if not hit:
                    page.wait_for_timeout(5000)
                    for accept_txt in ("Accept", "Accept all", "同意", "接受", "OK"):
                        click_anywhere(page, [accept_txt], 2000)
                    hit = click_anywhere(page, extend_labels, 30000, exact=False)
                
                entered_direct = False
                if not hit:
                    hit2 = click_anywhere(page, watch_labels, 30000) or click_anywhere(page, watch_labels, 15000, exact=False)
                    if hit2:
                        hit = "Watch ad(直入)"
                        entered_direct = True
                
                if not hit:
                    stop_reason = "找不到「延伸/续期」按钮"
                    log(f"终止：{stop_reason}")
                    break
                
                log(f"已点击续期入口: {hit}")
                page.wait_for_timeout(3000)

                if not entered_direct:
                    click_anywhere(page, watch_labels, 30000) or click_anywhere(page, watch_labels, 20000, exact=False)
                
                log("已打开广告流程，等待 Ad ready…")
                page.wait_for_timeout(8000)

                # 看广告流程
                total = int(cfg["ads_per_extension"])
                for i in range(1, total + 1):
                    ad_hit = click_anywhere(page, ["Watch ad", "觀看廣告", "观看广告"], 75000)
                    if not ad_hit: break
                    log(f"正在播放第 {i}/{total} 个广告…")
                    page.wait_for_timeout(int(cfg["ad_duration_sec"]) * 1000)
                    click_anywhere(page, ["Close", "關閉", "关闭"], 60000)
                    page.wait_for_timeout(6000)

                # 等待并验证续期是否生效
                end_wait = time.time() + 180
                round_ok = False
                while time.time() < end_wait:
                    now = api_state(cfg, server_id)
                    if now and (now.get("sessionExtensions", 0) > session_ext or now.get("sessionExpiresAt") != before.get("sessionExpiresAt")):
                        rounds_ok += 1
                        round_ok = True
                        success = True
                        
                        # ======== 步骤五：验证成功，实时播报剩余次数 ========
                        rt_used_today = today_used(now)
                        rt_session_ext = int(now.get("sessionExtensions") or 0)
                        rt_available = min(max(0, 4 - rt_used_today), max(0, 4 - rt_session_ext))
                        
                        log(f"第 {round_no} 轮续期生效！(+4h)")
                        log(f"实时状态更新 -> [剩余可续期次数: {rt_available}] (当前本会话已累计 {rt_session_ext}/4 | 今日已累计 {rt_used_today}/4)")
                        
                        before = now # 状态前移，供下一轮计算使用
                        session_ext = rt_session_ext
                        break
                    time.sleep(10)

                if not round_ok:
                    stop_reason = "本轮看广告未检测到时间增加（可能页面卡死或异常）"
                    log(f"验证失败：{stop_reason}")
                    now = now or before
                    break

                page.wait_for_timeout(3000)

            last_shot = take_screenshot(page, "renew_screenshot.png")

        except SystemExit: raise
        except Exception as e:
            log(f"运行时环境异常: {e}")
            return False
        finally:
            page.wait_for_timeout(1500)
            browser.close()

    # 执行 Telegram 最终消息推送逻辑
    final_state = now or before
    uptime = seconds_until(final_state.get("sessionExpiresAt"))
    photo = last_shot if (last_shot is not None and last_shot.exists()) else None

    if success:
        result = f"✅续期成功（+{rounds_ok * 4}h，本次延期 {rounds_ok} 轮）"
        notify_godlike(cfg, account, short_id, result, uptime, final_state.get("status"), photo=photo)
        return True
    else:
        reason = stop_reason or "未检测到任何续期动作生效"
        notify_godlike(cfg, account, short_id, f"⚠️续期中断（{reason}）", uptime, final_state.get("status"), photo=photo)
        return False

def main():
    cfg = load_config()
    server_ids = cfg.get("server_ids") or [cfg["server_id"]]
    account = fetch_account_email(cfg) if "--status" not in sys.argv else ""

    total = len(server_ids)
    results = []
    for idx, sid in enumerate(server_ids, 1):
        log("=" * 60)
        log(f"[{idx}/{total}] 挂载服务器 {sid[:8]} 开始任务...")
        os.environ["VOER_SHOT_TAG"] = str(idx)
        try:
            ok = run_server(cfg, sid, account=account)
        except SystemExit as e:
            log(f"API认证拒绝或发生致命网络错误，结束所有列队任务。")
            raise
        except Exception as e:
            log(f"服务器 {sid[:8]}… 未知崩溃: {e}")
            ok = False
        results.append((sid, ok))

    log("=" * 60)
    fail = sum(1 for _, ok in results if ok is False)
    for sid, ok in results:
        mark = "✅ 成功" if ok is True else ("⏭️ 守护跳过(次数已满)" if ok is None else "❌ 失败")
        log(f"  {mark}  {sid[:8]}…")
    log(f"任务汇总: 完成或安全跳过 {total - fail}/{total} 台，失败 {fail} 台。")
    if fail: sys.exit(3)

if __name__ == "__main__":
    main()
