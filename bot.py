import time
import os
import sys
import re
import base64
import argparse
import socket
import queue
import requests
import firebase_admin
import threading
from datetime import datetime, timezone
from firebase_admin import credentials, firestore
from google.cloud.firestore_v1.base_query import FieldFilter
from playwright.sync_api import sync_playwright
from aidancing_api import AidancingApiClient

# --- CONFIGURATION ---
cred = credentials.Certificate("serviceAccountKey.json")
firebase_admin.initialize_app(cred)
db = firestore.client()

# Tên bot: bắt buộc khi chạy — python bot.py --name aidancing-vps1
BOT_NAME = None
bot_enabled = False
bot_enabled_lock = threading.Lock()

def is_bot_enabled():
    with bot_enabled_lock:
        return bot_enabled

def set_bot_enabled(value):
    global bot_enabled
    with bot_enabled_lock:
        bot_enabled = bool(value)

# CREATE_URL đã được chuyển thành dynamic theo modelId trong đơn hàng
AIDANCING_ORIGIN = "https://aidancing.net"
DASHBOARD_URL = f"{AIDANCING_ORIGIN}/dashboard"
WORKER_URL = "https://motionai-upload-api.traderfinn0312.workers.dev"
BOT_CHROME_PROFILE = os.path.abspath(os.environ.get("BOT_CHROME_PROFILE", "bot_chrome_profile_web2"))

browser_lock = threading.Lock()
_pending_order_queue = []
_pending_queue_lock = threading.Lock()
_pending_worker_started = False
_submitting_orders = set()
_submitting_orders_lock = threading.Lock()
MIN_RENDER_SEC = int(os.environ.get("BOT_MIN_RENDER_SEC", "600"))
_processing_cache = {}
_processing_cache_lock = threading.Lock()
HEARTBEAT_SEC = int(os.environ.get("BOT_HEARTBEAT_SEC", "60"))


class PersistentApiPool:
    """Giữ 1 kết nối CDP + 1 tab nền suốt phiên bot — không mở/đóng Chrome mỗi lần poll."""

    def __init__(self):
        self._playwright = None
        self._browser = None
        self._api = None

    def get(self):
        if self._api is not None and self._api._page_alive():
            return self._api
        self.reset()
        self._playwright = sync_playwright().start()
        self._browser = launch_aidancing_browser(self._playwright)
        self._api = AidancingApiClient(self._browser.context, persistent=True)
        print("🔌 Session API cố định — 1 tab nền (fetch API, không reload dashboard)")
        return self._api

    def reset(self):
        if self._api:
            try:
                self._api.shutdown()
            except Exception:
                pass
        self._api = None
        if self._browser:
            try:
                self._browser.close()
            except Exception:
                pass
        self._browser = None
        if self._playwright:
            try:
                self._playwright.stop()
            except Exception:
                pass
        self._playwright = None


_api_pool = PersistentApiPool()

_pw_queue = queue.Queue()
_pw_worker_started = False
_pw_worker_lock = threading.Lock()
_pw_worker_tid = None


def _ensure_playwright_worker():
    global _pw_worker_started
    with _pw_worker_lock:
        if _pw_worker_started:
            return
        _pw_worker_started = True
        threading.Thread(
            target=_playwright_worker_loop,
            daemon=True,
            name="playwright-worker",
        ).start()


def _playwright_worker_loop():
    global _pw_worker_tid
    _pw_worker_tid = threading.get_ident()
    while True:
        fn, args, kwargs, done = _pw_queue.get()
        try:
            done["result"] = fn(*args, **kwargs)
        except Exception as e:
            done["error"] = e
        finally:
            done["event"].set()


def run_playwright(fn, *args, **kwargs):
    """Playwright sync API chỉ chạy trên 1 thread — gọi hàm này từ thread khác."""
    if _pw_worker_tid == threading.get_ident():
        return fn(*args, **kwargs)
    _ensure_playwright_worker()
    done = {"event": threading.Event(), "result": None, "error": None}
    _pw_queue.put((fn, args, kwargs, done))
    done["event"].wait()
    if done["error"] is not None:
        raise done["error"]
    return done["result"]


def _persistent_api():
    return run_playwright(_api_pool.get)


def _reset_persistent_api():
    run_playwright(_api_pool.reset)


def _pw_create_job(model_id, char_path, vid_path):
    api = _api_pool.get()
    return api.create_job(model_id, char_path, vid_path)


def _pw_poll_orders(orders_to_check):
    api = _api_pool.get()
    job_ids = [str(doc.to_dict().get('aidancingJobId')) for doc in orders_to_check]
    jobs_map = api.find_jobs_by_ids(job_ids)
    for doc in orders_to_check:
        job_id = str(doc.to_dict().get('aidancingJobId'))
        print(f"🧐 API — Job {job_id}...")
        job = jobs_map.get(int(job_id))
        if not job:
            print(f"❌ Không thấy job {job_id} trong API (3 trang đầu)")
            continue
        status = (job.get('status') or '').upper()
        print(f"   status={status}, outputFileId={job.get('outputFileId')}")
        if status == 'COMPLETED' and job.get('outputFileId'):
            print(f"🎉 Job {job_id} HOÀN TẤT — tải file {job['outputFileId']}...")
            try:
                local_vid = api.download_file(job['outputFileId'], f"res_{doc.id}.mp4")
                _complete_order_with_video(doc, local_vid)
            except Exception as e:
                print(f"⚠️ Lỗi tải/hoàn đơn {doc.id}: {e}")
        elif status in ('FAILED', 'ERROR', 'CANCELLED'):
            print(f"❌ Job {job_id} thất bại trên aidancing ({status})")
            order_data = doc.to_dict()
            err_detail = f'Aidancing job {job_id} {status}: {job.get("errorMessage") or ""}'
            notify_internal_error_telegram(doc.id, order_data, err_detail, 'render aidancing')
            cost_coins = order_data.get('costCoins', 0)
            user_id = order_data.get('userId')
            if cost_coins > 0 and user_id:
                try:
                    db.collection('users').document(user_id).update({'coins': firestore.Increment(cost_coins)})
                except Exception as e:
                    print(f"⚠️ Hoàn coin lỗi: {e}")
            db.collection('orders').document(doc.id).update({
                'status': 'failed',
                'adminNote': firestore.DELETE_FIELD,
                'systemNote': 'Đơn hàng xử lý không thành công, hệ thống đã hoàn lại coin.',
                'updatedAt': firestore.SERVER_TIMESTAMP
            })
        else:
            print(f"⏳ Job {job_id} vẫn {status}")


def _processing_monitor_state():
    """Đọc từ RAM — không query Firestore mỗi lần poll."""
    now = datetime.now(timezone.utc)
    eligible = []
    with _processing_cache_lock:
        processing_count = len(_processing_cache)
        for doc in _processing_cache.values():
            d = doc.to_dict() or {}
            job_id = d.get('aidancingJobId')
            submitted_at = d.get('submittedAt')
            if not job_id or job_id == "MANUAL":
                continue
            if submitted_at:
                if (now - submitted_at).total_seconds() > MIN_RENDER_SEC:
                    eligible.append(doc)
            else:
                eligible.append(doc)
    return eligible, processing_count


