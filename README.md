# Nhay Cloud Bot (web2) — Hướng dẫn VPS & xử lý lỗi

Bot tự động nạp đơn lên **aidancing.net** và trả kết quả về Firebase (project `wallpaper-6cbbe`).

Chạy trên **GCP VPS** với Chrome headless (xvfb) + bot **API mode** (không scrape dashboard).

---

## Thông số cố định

| Mục | Giá trị |
|-----|---------|
| VPS | `hoang1432001@136.119.193.255` |
| Repo trên VPS | `~/ai_web2` |
| Bot name | `nhaycloud_vps_bot` |
| Chrome CDP port | **9223** |
| Chrome profile | `~/.chrome-aidancing-wallpaper` / **Profile 14** (Lan Trần) |
| Tài khoản Aidancing | Google Auth — **lantran03122001** |
| tmux sessions | `chrome`, `bot` |

---

## 1. Chuẩn bị lần đầu (máy mới / VPS mới)

### 1.1 Trên VPS — cài dependency

```bash
ssh hoang1432001@136.119.193.255

sudo apt-get update
sudo apt-get install -y python3-pip xvfb google-chrome-stable tmux

cd ~
git clone https://github.com/tramiune/ai_web2.git
cd ai_web2
pip3 install -r requirements.txt
python3 -m playwright install-deps
```

### 1.2 Copy `serviceAccountKey.json` từ Mac

```bash
# Trên Mac
scp ~/Documents/Tramiune/ai_web2/serviceAccountKey.json hoang1432001@136.119.193.255:~/ai_web2/
```

### 1.3 Copy Chrome profile từ Mac (Google Auth)

**Quan trọng:** Aidancing dùng **Google login**. Copy profile từ Mac giúp có session ban đầu, nhưng sau reboot hoặc hết hạn session vẫn có thể cần **login lại trên VPS** (mục 4).

**Trên Mac** — thoát hết Chrome (Cmd+Q):

```bash
rm -rf ~/.chrome-aidancing-wallpaper
mkdir -p ~/.chrome-aidancing-wallpaper
cp "$HOME/Library/Application Support/Google/Chrome/Local State" ~/.chrome-aidancing-wallpaper/
cp -R "$HOME/Library/Application Support/Google/Chrome/Profile 14" ~/.chrome-aidancing-wallpaper/

tar czf ~/chrome-wallpaper.tar.gz -C ~/.chrome-aidancing-wallpaper .
scp ~/chrome-wallpaper.tar.gz hoang1432001@136.119.193.255:~/
```

**Trên VPS:**

```bash
rm -rf ~/.chrome-aidancing-wallpaper
mkdir -p ~/.chrome-aidancing-wallpaper
tar xzf ~/chrome-wallpaper.tar.gz -C ~/.chrome-aidancing-wallpaper

# Bỏ qua cảnh báo tar "LIBARCHIVE.xattr..." — vô hại (file nén từ Mac)

rm -f ~/.chrome-aidancing-wallpaper/SingletonLock \
      ~/.chrome-aidancing-wallpaper/SingletonCookie \
      ~/.chrome-aidancing-wallpaper/SingletonSocket
```

---

## 2. Quy trình hàng ngày / sau reboot VPS

SSH vào VPS:

```bash
ssh hoang1432001@136.119.193.255
```

### Bước 1 — Bật Chrome (xvfb + CDP)

```bash
tmux kill-session -t chrome 2>/dev/null
tmux new-session -d -s chrome "xvfb-run -a --server-args='-screen 0 1280x800x24' google-chrome --remote-debugging-port=9223 --remote-allow-origins='*' --user-data-dir=\$HOME/.chrome-aidancing-wallpaper --profile-directory='Profile 14' --no-first-run --no-default-browser-check --disable-gpu"

# Sau reboot Chrome cần 15–20 giây mới lên port
sleep 15
curl -s http://127.0.0.1:9223/json/version | head -2
```

