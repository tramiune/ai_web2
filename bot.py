import time
import os
import sys
import re
import argparse
import socket
import requests
import firebase_admin
import threading
from datetime import datetime, timezone
from firebase_admin import credentials, firestore
from google.cloud.firestore_v1.base_query import FieldFilter
from playwright.sync_api import sync_playwright

# --- CONFIGURATION ---
cred = credentials.Certificate("serviceAccountKey.json")
firebase_admin.initialize_app(cred)
db = firestore.client()

# Tên bot: bắt buộc khi chạy — python bot.py --name aidancing-vps1
BOT_NAME = None
bot_enabled = False
bot_enabled_lock = threading.Lock()
pending_kick_lock = threading.Lock()

def is_bot_enabled():
    with bot_enabled_lock:
        return bot_enabled

def set_bot_enabled(value):
    global bot_enabled
    with bot_enabled_lock:
        bot_enabled = bool(value)

# Khi vừa bật bot, cần "kick" lại các đơn pending đã tồn tại từ trước.
# Lý do: listener on_snapshot chỉ xử lý ADDED/MODIFIED tại thời điểm callback chạy;
# nếu bot đang TẮT lúc snapshot initial fire, các đơn pending sẽ bị bỏ qua và không được xử lý lại.
def kick_existing_pending_orders(limit=30):
    if not is_bot_enabled():
        return
    if browser_lock.locked():
        return
    if not pending_kick_lock.acquire(blocking=False):
        return
    try:
        pending = list(
            db.collection('orders')
              .where(filter=FieldFilter("status", "==", "pending"))
              .limit(limit)
              .stream()
        )
        if not pending:
            return
        print(f"🚀 [{BOT_NAME}] Bot vừa BẬT — kick lại {len(pending)} đơn pending đang tồn tại...")
        for doc in pending:
            threading.Thread(target=submit_to_aidancing, args=(doc.id,), daemon=True).start()
    except Exception as e:
        print(f"⚠️ [{BOT_NAME}] Lỗi khi kick pending orders: {e}")
    finally:
        pending_kick_lock.release()

# CREATE_URL đã được chuyển thành dynamic theo modelId trong đơn hàng
DASHBOARD_URL = "https://aidancing.net/dashboard"
WORKER_URL = "https://motionai-upload-api.traderfinn0312.workers.dev"

browser_lock = threading.Lock()
SHOW_CHROME = os.environ.get("SHOW_CHROME", "1").strip() not in ("0", "false", "False")

TELEGRAM_BOT_TOKEN = "8676046240:AAE14lDxAj9otGTjVnd8Smr2__Wg-J2dCLc"
TELEGRAM_CHAT_ID = "6067707939"
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
        time.sleep(30)

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
        if enabled:
            # Khi bật bot: quét lại các đơn pending đã có sẵn.
            threading.Thread(target=kick_existing_pending_orders, daemon=True).start()

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

def download_file(url, filename, cookies=None):
    print(f"📥 Tải file: {filename}...")
    try:
        headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'}
        response = requests.get(url, headers=headers, cookies=cookies, timeout=60)
        response.raise_for_status()
        with open(filename, 'wb') as f:
            f.write(response.content)
        return os.path.abspath(filename)
    except Exception as e:
        print(f"❌ Lỗi tải file: {e}")
        return None

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