def on_processing_orders_snapshot(keys, changes, read_time):
    """Listener: chỉ read Firestore khi đơn vào/ra khỏi processing (không poll lặp)."""
    with _processing_cache_lock:
        for ch in changes:
            doc = ch.document
            oid = doc.id
            if ch.type.name == 'REMOVED':
                _processing_cache.pop(oid, None)
                continue
            d = doc.to_dict() or {}
            if d.get('status') == 'processing':
                _processing_cache[oid] = doc
            else:
                _processing_cache.pop(oid, None)


def start_processing_listener():
    db.collection('orders').where(
        filter=FieldFilter("status", "==", "processing")
    ).on_snapshot(on_processing_orders_snapshot)
    print("👂 Listener processing orders — cache RAM, không query Firestore mỗi lần poll")


def _monitor_sleep_seconds(eligible_count, processing_count):
    """Không có webhook aidancing — chỉ poll; interval dài khi không có việc."""
    idle = int(os.environ.get("BOT_POLL_IDLE_SEC", "300"))
    wait_render = int(os.environ.get("BOT_POLL_WAIT_RENDER_SEC", "120"))
    active = int(os.environ.get("BOT_POLL_ACTIVE_SEC", "90"))
    if processing_count == 0:
        return idle
    if eligible_count == 0:
        return wait_render
    return active


def _warm_api_session_loop():
    if not use_api_mode():
        return
    _ensure_playwright_worker()
    while True:
        if is_bot_enabled():
            try:
                run_playwright(_api_pool.get)
                print("✅ Tab nền aidancing sẵn sàng — poll qua fetch (không F5 dashboard)")
                return
            except Exception as e:
                print(f"⚠️ Chờ Chrome CDP để khởi tạo session API: {e}")
                try:
                    run_playwright(_api_pool.reset)
                except Exception:
                    pass
        time.sleep(20)

def ensure_cdp_available(cdp_url, timeout=3):
    try:
        url = cdp_url.rstrip("/") + "/json/version"
        requests.get(url, timeout=timeout)
        return True
    except Exception:
        return False

def _cdp_not_running_error(cdp_url):
    return RuntimeError(
        f"Chrome CDP chưa chạy tại {cdp_url}. "
        "Mở Chrome ở terminal RIÊNG và GIỮ chạy (đừng Ctrl+C), rồi chạy bot:\n"
        "  /Applications/Google\\ Chrome.app/Contents/MacOS/Google\\ Chrome \\\n"
        "    --remote-debugging-port=9223 --remote-allow-origins='*' \\\n"
        "    --user-data-dir=\"$HOME/.chrome-aidancing-wallpaper\" \\\n"
        "    --profile-directory=\"Profile 14\""
    )

def _ensure_pending_worker():
    global _pending_worker_started
    with _pending_queue_lock:
        if _pending_worker_started:
            return
        _pending_worker_started = True
        threading.Thread(target=_pending_order_worker, daemon=True).start()

def _pending_order_worker():
    while True:
        order_id = None
        with _pending_queue_lock:
            if _pending_order_queue:
                order_id = _pending_order_queue.pop(0)
        if order_id:
            submit_to_aidancing(order_id)
        else:
            time.sleep(0.5)

AIDANCING_BLOCKED_MARKERS = (
    "bảo trì", "bao tri", "maintenance", "under maintenance",
    "scheduled maintenance", "hệ thống đang", "temporarily unavailable",
    "service unavailable", "coming soon",
)

STEALTH_INIT_SCRIPT = """
Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
window.chrome = window.chrome || { runtime: {}, loadTimes: function() {}, csi: function() {} };
Object.defineProperty(navigator, 'languages', { get: () => ['vi-VN', 'vi', 'en-US', 'en'] });
Object.defineProperty(navigator, 'plugins', { get: () => [1, 2, 3, 4, 5] });
const originalQuery = window.navigator.permissions.query;
window.navigator.permissions.query = (parameters) =>
  parameters.name === 'notifications'
    ? Promise.resolve({ state: Notification.permission })
    : originalQuery(parameters);
"""

class AidancingBrowserSession:
    """Wrapper: CDP mode không đóng Chrome của user khi bot xong."""

    def __init__(self, context, close_context_on_exit=True):
        self.context = context
        self.close_context_on_exit = close_context_on_exit
        self._pages = []

    def new_page(self):
        page = self.context.new_page()
        self._pages.append(page)
        return page

    def cookies(self, urls=None):
        if urls:
            return self.context.cookies(urls)
        return self.context.cookies()

    def clear_cookies(self):
        self.context.clear_cookies()

    def close(self):
        for page in self._pages:
            try:
                page.close()
            except Exception:
                pass
        self._pages.clear()
        if self.close_context_on_exit:
            try:
                self.context.close()
            except Exception:
                pass

def close_extra_aidancing_tabs(session, keep_page):
    """Đóng tab aidancing phụ (do nút Tải mở target=_blank)."""
    for p in list(session.context.pages):
        if p == keep_page:
            continue
        try:
            u = p.url or ''
            if 'aidancing' in u or u.startswith('blob:') or 'proxy/files' in u:
                p.close()
        except Exception:
            pass

def _apply_stealth(context):
    try:
        context.add_init_script(STEALTH_INIT_SCRIPT)
    except Exception as e:
        print(f"⚠️ Không gắn stealth script: {e}")

def _aidancing_chrome_args():
    args = [
        "--disable-blink-features=AutomationControlled",
        "--no-first-run",
        "--no-default-browser-check",
    ]
    if os.environ.get("BOT_CHROME_OFFSCREEN", "0") == "1":
        args.append("--window-position=-2400,-2400")
    return args

def _chrome_profile_dir():
    return os.path.abspath(os.environ.get("BOT_CHROME_PROFILE", BOT_CHROME_PROFILE))

def launch_aidancing_browser(playwright):
    cdp_url = os.environ.get("BOT_CDP_URL", "").strip()
    if cdp_url:
        if not ensure_cdp_available(cdp_url):
            raise _cdp_not_running_error(cdp_url)
        browser = playwright.chromium.connect_over_cdp(cdp_url)
        context = browser.contexts[0] if browser.contexts else browser.new_context(
            locale="vi-VN",
            timezone_id="Asia/Ho_Chi_Minh",
            viewport={"width": 1280, "height": 800},
        )
        _apply_stealth(context)
        print(f"🔗 Nối Chrome qua CDP ({cdp_url}) — dùng Chrome thật, không đóng khi bot xong.")
        return AidancingBrowserSession(context, close_context_on_exit=False)

    profile_dir = _chrome_profile_dir()
    kwargs = dict(
        user_data_dir=profile_dir,
        headless=False,
        slow_mo=int(os.environ.get("BOT_SLOW_MO", "500")),
        ignore_default_args=["--enable-automation"],
        args=_aidancing_chrome_args(),
        viewport={"width": 1280, "height": 800},
        locale="vi-VN",
        timezone_id="Asia/Ho_Chi_Minh",
    )
    try:
        context = playwright.chromium.launch_persistent_context(channel="chrome", **kwargs)
    except Exception as e:
        print(f"⚠️ Không mở được Chrome ({e}), dùng Chromium bundled...")
        context = playwright.chromium.launch_persistent_context(**kwargs)
    _apply_stealth(context)
    return AidancingBrowserSession(context, close_context_on_exit=True)