Phải thấy JSON `"Browser": "Chrome/..."`. Nếu rỗng → đợi thêm hoặc xem log:

```bash
tmux capture-pane -t chrome -p | tail -20
```

### Bước 2 — Kiểm tra Aidancing đã login

```bash
curl -s http://127.0.0.1:9223/json/list | grep dashboard
```

- **Có** `aidancing.net/dashboard` → OK, sang bước 3
- **Không có** (chỉ `/` hoặc `/login`) → làm **mục 4** (login Google)

### Bước 3 — Bật bot

```bash
tmux kill-session -t bot 2>/dev/null
tmux new-session -d -s bot "cd ~/ai_web2 && export BOT_CDP_URL=http://127.0.0.1:9223 && python3 bot.py --name nhaycloud_vps_bot --mode api"

sleep 5
tmux capture-pane -t bot -p | tail -15
```

Phải thấy:

```
✅ Chrome CDP sẵn sàng: http://127.0.0.1:9223
🟢 [nhaycloud_vps_bot] Đang trực...
```

### Bước 4 — Bật bot trong Admin

Nhay Cloud → **Admin → Bots** → bật `nhaycloud_vps_bot`.

---

## 3. Một lệnh gộp (sau reboot)

Paste trên VPS:

```bash
tmux kill-session -t chrome 2>/dev/null; tmux kill-session -t bot 2>/dev/null
tmux new-session -d -s chrome "xvfb-run -a --server-args='-screen 0 1280x800x24' google-chrome --remote-debugging-port=9223 --remote-allow-origins='*' --user-data-dir=\$HOME/.chrome-aidancing-wallpaper --profile-directory='Profile 14' --no-first-run --no-default-browser-check --disable-gpu"
sleep 15
tmux new-session -d -s bot "cd ~/ai_web2 && export BOT_CDP_URL=http://127.0.0.1:9223 && python3 bot.py --name nhaycloud_vps_bot --mode api"
tmux ls
curl -s http://127.0.0.1:9223/json/version | head -2
curl -s http://127.0.0.1:9223/json/list | grep dashboard
```

---

## 4. Login Aidancing trên VPS (Google Auth)

Copy profile Mac **không luôn đủ** trên Linux. Khi hết session, login trực tiếp trên Chrome VPS:

### Terminal Mac (giữ mở)

```bash
ssh -L 9223:127.0.0.1:9223 hoang1432001@136.119.193.255
```

### Mac Chrome

1. Mở `chrome://inspect/#devices`
2. **Configure...** → thêm `localhost:9223` → Done
3. **Remote Target** → tab aidancing → **inspect**
4. Vào `https://aidancing.net/dashboard`
5. **Đăng nhập Google** (Lan Trần)
6. Xác nhận thấy Dashboard (danh sách job)

### Kiểm tra trên VPS

```bash
curl -s http://127.0.0.1:9223/json/list | grep dashboard
```

Bot tự thử lại đơn pending sau ~5 phút (không cần restart nếu bot đang chạy).

---

## 5. Xem log & quản lý tmux

```bash
tmux ls                          # liệt kê session
tmux capture-pane -t bot -p | tail -20    # log bot (không cần attach)
tmux capture-pane -t chrome -p | tail -20 # log Chrome
tmux attach -t bot               # vào xem trực tiếp — thoát: Ctrl+B rồi D
```

**Không** Ctrl+C trong tmux bot/chrome trừ khi muốn tắt hẳn.

---

## 6. Cập nhật code bot

**Mac:**

```bash
cd ~/Documents/Tramiune/ai_web2
git pull   # hoặc sửa code, commit, push
```

**VPS:**

```bash
cd ~/ai_web2 && git pull
tmux send-keys -t bot C-c Enter
sleep 2
tmux send-keys -t bot 'cd ~/ai_web2 && export BOT_CDP_URL=http://127.0.0.1:9223 && python3 bot.py --name nhaycloud_vps_bot --mode api' Enter
```