# --- PHA 1: NẠP ĐƠN ---
def submit_to_aidancing(order_id):
    if not is_bot_enabled():
        print(f"⏸️ [{BOT_NAME}] Bot TẮT — bỏ qua nạp đơn {order_id}")
        return
    with browser_lock:
        doc_ref = db.collection('orders').document(order_id)
        doc = doc_ref.get()
        if not doc.exists: return
        data = doc.to_dict()
        if data.get('status') != 'pending': return

        print(f"\n⚡ [NẠP ĐƠN] {order_id}...")
        doc_ref.update({'status': 'processing', 'updatedAt': firestore.SERVER_TIMESTAMP})

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

        with sync_playwright() as p:
            browser = p.chromium.launch_persistent_context(
                user_data_dir=os.path.abspath("bot_chrome_profile"),
                channel="chrome", headless=(not SHOW_CHROME), slow_mo=500,
                ignore_default_args=["--enable-automation"],
                args=["--disable-blink-features=AutomationControlled",
                      "--window-position=80,80",
                      "--window-size=1280,900"]
            )
            page = browser.new_page()
            try:
                # [FIX]: Lấy danh sách Job cũ trước để tránh lấy nhầm
                print("🌐 Đang kiểm tra danh sách Job cũ trên Dashboard...")
                page.goto(DASHBOARD_URL, timeout=60000)
                page.wait_for_timeout(3000)

                balance = scrape_aidancing_balance(page)
                if balance is not None:
                    print(f"💰 Aidancing balance: {balance} Coin")
                if balance is not None and balance < AIDANCING_LOW_BALANCE_THRESHOLD:
                    short_id = order_id[-6:].upper()
                    user_name = data.get('userName', 'Khách hàng')
                    alert_low_aidancing_balance(
                        balance,
                        extra=(
                            f"\n📋 Bot đang nạp đơn: #{short_id}\n"
                            f"👤 Khách: {user_name}"
                        )
                    )

                old_job_ids = set(re.findall(r'\b\d{6}\b', page.content()))
                print(f"📦 Đã ghi nhận {len(old_job_ids)} Job ID cũ.")

                # Default ("model thường"): Aidancing model id 124
                model_id = data.get('modelId', '124')
                create_url = f"https://aidancing.net/create/general?id={model_id}"
                print(f"🌐 Vào trang tạo: {create_url}")
                page.goto(create_url, timeout=90000)
                page.set_input_files('input[name="image"]', char_path)
                page.set_input_files('input[name="video"]', vid_path)
                page.locator('button.neon-ai-2').first.click()

                print("⏳ Đợi chuyển về Dashboard và quét Job ID mới...")
                page.wait_for_url("**/dashboard**", timeout=60000)
                
                job_id = None
                for _ in range(15): # Thử tối đa 30 giây
                    page.wait_for_timeout(2000)
                    current_job_ids = set(re.findall(r'\b\d{6}\b', page.content()))
                    new_jobs = current_job_ids - old_job_ids
                    if new_jobs:
                        job_id = sorted(list(new_jobs))[-1] # Lấy ID lớn nhất/mới nhất
                        break

                if job_id:
                    print(f"🆔 LẤY ĐƯỢC JOB ID MỚI: {job_id}")
                    doc_ref.update({'aidancingJobId': job_id, 'submittedAt': firestore.SERVER_TIMESTAMP})
                else:
                    # Fallback nếu sau 30s vẫn không thấy job mới (có thể lỗi hoặc web lag)
                    print("⚠️ Không tìm thấy Job ID mới sau 30s! Dùng cách lấy mặc định...")
                    job_ids = re.findall(r'\b\d{6}\b', page.content())
                    if job_ids:
                        job_id = job_ids[0]
                        print(f"🆔 LẤY ĐƯỢC JOB ID (Fallback): {job_id}")
                        doc_ref.update({'aidancingJobId': job_id, 'submittedAt': firestore.SERVER_TIMESTAMP})

                    # Gửi thông báo Telegram: Đã nạp đơn thành công, đang render
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

            except Exception as e:
                print(f"❌ Lỗi nạp: {e}")
                doc_ref.update({'adminNote': f"Bot nạp lỗi: {str(e)}"})
            finally:
                browser.close()
                if os.path.exists(char_path): os.remove(char_path)
                if os.path.exists(vid_path): os.remove(vid_path)