def _aidancing_page_info(page):
    try:
        return f"{page.url} | {page.title()}"
    except Exception:
        return page.url

def is_aidancing_blocked(page):
    try:
        url = (page.url or "").lower()
        if any(x in url for x in ("maintenance", "maintain", "bao-tri")):
            return True
        combined = f"{page.title() or ''} {page.content()}".lower()
        return any(marker in combined for marker in AIDANCING_BLOCKED_MARKERS)
    except Exception:
        return False

def _raise_if_aidancing_blocked(page):
    if not is_aidancing_blocked(page):
        return
    print(f"🚫 Aidancing chặn/trang bảo trì: {_aidancing_page_info(page)}")
    raise RuntimeError(
        "Aidancing hiển thị trang bảo trì hoặc chặn trình duyệt tự động. "
        "Thường do profile Chrome BOT chưa có cookie đăng nhập (Chrome thường của bạn vẫn vào được vì đã login). "
        "Cách xử lý: thoát hết Chrome (Cmd+Q), copy profile Default đã login sang ~/.chrome-aidancing-bot "
        "(xem README hoặc hướng dẫn setup), mở Chrome CDP rồi BOT_CDP_URL=http://127.0.0.1:9223 python3 bot.py --name wallpaper --mode api"
    )

def _aidancing_on_dashboard(page):
    u = page.url.lower()
    if "login" in u or "signin" in u or "sign-in" in u:
        return False
    if is_aidancing_blocked(page):
        return False
    return "dashboard" in u

def goto_aidancing_dashboard(page, session, login_wait_sec=120):
    """Mở dashboard; xử lý redirect loop (cookie hỏng) và chờ đăng nhập thủ công."""

    def _goto(url):
        page.goto(url, timeout=60000, wait_until="domcontentloaded")
        print(f"📄 {_aidancing_page_info(page)}")
        _raise_if_aidancing_blocked(page)

    try:
        _goto(DASHBOARD_URL)
    except Exception as e:
        err = str(e)
        if "Aidancing hiển thị" in err:
            raise
        if "ERR_TOO_MANY_REDIRECTS" in err or "too many redirects" in err.lower():
            print("⚠️ Redirect loop — xóa cookie profile bot và thử lại...")
            try:
                session.clear_cookies()
            except Exception as ce:
                print(f"   (không xóa được cookie: {ce})")
            _goto(AIDANCING_ORIGIN)
            page.wait_for_timeout(2000)
            _goto(DASHBOARD_URL)
        else:
            raise

    page.wait_for_timeout(2000)
    if _aidancing_on_dashboard(page):
        return

    print(f"⚠️ Chưa vào Dashboard (URL: {page.url})")
    print("👉 Đăng nhập aidancing.net trên cửa sổ Chrome BOT (thư mục bot_chrome_profile).")
    print("   Chrome thường của bạn dùng profile khác — cần login 1 lần trên cửa sổ bot.")

    deadline = time.time() + login_wait_sec
    while time.time() < deadline:
        page.wait_for_timeout(3000)
        if _aidancing_on_dashboard(page):
            print("✅ Đã vào Dashboard sau khi đăng nhập.")
            return
        try:
            _goto(DASHBOARD_URL)
        except Exception as e:
            if "Aidancing hiển thị" in str(e):
                raise

    raise RuntimeError(
        f"Không vào được Dashboard sau {login_wait_sec}s. "
        f"Đăng nhập trên cửa sổ Chrome bot rồi chạy lại. URL: {page.url}"
    )

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "8855918099:AAHmPUWTe6_dicXyh0nseADQomVv6MGKjGQ")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "6067707939")
AIDANCING_LOW_BALANCE_THRESHOLD = 10

def normalize_bot_name(name):
    name = (name or '').strip().lower()
    name = re.sub(r'[^a-z0-9_-]', '-', name)
    name = re.sub(r'-+', '-', name).strip('-')
    return name[:64]

def ensure_bot_registered():
    ref = db.collection('bots').document(BOT_NAME)
    doc = ref.get()
    now = firestore.SERVER_TIMESTAMP
    if not doc.exists:
        ref.set({
            'name': BOT_NAME,
            'displayName': BOT_NAME,
            'enabled': False,
            'hostname': socket.gethostname(),
            'createdAt': now,
            'lastSeenAt': now,
            'startedAt': now,
        })
        print(f"🆕 Bot mới đăng ký trên Firestore: {BOT_NAME} (mặc định TẮT — bật trên Admin)")
    else:
        ref.set({
            'name': BOT_NAME,
            'lastSeenAt': now,
            'startedAt': now,
            'hostname': socket.gethostname(),
        }, merge=True)

def bot_heartbeat_loop():
    while True:
        try:
            if BOT_NAME:
                ref = db.collection('bots').document(BOT_NAME)
                try:
                    ref.update({'lastSeenAt': firestore.SERVER_TIMESTAMP})
                except Exception as e:
                    # Doc bị admin xóa — đăng ký lại đầy đủ (cùng tên = cùng 1 bot)
                    if 'NOT_FOUND' in str(e) or 'No document to update' in str(e):
                        ensure_bot_registered()
                    else:
                        raise
        except Exception as e:
            print(f"⚠️ Heartbeat lỗi: {e}")
        time.sleep(HEARTBEAT_SEC)

def on_bot_config_snapshot(keys, changes, read_time):
    # Document watch callback: (sorted_keys, DocumentChange[], read_time) — not a DocumentSnapshot.
    if not changes:
        return
    enabled = False
    for change in changes:
        doc = change.document
        if getattr(doc, 'exists', False):
            enabled = bool((doc.to_dict() or {}).get('enabled', False))
        break
    prev = is_bot_enabled()
    set_bot_enabled(enabled)
    if enabled != prev:
        status = "🟢 BẬT — bot đang xử lý đơn" if enabled else "🔴 TẮT — bot không làm gì"
        print(f"\n[{BOT_NAME}] Admin đổi trạng thái: {status}\n")

def start_bot_control_listener():
    ensure_bot_registered()
    doc = db.collection('bots').document(BOT_NAME).get()
    set_bot_enabled(bool(doc.to_dict().get('enabled', False)) if doc.exists else False)
    status = "🟢 BẬT" if is_bot_enabled() else "🔴 TẮT"
    print(f"[{BOT_NAME}] Trạng thái hiện tại: {status}")
    if not is_bot_enabled():
        print("⏸️  Bot đang TẮT. Vào Admin → Bots để bật.")

    db.collection('bots').document(BOT_NAME).on_snapshot(on_bot_config_snapshot)
    threading.Thread(target=bot_heartbeat_loop, daemon=True).start()

def send_telegram_message(text):
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
        payload = {
            "chat_id": TELEGRAM_CHAT_ID,
            "text": text,
            "parse_mode": "HTML"
        }
        res = requests.post(url, json=payload, headers={"Content-Type": "application/json"}, timeout=10)
        if res.status_code != 200:
            print(f"❌ Lỗi gửi tin nhắn Telegram: {res.status_code} - {res.text}")
    except Exception as e:
        print(f"❌ Lỗi kết nối gửi Telegram: {e}")