Hoặc copy nhanh 1 file:

```bash
scp bot.py hoang1432001@136.119.193.255:~/ai_web2/
```

---

## 7. Xử lý lỗi thường gặp

| Triệu chứng | Nguyên nhân | Cách xử lý |
|-------------|-------------|------------|
| `tmux ls` → `No such file or directory` | Sau reboot, chưa bật gì | Làm **mục 2** hoặc **mục 3** |
| `curl .../json/version` rỗng | Chrome chưa kịp lên | `sleep 15` rồi thử lại; xem `tmux capture-pane -t chrome` |
| `Missing X server` / `Authorization required` | Thiếu xvfb | Dùng `xvfb-run` như mục 2, **không** chạy `google-chrome` trực tiếp với `DISPLAY=:99` |
| `HTTP 401: Vui lòng đăng nhập lại` | Session Aidancing hết hạn | **Mục 4** — login Google qua inspect |
| `grep aidancing` chỉ thấy `/` không có `dashboard` | Chưa login | **Mục 4** |
| `grep aidancing` rỗng dù Chrome chạy | JSON có dấu cách sau `:` | Dùng `grep dashboard` hoặc `grep aidancing` (không grep `"url":"`) |
| `tar: Ignoring unknown extended header...` | File tar từ Mac | **Bỏ qua** — giải nén vẫn OK |
| Bot nạp lỗi hiện trên web khách | Code cũ | Pull code mới — lỗi Aidancing chỉ gửi **Telegram** admin |
| `FutureWarning Python 3.10` | Cảnh báo thư viện Google | **Bỏ qua** — bot vẫn chạy |
| Google báo "trình duyệt không an toàn" | Login trên Linux headless | Thử lại inspect; hoặc copy profile Mac mới rồi login trên VPS |

### Log bot thành công

```
🆔 [API] Job mới: 657512
✅ Đơn ... → processing
...
🎉 Job ... HOÀN TẤT
✅ ĐÃ TRẢ HÀNG
```

---

## 8. Chạy local trên Mac (tùy chọn)

```bash
cd ~/Documents/Tramiune/ai_web2
pip install -r requirements.txt
playwright install chromium

# Terminal 1 — Chrome web2 (port 9223)
/Applications/Google\ Chrome.app/Contents/MacOS/Google\ Chrome \
  --remote-debugging-port=9223 \
  --remote-allow-origins='*' \
  --user-data-dir="$HOME/.chrome-aidancing-wallpaper" \
  --profile-directory="Profile 14"

# Terminal 2 — bot
export BOT_CDP_URL=http://127.0.0.1:9223
python3 bot.py --name nhaycloud_local --mode api
```

---

## 9. File nhạy cảm (không commit Git)

- `serviceAccountKey.json`
- `~/.chrome-aidancing-wallpaper/` (profile Chrome)
- `bot_chrome_profile_web2/`

Đã có trong `.gitignore`.

---

## 10. Reboot VPS

```bash
sudo reboot
```

Sau reboot: làm lại **mục 2** hoặc **mục 3**. Profile Chrome **không mất**; session Aidancing có thể còn hoặc cần login lại (mục 4).

---

## 11. Chạy song song web1 (MotionAI)

Cùng VPS, bot web1 dùng port **9222** + profile **Profile 4** — xem hướng dẫn đầy đủ trong repo `ai_web/README.md`.

Tóm tắt tmux web1:

```bash
tmux new-session -d -s chrome-motion "xvfb-run -a ... --remote-debugging-port=9222 ... --user-data-dir=\$HOME/.chrome-aidancing-motionai --profile-directory='Profile 4' ..."
tmux new-session -d -s bot-motion "cd ~/ai_web && export BOT_CDP_URL=http://127.0.0.1:9222 && python3 bot.py --name motionai_vps_bot --mode api"
```