# --- PHA 2: RÌNH KẾT QUẢ ---
def check_finished_orders():
    if not is_bot_enabled():
        return
    try:
        # Nếu đang nạp đơn thì không check dashboard để tránh khóa profile
        if browser_lock.locked(): return

        now = datetime.now(timezone.utc)
        processing_orders = db.collection('orders').where(filter=FieldFilter("status", "==", "processing")).stream()

        orders_to_check = []
        for doc in processing_orders:
            d = doc.to_dict()
            job_id = d.get('aidancingJobId')
            submitted_at = d.get('submittedAt')

            if not job_id or job_id == "MANUAL": continue

            # Chỉ check nếu đã nạp > 10 phút
            if submitted_at:
                if (now - submitted_at).total_seconds() > 600:
                    orders_to_check.append(doc)
            else:
                orders_to_check.append(doc)

        if not orders_to_check: return

        print(f"\n🔍 [MONITOR] Đang rình kết quả cho {len(orders_to_check)} đơn đủ 10p...")
        with browser_lock:
            with sync_playwright() as p:
                browser = p.chromium.launch_persistent_context(
                    user_data_dir=os.path.abspath("bot_chrome_profile"),
                    headless=(not SHOW_CHROME),
                    ignore_default_args=["--enable-automation"],
                    args=["--disable-blink-features=AutomationControlled",
                          "--window-position=80,80",
                          "--window-size=1280,900"]
                )
                page = browser.new_page()
                page.goto(DASHBOARD_URL, timeout=60000)
                print(f"🌐 Đang ở: {page.url}")
                time.sleep(10)

                # Nếu bị đá ra trang chủ/login thì dừng để bạn đăng nhập
                if "dashboard" not in page.url:
                    print(f"⚠️ Bot chưa đăng nhập! Bạn hãy đăng nhập trên cửa sổ Chrome đang mở này, sau đó chạy lại bot.")
                    time.sleep(60) # Để trình duyệt mở trong 1 phút cho bạn nhìn
                    browser.close()
                    return

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
                                download_link = card.locator('a[href*="download"], a:has-text("Tải"), a:has-text("Download")').first
                                ext_url = None
                                if download_link.count() > 0 and download_link.is_visible():
                                    ext_url = download_link.get_attribute('href', timeout=3000)

                                # Bước 2 (MỚI): Thử tìm thẻ video NGAY TRONG CARD NÀY (Không quét toàn trang)
                                if not ext_url:
                                    video_element = card.locator('video source, video[src]').first
                                    if video_element.count() > 0 and video_element.is_visible():
                                        ext_url = video_element.get_attribute('src')

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
                                    if not ext_url.startswith('http'): ext_url = "https://aidancing.net" + ext_url

                                    # Lấy cookies từ trình duyệt để vượt qua lỗi 401
                                    browser_cookies = {c['name']: c['value'] for c in browser.cookies()}

                                    local_vid = download_file(ext_url, f"res_{doc.id}.mp4", cookies=browser_cookies)
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
                                if page.url != DASHBOARD_URL:
                                    page.goto(DASHBOARD_URL)
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
    for ch in changes:
        if ch.type.name in ['ADDED', 'MODIFIED']:
            threading.Thread(target=submit_to_aidancing, args=(ch.document.id,), daemon=True).start()

def start_bot():
    global BOT_NAME
    parser = argparse.ArgumentParser(description='MotionAI order bot — aidancing.net')
    parser.add_argument('--name', required=True, help='Tên bot duy nhất (vd: aidancing-vps1, bot-may-nha)')
    args = parser.parse_args()
    BOT_NAME = normalize_bot_name(args.name)
    if not BOT_NAME:
        print("❌ Tên bot không hợp lệ. Dùng: python bot.py --name aidancing-vps1")
        sys.exit(1)

    print(f"📡 MotionAI BOT [{BOT_NAME}] (v3.3 - Admin on/off) đang khởi động...")
    start_bot_control_listener()

    def monitor_loop():
        while True:
            if is_bot_enabled():
                check_finished_orders()
            # Orders are only eligible for monitoring after 10 minutes (see check_finished_orders),
            # so polling faster than 60s just wastes reads/CPU.
            time.sleep(60)

    threading.Thread(target=monitor_loop, daemon=True).start()

    db.collection('orders').where(filter=FieldFilter("status", "==", "pending")).on_snapshot(on_pending_orders_snapshot)

    print(f"🟢 [{BOT_NAME}] Đang trực — lắng nghe Firestore (bật/tắt từ Admin)...")
    while True:
        time.sleep(1)

if __name__ == "__main__":
    start_bot()