_INTERNAL_ERROR_MARKERS = (
    'aidancing', '/api/proxy/', 'proxy/jobs', 'proxy/files',
    '401', '503', '502', '429', 'đăng nhập lại', 'bảo trì',
    'chrome cdp', 'connect_over_cdp', 'econnrefused', 'target closed',
    'different thread', 'job id aidancing', 'dashboard', 'create/general',
    'bot nạp', 'maintenance',
)
_ERROR_TELEGRAM_COOLDOWN = 900
_error_telegram_sent = {}
_error_telegram_lock = threading.Lock()
_session_error_backoff = {}
SESSION_ERROR_BACKOFF_SEC = 300

def is_internal_bot_error(err):
    s = (err or '').lower()
    return any(m in s for m in _INTERNAL_ERROR_MARKERS)

def notify_internal_error_telegram(order_id, order_data, err, context=''):
    now = time.time()
    with _error_telegram_lock:
        last = _error_telegram_sent.get(order_id, 0)
        if now - last < _ERROR_TELEGRAM_COOLDOWN:
            return
        _error_telegram_sent[order_id] = now
    short_id = order_id[-6:].upper()
    user_name = (order_data or {}).get('userName', 'Khách hàng')
    user_email = (order_data or {}).get('userEmail', 'N/A')
    ctx = f" ({context})" if context else ""
    err_text = (err or '')[:500]
    msg = (
        f"🚨 <b>BOT LỖI NỘI BỘ{ctx}</b>\n\n"
        f"🆔 Mã đơn: #{short_id}\n"
        f"👤 Khách: {user_name}\n"
        f"📧 Email: {user_email}\n"
        f"⚠️ Chi tiết:\n<code>{err_text}</code>"
    )
    send_telegram_message(msg)

def apply_bot_error_update(doc_ref, order_id, order_data, err, context='nạp đơn'):
    """Lỗi Aidancing/hạ tầng bot → Telegram admin, không hiện adminNote cho khách."""
    if is_internal_bot_error(err):
        notify_internal_error_telegram(order_id, order_data, err, context)
        _session_error_backoff[order_id] = time.time() + SESSION_ERROR_BACKOFF_SEC
        return True
    doc_ref.update({
        'adminNote': f"Bot nạp lỗi: {err}",
        'updatedAt': firestore.SERVER_TIMESTAMP,
    })
    return False

def _pending_submit_backoff_active(order_id):
    return time.time() < _session_error_backoff.get(order_id, 0)

def scrape_aidancing_balance(page):
    """Đọc số coin còn lại trên header aidancing.net (vd: 101.0)."""
    try:
        val = page.evaluate('''() => {
            const pick = (s) => {
                const m = String(s).trim().match(/^(\\d+(?:\\.\\d+)?)$/);
                return m ? parseFloat(m[1]) : null;
            };
            const scopes = document.querySelectorAll('header *, nav *, [class*="wallet"], [class*="balance"], [class*="coin"]');
            for (const el of scopes) {
                if (el.children.length > 0) continue;
                const v = pick(el.textContent);
                if (v !== null && v >= 0 && v < 100000) return v;
            }
            return null;
        }''')
        if val is not None:
            return float(val)
    except Exception as e:
        print(f"⚠️ Không đọc được balance aidancing: {e}")
    return None

def alert_low_aidancing_balance(balance, extra=''):
    if balance is None or balance >= AIDANCING_LOW_BALANCE_THRESHOLD:
        return
    msg = (
        f"🚨🚨 <b>CẢNH BÁO KHẨN — SẮP HẾT COIN AIDANCING!</b>\n\n"
        f"💰 Số dư aidancing.net: <b>{balance}</b> Coin\n"
        f"⚠️ Dưới ngưỡng {AIDANCING_LOW_BALANCE_THRESHOLD} Coin — "
        f"<b>nạp gấp</b> trước khi bot không tạo được đơn!\n"
        f"{extra}"
    )
    send_telegram_message(msg)

def download_file(url, filename, cookies=None, referer=None, retries=2):
    print(f"📥 Tải file (requests): {filename}...")
    headers = {
        'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
        'Referer': referer or f'{AIDANCING_ORIGIN}/dashboard',
        'Origin': AIDANCING_ORIGIN,
    }
    last_err = None
    for attempt in range(1, retries + 1):
        try:
            response = requests.get(url, headers=headers, cookies=cookies, timeout=120)
            if response.status_code in (503, 502, 429) and attempt < retries:
                wait = 5 * attempt
                print(f"⚠️ HTTP {response.status_code} — thử lại {attempt}/{retries} sau {wait}s...")
                time.sleep(wait)
                continue
            response.raise_for_status()
            with open(filename, 'wb') as f:
                f.write(response.content)
            return os.path.abspath(filename)
        except Exception as e:
            last_err = e
            if attempt < retries:
                time.sleep(3 * attempt)
    print(f"❌ Lỗi tải file: {last_err}")
    return None

def download_aidancing_result(session, page, url, filename, download_locator=None):
    """Tải video kết quả aidancing — không click mở tab (aidancing dùng target=_blank)."""
    print(f"📥 Tải kết quả aidancing: {filename}...")
    if not url.startswith('http'):
        url = AIDANCING_ORIGIN + url

    def save_bytes(data):
        with open(filename, 'wb') as f:
            f.write(data)
        return os.path.abspath(filename)

    def session_get(target_url, label):
        try:
            resp = session.context.request.get(
                target_url,
                headers={'Referer': DASHBOARD_URL, 'Origin': AIDANCING_ORIGIN},
                timeout=120000,
            )
            if resp.ok:
                save_bytes(resp.body())
                print(f"✅ {label}")
                return os.path.abspath(filename)
            print(f"⚠️ {label} — HTTP {resp.status}")
        except Exception as e:
            print(f"⚠️ {label} — {e}")
        return None

    # 1) Tải thẳng URL proxy/API — không click (tránh mở tab mới)
    result = session_get(url, "Tải direct URL (session cookie)")
    if result:
        return result

    # 2) fetch() ngay trên dashboard (credentials: include)
    try:
        data = page.evaluate('''async (videoUrl) => {
            const r = await fetch(videoUrl, { credentials: 'include' });
            if (!r.ok) return { ok: false, status: r.status };
            const buf = await r.arrayBuffer();
            const bytes = new Uint8Array(buf);
            let binary = '';
            const chunk = 0x8000;
            for (let i = 0; i < bytes.length; i += chunk) {
                binary += String.fromCharCode.apply(null, bytes.subarray(i, i + chunk));
            }
            return { ok: true, b64: btoa(binary) };
        }''', url)
        if data and data.get('ok') and data.get('b64'):
            save_bytes(base64.b64decode(data['b64']))
            print("✅ Tải qua fetch in-page")
            return os.path.abspath(filename)
        if data:
            print(f"⚠️ In-page fetch HTTP {data.get('status')}")
    except Exception as e:
        print(f"⚠️ In-page fetch lỗi: {e}")

    # 3) Nút Tải mở tab video mới (target=_blank) — bắt tab, lấy src, đóng tab
    if download_locator is not None and download_locator.count() > 0:
        new_page = None
        try:
            print("🖱️ Nút Tải mở tab mới — bắt tab video...")
            with session.context.expect_page(timeout=30000) as page_info:
                download_locator.click()
            new_page = page_info.value
            new_page.wait_for_load_state('domcontentloaded', timeout=30000)
            new_page.wait_for_timeout(1500)
            video_url = new_page.evaluate('''() => {
                const v = document.querySelector('video');
                if (v) {
                    const s = v.querySelector('source');
                    const src = (s && s.src) || v.src || v.currentSrc || '';
                    if (src) return src;
                }
                return location.href;
            }''')
            if video_url and not video_url.startswith('http'):
                video_url = AIDANCING_ORIGIN + video_url
            if video_url:
                print(f"🔗 URL tab video: {video_url[:100]}...")
                result = session_get(video_url, "Tải từ tab video")
                if result:
                    return result
                result = session_get(url, "Tải lại URL gốc sau tab")
                if result:
                    return result
        except Exception as e:
            print(f"⚠️ Xử lý tab video: {e}")
        finally:
            if new_page:
                try:
                    new_page.close()
                except Exception:
                    pass
            close_extra_aidancing_tabs(session, page)

    # 4) Fallback requests + cookie
    try:
        cookie_list = session.cookies(urls=[AIDANCING_ORIGIN, f"{AIDANCING_ORIGIN}/"])
        jar = {c['name']: c['value'] for c in cookie_list}
    except Exception:
        jar = {c['name']: c['value'] for c in session.cookies()}
    return download_file(url, filename, cookies=jar, referer=DASHBOARD_URL, retries=3)

def upload_to_r2(file_path, folder="results"):
    print(f"📤 Đang upload lên R2...")
    try:
        file_name = f"{folder}/{int(time.time() * 1000)}_{os.path.basename(file_path)}"
        url = f"{WORKER_URL}/?file={requests.utils.quote(file_name)}&t={int(time.time() * 1000)}"
        with open(file_path, 'rb') as f:
            response = requests.post(url, data=f, headers={'Content-Type': 'video/mp4'}, timeout=120)
            if response.status_code == 200:
                return response.json().get('url')
    except Exception as e:
        print(f"❌ Lỗi R2: {e}")
    return None

def send_completion_email(order_id, order_data, result_link):
    user_email = order_data.get('userEmail')
    user_name = order_data.get('userName', 'Khách hàng')
    service_type = order_data.get('serviceType', 'copy-motion-photo')
    
    if not user_email:
        print("⚠️ Không tìm thấy Email của khách để gửi thông báo hoàn thành đơn.")
        return
        
    print(f"📧 Đang gửi email thông báo hoàn thành đơn tới: {user_email}...")
    
    # Ánh xạ tên dịch vụ tiếng Việt
    service_label = service_type
    if service_type == 'copy-motion-photo':
        service_label = "AI Copy Chuyển Động Vào Ảnh (20s)"
    elif service_type == 'copy-motion-multi':
        service_label = "AI Copy Nhảy Nhiều Người"
    elif service_type == 'char-to-video-fashion':
        service_label = "AI Copy Thời Trang"
    elif service_type == 'char-to-video-ads':
        service_label = "AI Copy Sản Phẩm"

    short_order_id = order_id[-6:].upper()
    
    payload = {
        "service_id": "service_6r6rd2q",
        "template_id": "template_09eir3r",
        "user_id": "92pP97oTzMGR4p_Zp",
        "template_params": {
            "user_name": user_name,
            "user_email": user_email,
            "order_id": short_order_id,
            "result_link": result_link,
            "service_label": service_label
        }
    }
    
    try:
        url = "https://api.emailjs.com/api/v1.0/email/send"
        response = requests.post(url, json=payload, headers={"Content-Type": "application/json"}, timeout=15)
        if response.status_code == 200 or response.text == "OK":
            print(f"✅ Gửi email thông báo qua EmailJS thành công!")
        else:
            print(f"❌ Lỗi gửi email qua EmailJS: {response.status_code} - {response.text}")
    except Exception as e:
        print(f"❌ Lỗi kết nối khi gửi email thông báo qua EmailJS: {e}")

def use_api_mode():
    return os.environ.get("BOT_MODE", "browser").strip().lower() == "api"

def _complete_order_with_video(doc, local_vid):
    """Upload R2 + cập nhật Firestore + thông báo."""
    r2_url = upload_to_r2(local_vid)
    if not r2_url:
        return False
    db.collection('orders').document(doc.id).update({
        'status': 'completed',
        'resultLink': r2_url,
        'updatedAt': firestore.SERVER_TIMESTAMP
    })
    print(f"✅ ĐÃ TRẢ HÀNG CHO ĐƠN {doc.id}")
    try:
        order_data = doc.to_dict()
        short_id = doc.id[-6:].upper()
        user_name = order_data.get('userName', 'Khách hàng')
        user_email = order_data.get('userEmail', 'N/A')
        char_img = order_data.get('characterImageLink', '')
        msg = (
            f"✅ <b>ĐƠN HÀNG HOÀN THÀNH</b>\n\n"
            f"🆔 Mã đơn: #{short_id}\n"
            f"👤 Khách: {user_name}\n"
            f"📧 Email: {user_email}\n"
        )
        if char_img:
            msg += f"📸 Ảnh đầu vào: <a href=\"{char_img}\">Xem ảnh gốc</a>\n"
        msg += f"📹 Kết quả: <a href=\"{r2_url}\">Xem video kết quả</a>"
        send_telegram_message(msg)
    except Exception as tele_err:
        print(f"⚠️ Lỗi gửi thông báo Telegram hoàn thành: {tele_err}")
    try:
        send_completion_email(doc.id, doc.to_dict(), r2_url)
    except Exception as mail_err:
        print(f"⚠️ Không gửi được email thông báo: {mail_err}")
    if os.path.exists(local_vid):
        os.remove(local_vid)
    return True

def check_finished_orders_api():
    """Monitor qua GET /api/proxy/jobs — fetch trên tab nền, không reload."""
    if not is_bot_enabled() or browser_lock.locked():
        return
    orders_to_check, _ = _processing_monitor_state()
    if not orders_to_check:
        return

    print(f"\n🔍 [MONITOR/API] Poll {len(orders_to_check)} đơn (fetch, không reload trang)...")
    with browser_lock:
        try:
            run_playwright(_pw_poll_orders, orders_to_check)
        except Exception as e:
            err = str(e)
            print(f"❌ Lỗi monitor API: {e}")
            if any(x in err for x in ('ECONNREFUSED', 'Chrome CDP', 'connect_over_cdp', 'Target closed', 'different thread')):
                try:
                    run_playwright(_api_pool.reset)
                except Exception:
                    pass

def _mark_order_processing(doc_ref, job_id):
    """Chỉ chuyển processing sau khi aidancing đã nhận job."""
    doc_ref.update({
        'status': 'processing',
        'aidancingJobId': str(job_id),
        'submittedAt': firestore.SERVER_TIMESTAMP,
        'updatedAt': firestore.SERVER_TIMESTAMP,
    })


def submit_to_aidancing(order_id):
    if not is_bot_enabled():
        print(f"⏸️ [{BOT_NAME}] Bot TẮT — bỏ qua nạp đơn {order_id}")
        return
    if _pending_submit_backoff_active(order_id):
        return
    with _submitting_orders_lock:
        if order_id in _submitting_orders:
            print(f"⏭️ [{BOT_NAME}] Đơn {order_id} đang nạp — bỏ qua trùng lặp")
            return
        _submitting_orders.add(order_id)
    try:
        with browser_lock:
            doc_ref = db.collection('orders').document(order_id)
            doc = doc_ref.get()
            if not doc.exists:
                return
            data = doc.to_dict()
            if data.get('status') != 'pending':
                return

            print(f"\n⚡ [NẠP ĐƠN] {order_id}... (giữ pending cho đến khi aidancing OK)")

            char_path = None
            vid_path = None

            # Thử tải tối đa 2 lần
            for attempt in range(1, 3):
                if attempt > 1: print(f"🔄 Thử lại lần {attempt}...")
                char_path = download_file(data.get('characterImageLink'), f"char_{order_id}.png")
                vid_path = download_file(data.get('referenceVideoLink'), f"vid_{order_id}.mp4")

                if char_path and vid_path:
                    break
                time.sleep(2)

            if not char_path or not vid_path:
                print(f"❌ Không thể tải file sau 2 lần thử cho đơn {order_id}")
                # Hoàn tiền cho khách
                cost_coins = data.get('costCoins', 0)
                user_id = data.get('userId')
                if cost_coins > 0 and user_id:
                    try:
                        db.collection('users').document(user_id).update({
                            'coins': firestore.Increment(cost_coins)
                        })
                        print(f"💰 Đã hoàn lại {cost_coins} coin cho user {user_id}")
                    except Exception as e:
                        print(f"⚠️ Lỗi khi hoàn tiền cho user {user_id}: {e}")

                doc_ref.update({
                    'status': 'failed',
                    'adminNote': 'Ảnh hoặc video quý khách tải lên không tồn tại, hệ thống đã hoàn lại coin.',
                    'updatedAt': firestore.SERVER_TIMESTAMP
                })

                # Gửi thông báo Telegram: Đơn hàng thất bại
                try:
                    short_id = order_id[-6:].upper()
                    user_name = data.get('userName', 'Khách hàng')
                    user_email = data.get('userEmail', 'N/A')
                    msg = (
                        f"❌ <b>ĐƠN HÀNG THẤT BẠI</b>\n\n"
                        f"🆔 Mã đơn: #{short_id}\n"
                        f"👤 Khách: {user_name}\n"
                        f"📧 Email: {user_email}\n"
                        f"📝 Lý do: Không thể tải ảnh/video nhân vật quý khách tải lên."
                    )
                    send_telegram_message(msg)
                except Exception as tele_err:
                    print(f"⚠️ Lỗi gửi thông báo Telegram thất bại: {tele_err}")
                if char_path and os.path.exists(char_path): os.remove(char_path)
                if vid_path and os.path.exists(vid_path): os.remove(vid_path)
                return

            if use_api_mode():
                try:
                    model_id = data.get('modelId', '124')
                    print(f"🚀 [API] Nạp đơn model {model_id}...")
                    job_id = run_playwright(_pw_create_job, model_id, char_path, vid_path)
                    print(f"🆔 [API] Job mới: {job_id}")
                    _mark_order_processing(doc_ref, job_id)
                    _session_error_backoff.pop(order_id, None)
                    print(f"✅ Đơn {order_id} → processing (aidancing đã nhận job)")
                    try:
                        short_id = order_id[-6:].upper()
                        msg = (
                            f"⚙️ <b>ĐƠN HÀNG ĐANG XỬ LÝ</b>\n\n"
                            f"🆔 Mã đơn: #{short_id}\n"
                            f"🤖 Job ID aidancing: <code>{job_id}</code>\n"
                            f"⏳ Đang render (API mode)..."
                        )
                        send_telegram_message(msg)
                    except Exception:
                        pass
                except Exception as e:
                    print(f"❌ Lỗi nạp API: {e}")
                    err = str(e)
                    if any(x in err for x in ('ECONNREFUSED', 'Chrome CDP chưa chạy', 'connect_over_cdp', 'Target closed', 'different thread')):
                        try:
                            run_playwright(_api_pool.reset)
                        except Exception:
                            pass
                    apply_bot_error_update(doc_ref, order_id, data, err, 'nạp API')
                finally:
                    if char_path and os.path.exists(char_path):
                        os.remove(char_path)
                    if vid_path and os.path.exists(vid_path):
                        os.remove(vid_path)
                return

            def _pw_browser_submit():
                with sync_playwright() as p:
                    browser = launch_aidancing_browser(p)
                    page = browser.new_page()
                    try:
                        print("🌐 Đang kiểm tra danh sách Job cũ trên Dashboard...")
                        goto_aidancing_dashboard(page, browser)
                        balance = scrape_aidancing_balance(page)
                        if balance is not None:
                            print(f"💰 Aidancing balance: {balance} Coin")
                        if balance is not None and balance < AIDANCING_LOW_BALANCE_THRESHOLD:
                            short_id = order_id[-6:].upper()
                            user_name = data.get('userName', 'Khách hàng')
                            alert_low_aidancing_balance(
                                balance,
                                extra=f"\n📋 Bot đang nạp đơn: #{short_id}\n👤 Khách: {user_name}"
                            )
                        old_job_ids = set(re.findall(r'\b\d{6}\b', page.content()))
                        print(f"📦 Đã ghi nhận {len(old_job_ids)} Job ID cũ.")
                        model_id = data.get('modelId', '124')
                        create_url = f"{AIDANCING_ORIGIN}/create/general?id={model_id}"
                        print(f"🌐 Vào trang tạo: {create_url}")
                        page.goto(create_url, timeout=90000)
                        page.set_input_files('input[name="image"]', char_path)
                        page.set_input_files('input[name="video"]', vid_path)
                        page.locator('button.neon-ai-2').first.click()
                        print("⏳ Đợi chuyển về Dashboard và quét Job ID mới...")
                        page.wait_for_url("**/dashboard**", timeout=60000)
                        job_id = None
                        for _ in range(15):
                            page.wait_for_timeout(2000)
                            current_job_ids = set(re.findall(r'\b\d{6}\b', page.content()))
                            new_jobs = current_job_ids - old_job_ids
                            if new_jobs:
                                job_id = sorted(list(new_jobs))[-1]
                                break
                        if not job_id:
                            print("⚠️ Không tìm thấy Job ID mới sau 30s! Dùng cách lấy mặc định...")
                            job_ids = re.findall(r'\b\d{6}\b', page.content())
                            if job_ids:
                                job_id = job_ids[0]
                                print(f"🆔 LẤY ĐƯỢC JOB ID (Fallback): {job_id}")
                        return job_id
                    finally:
                        browser.close()

            try:
                job_id = run_playwright(_pw_browser_submit)
                if job_id:
                    print(f"🆔 LẤY ĐƯỢC JOB ID MỚI: {job_id}")
                    _mark_order_processing(doc_ref, job_id)
                    _session_error_backoff.pop(order_id, None)
                    print(f"✅ Đơn {order_id} → processing (aidancing đã nhận job)")
                    try:
                        short_id = order_id[-6:].upper()
                        user_name = data.get('userName', 'Khách hàng')
                        user_email = data.get('userEmail', 'N/A')
                        msg = (
                            f"⚙️ <b>ĐƠN HÀNG ĐANG XỬ LÝ</b>\n\n"
                            f"🆔 Mã đơn: #{short_id}\n"
                            f"👤 Khách: {user_name}\n"
                            f"📧 Email: {user_email}\n"
                            f"🤖 Job ID aidancing: <code>{job_id}</code>\n"
                            f"⏳ Đang render trên aidancing.net..."
                        )
                        send_telegram_message(msg)
                    except Exception as tele_err:
                        print(f"⚠️ Lỗi gửi thông báo Telegram xử lý: {tele_err}")
                else:
                    err = 'Bot nạp xong nhưng không lấy được Job ID aidancing — vẫn pending, thử lại sau.'
                    apply_bot_error_update(doc_ref, order_id, data, err, 'nạp browser')
            except Exception as e:
                print(f"❌ Lỗi nạp: {e}")
                apply_bot_error_update(doc_ref, order_id, data, str(e), 'nạp browser')
            finally:
                if os.path.exists(char_path):
                    os.remove(char_path)
                if os.path.exists(vid_path):
                    os.remove(vid_path)
    finally:
        with _submitting_orders_lock:
            _submitting_orders.discard(order_id)

# --- PHA 2: RÌNH KẾT QUẢ ---
def check_finished_orders():
    if use_api_mode():
        try:
            check_finished_orders_api()
        except Exception as e:
            print(f"❌ Lỗi monitor API: {e}")
        return
    if not is_bot_enabled():
        return
    try:
        # Nếu đang nạp đơn thì không check dashboard để tránh khóa profile
        if browser_lock.locked():
            return

        orders_to_check, _ = _processing_monitor_state()
        if not orders_to_check:
            return

        print(f"\n🔍 [MONITOR] Đang rình kết quả cho {len(orders_to_check)} đơn đủ {MIN_RENDER_SEC // 60}p...")
        with browser_lock:
            with sync_playwright() as p:
                browser = launch_aidancing_browser(p)
                page = browser.new_page()
                try:
                    goto_aidancing_dashboard(page, browser)
                except RuntimeError as e:
                    print(f"⚠️ {e}")
                    time.sleep(60)
                    browser.close()
                    return
                print(f"🌐 Đang ở: {page.url}")
                time.sleep(10)

                for doc in orders_to_check:
                    job_id = str(doc.to_dict().get('aidancingJobId'))
                    print(f"🧐 Đang tìm Job {job_id}...")

                    # Thử tìm text trong toàn bộ trang
                    if job_id not in page.content():
                        print(f"❌ Không thấy mã {job_id} trên trang này. Kiểm tra xem Job có ở trang 2 không?")
                        continue

                    # [FIX]: Tìm chính xác thẻ Card chứa đơn hàng này bằng cách mở rộng dần từ phần tử nhỏ nhất
                    # Đảm bảo không bao giờ bị dính vào thẻ List to đùng chứa nhiều đơn hàng (khiến cho bị nhận nhầm trạng thái của đơn khác)
                    containers = page.locator(f'div:has-text("{job_id}")')
                    count = containers.count()
                    card = None
                    
                    for i in range(count - 1, -1, -1):
                        container = containers.nth(i)
                        text = container.inner_text()
                        
                        # Đếm số lượng Job ID (6 số) trong thẻ này
                        ids_inside = set(re.findall(r'\b\d{6}\b', text))
                        if len(ids_inside) > 1:
                            # Nếu thẻ chứa nhiều hơn 1 đơn hàng -> Nó là thẻ List cha. Dừng lại, dùng thẻ con trước đó.
                            break
                        card = container

                    if card and card.is_visible():
                        text = card.inner_text()
                        # [FIX]: Bỏ "Tải Xuống" và "Download" khỏi điều kiện vì nút này luôn hiển thị trên UI kể cả khi đang xử lý
                        if any(x in text for x in ["Đã xong", "Success"]):
                            print(f"🎉 Job {job_id} HOÀN TẤT! Đang xử lý...")
                            # ... (giữ nguyên logic xử lý thành công)
                            try:
                                # Bước 1: Thử lấy link trực tiếp từ nút Tải TRONG CARD NÀY
                                ext_url = None
                                video_element = card.locator('video source, video[src]').first
                                if video_element.count() > 0 and video_element.is_visible():
                                    ext_url = video_element.get_attribute('src') or video_element.get_attribute('currentSrc')

                                download_link = card.locator(
                                    'a[href*="proxy/files"], a[href*="download"], a:has-text("Tải"), a:has-text("Download")'
                                ).first
                                if not ext_url and download_link.count() > 0 and download_link.is_visible():
                                    ext_url = download_link.get_attribute('href', timeout=3000)

                                # Bước 3 (Dự phòng): Click vào card để vào trang chi tiết lấy video
                                if not ext_url:
                                    try:
                                        print(f"🖱️ Click vào Job {job_id} để lấy link video...")
                                        card.click()
                                        page.wait_for_timeout(5000)
                                        # [FIX]: Kiểm tra xem trang CÓ THỰC SỰ CHUYỂN HAY KHÔNG
                                        if "dashboard" not in page.url:
                                            video_element = page.locator('video source, video[src]').first
                                            if video_element.count() > 0:
                                                ext_url = video_element.get_attribute('src')
                                            page.goto(DASHBOARD_URL) # Quay lại Dashboard
                                            time.sleep(3)
                                        else:
                                            print(f"❌ Nút click không chuyển trang. Bỏ qua để tránh lấy nhầm video ngoài Dashboard.")
                                    except Exception as e:
                                        print(f"❌ Lỗi khi vào trang chi tiết cho Job {job_id}: {e}")

                                # Bước 3: Tải file nếu đã có link (kèm cookies)
                                if ext_url:
                                    if not ext_url.startswith('http'):
                                        ext_url = AIDANCING_ORIGIN + ext_url

                                    dl_btn = download_link if (download_link.count() > 0 and ext_url) else None
                                    local_vid = download_aidancing_result(
                                        browser, page, ext_url, f"res_{doc.id}.mp4", download_locator=dl_btn
                                    )
                                    if local_vid:
                                        r2_url = upload_to_r2(local_vid)
                                        if r2_url:
                                            db.collection('orders').document(doc.id).update({
                                                'status': 'completed',
                                                'resultLink': r2_url,
                                                'updatedAt': firestore.SERVER_TIMESTAMP
                                            })
                                            print(f"✅ ĐÃ TRẢ HÀNG CHO ĐƠN {doc.id}")
                                            
                                            # Gửi thông báo Telegram: Đơn hàng hoàn thành
                                            try:
                                                order_data = doc.to_dict()
                                                short_id = doc.id[-6:].upper()
                                                user_name = order_data.get('userName', 'Khách hàng')
                                                user_email = order_data.get('userEmail', 'N/A')
                                                char_img = order_data.get('characterImageLink', '')
                                                msg = (
                                                    f"✅ <b>ĐƠN HÀNG HOÀN THÀNH</b>\n\n"
                                                    f"🆔 Mã đơn: #{short_id}\n"
                                                    f"👤 Khách: {user_name}\n"
                                                    f"📧 Email: {user_email}\n"
                                                )
                                                if char_img:
                                                    msg += f"📸 Ảnh đầu vào: <a href=\"{char_img}\">Xem ảnh gốc</a>\n"
                                                msg += f"📹 Kết quả: <a href=\"{r2_url}\">Xem video kết quả</a>"
                                                send_telegram_message(msg)
                                            except Exception as tele_err:
                                                print(f"⚠️ Lỗi gửi thông báo Telegram hoàn thành: {tele_err}")

                                            # Gửi mail thông báo tự động cho khách hàng
                                            try:
                                                order_data = doc.to_dict()
                                                send_completion_email(doc.id, order_data, r2_url)
                                            except Exception as mail_err:
                                                print(f"⚠️ Không gửi được email thông báo: {mail_err}")
                                                
                                            os.remove(local_vid)
                            except Exception as e:
                                print(f"⚠️ Lỗi xử lý Job {job_id}: {e}")
                            finally:
                                close_extra_aidancing_tabs(browser, page)
                                if page.url != DASHBOARD_URL:
                                    try:
                                        page.goto(DASHBOARD_URL, wait_until='domcontentloaded', timeout=60000)
                                        time.sleep(2)
                                    except Exception:
                                        pass
                        elif any(x in text for x in ["Chưa thành công", "Thất bại", "Failed", "Error"]):
                            print(f"❌ Job {job_id} THẤT BẠI TRÊN AIDANCING!")
                            order_data = doc.to_dict()
                            
                            # Hoàn tiền cho khách
                            cost_coins = order_data.get('costCoins', 0)
                            user_id = order_data.get('userId')
                            if cost_coins > 0 and user_id:
                                try:
                                    db.collection('users').document(user_id).update({
                                        'coins': firestore.Increment(cost_coins)
                                    })
                                    print(f"💰 Đã hoàn lại {cost_coins} coin cho user {user_id}")
                                except Exception as e:
                                    print(f"⚠️ Lỗi khi hoàn tiền cho user {user_id}: {e}")

                            db.collection('orders').document(doc.id).update({
                                'status': 'failed',
                                'adminNote': 'Ảnh hoặc video quý khách tải lên không hợp lệ, hệ thống đã hoàn lại coin.',
                                'updatedAt': firestore.SERVER_TIMESTAMP
                            })

                            # Gửi thông báo Telegram: Đơn hàng thất bại
                            try:
                                order_data = doc.to_dict()
                                short_id = doc.id[-6:].upper()
                                user_name = order_data.get('userName', 'Khách hàng')
                                user_email = order_data.get('userEmail', 'N/A')
                                msg = (
                                    f"❌ <b>ĐƠN HÀNG THẤT BẠI</b>\n\n"
                                    f"🆔 Mã đơn: #{short_id}\n"
                                    f"👤 Khách: {user_name}\n"
                                    f"📧 Email: {user_email}\n"
                                    f"📝 Lý do: Ảnh/video tham chiếu không hợp lệ."
                                )
                                send_telegram_message(msg)
                            except Exception as tele_err:
                                print(f"⚠️ Lỗi gửi thông báo Telegram thất bại: {tele_err}")
                        else:
                            print(f"⏳ Job {job_id} vẫn đang render...")
                browser.close()
    except Exception as e:
        print(f"❌ Lỗi monitor: {e}")

def on_pending_orders_snapshot(keys, changes, read_time):
    if not is_bot_enabled():
        return
    _ensure_pending_worker()
    with _pending_queue_lock:
        for ch in changes:
            if ch.type.name != 'ADDED':
                continue
            oid = ch.document.id
            with _submitting_orders_lock:
                if oid in _submitting_orders:
                    continue
            if oid not in _pending_order_queue:
                _pending_order_queue.append(oid)
                print(f"📋 Xếp hàng nạp đơn: {oid} (còn {len(_pending_order_queue)} trong queue)")


def _rescan_pending_orders_loop():
    """Thử lại đơn pending sau khi session Aidancing được sửa (mỗi 5 phút)."""
    while True:
        time.sleep(SESSION_ERROR_BACKOFF_SEC)
        if not is_bot_enabled():
            continue
        try:
            docs = db.collection('orders').where(
                filter=FieldFilter("status", "==", "pending")
            ).limit(20).stream()
            with _pending_queue_lock:
                for doc in docs:
                    oid = doc.id
                    if _pending_submit_backoff_active(oid):
                        continue
                    with _submitting_orders_lock:
                        if oid in _submitting_orders:
                            continue
                    if oid not in _pending_order_queue:
                        _pending_order_queue.append(oid)
                        print(f"🔄 Hàng đợi thử lại đơn pending: {oid}")
        except Exception as e:
            print(f"⚠️ rescan pending: {e}")

def start_bot():
    global BOT_NAME
    parser = argparse.ArgumentParser(description='Wallpaper/Nhay Cloud order bot — aidancing.net')
    parser.add_argument('--name', required=True, help='Tên bot duy nhất (vd: aidancing-vps1, bot-may-nha)')
    parser.add_argument('--mode', choices=['browser', 'api'], default=None,
                        help='browser=scrape dashboard (mặc định), api=gọi /api/proxy/jobs')
    args = parser.parse_args()
    if args.mode:
        os.environ['BOT_MODE'] = args.mode
    BOT_NAME = normalize_bot_name(args.name)
    if not BOT_NAME:
        print("❌ Tên bot không hợp lệ. Dùng: python bot.py --name aidancing-vps1")
        sys.exit(1)

    print(f"📡 Wallpaper BOT [{BOT_NAME}] (v3.8 - mode={os.environ.get('BOT_MODE', 'browser')}) đang khởi động...")
    cdp_url = os.environ.get("BOT_CDP_URL", "").strip()
    if cdp_url:
        if ensure_cdp_available(cdp_url):
            print(f"✅ Chrome CDP sẵn sàng: {cdp_url}")
        else:
            print(f"⚠️  BOT_CDP_URL={cdp_url} nhưng Chrome chưa mở CDP!")
            print("    → Mở Chrome CDP ở terminal KHÁC trước, giữ chạy, rồi bot mới nối được.")
    start_bot_control_listener()
    start_processing_listener()

    if use_api_mode():
        _ensure_playwright_worker()
        threading.Thread(target=_warm_api_session_loop, daemon=True).start()

    def monitor_loop():
        while True:
            eligible, processing = _processing_monitor_state()
            if is_bot_enabled():
                check_finished_orders()
            if use_api_mode():
                sleep_sec = _monitor_sleep_seconds(len(eligible), processing)
            else:
                sleep_sec = 60 if processing else int(os.environ.get("BOT_POLL_IDLE_SEC", "300"))
            time.sleep(sleep_sec)

    threading.Thread(target=monitor_loop, daemon=True).start()
    threading.Thread(target=_rescan_pending_orders_loop, daemon=True).start()

    db.collection('orders').where(filter=FieldFilter("status", "==", "pending")).on_snapshot(on_pending_orders_snapshot)

    print(f"🟢 [{BOT_NAME}] Đang trực — lắng nghe Firestore (bật/tắt từ Admin)...")
    while True:
        time.sleep(1)

if __name__ == "__main__":
    start_bot()
