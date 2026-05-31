#!/usr/bin/env bash
# Cài profile Chrome nhận từ Mac lên VPS và khởi động lại Chrome CDP + bot web2.
# Chạy trên VPS: bash vps-install-chrome-profile.sh ~/chrome-profile-sync.tar.gz

set -euo pipefail

ARCHIVE="${1:-$HOME/chrome-profile-sync.tar.gz}"
PROFILE_DIR="${CHROME_PROFILE_DIR:-$HOME/.chrome-aidancing-wallpaper}"
PROFILE_DIRECTORY="${CHROME_PROFILE_DIRECTORY:-Profile 14}"
CDP_PORT="${CHROME_CDP_PORT:-9223}"
BOT_DIR="${BOT_DIR:-$HOME/ai_web2}"
BOT_NAME="${BOT_NAME:-nhaycloud_vps_bot}"
DISPLAY_NUM="${DISPLAY_NUM:-:99}"

if [[ ! -f "$ARCHIVE" ]]; then
  echo "❌ Không thấy file: $ARCHIVE"
  exit 1
fi

echo "⏹️  Dừng Chrome + bot (tmux)..."
tmux kill-session -t chrome 2>/dev/null || true
tmux kill-session -t bot 2>/dev/null || true
sleep 2

echo "📦 Giải nén profile → $PROFILE_DIR"
rm -rf "$PROFILE_DIR"
mkdir -p "$PROFILE_DIR"
tar xzf "$ARCHIVE" -C "$PROFILE_DIR"

echo "🔓 Xóa lock file (copy từ Mac)..."
rm -f "$PROFILE_DIR/SingletonLock" "$PROFILE_DIR/SingletonCookie" "$PROFILE_DIR/SingletonSocket"
rm -f "$PROFILE_DIR/$PROFILE_DIRECTORY/SingletonLock" \
      "$PROFILE_DIR/$PROFILE_DIRECTORY/SingletonCookie" \
      "$PROFILE_DIR/$PROFILE_DIRECTORY/SingletonSocket" 2>/dev/null || true

CHROME_CMD="export DISPLAY=${DISPLAY_NUM}; google-chrome \
  --remote-debugging-port=${CDP_PORT} \
  --remote-allow-origins=* \
  --user-data-dir=${PROFILE_DIR} \
  --profile-directory=\"${PROFILE_DIRECTORY}\" \
  --no-first-run \
  --no-default-browser-check \
  --disable-blink-features=AutomationControlled"

echo "🌐 Khởi động Chrome CDP (tmux: chrome)..."
if ! tmux has-session -t chrome 2>/dev/null; then
  tmux new-session -d -s chrome
fi
tmux send-keys -t chrome "pkill -f 'remote-debugging-port=${CDP_PORT}' 2>/dev/null; sleep 1; ${CHROME_CMD}" Enter

echo "⏳ Chờ CDP port ${CDP_PORT}..."
for i in $(seq 1 30); do
  if curl -sf "http://127.0.0.1:${CDP_PORT}/json/version" >/dev/null 2>&1; then
    echo "✅ Chrome CDP OK"
    break
  fi
  sleep 1
  if [[ "$i" -eq 30 ]]; then
    echo "⚠️  CDP chưa lên — kiểm tra: tmux attach -t chrome"
  fi
done

BOT_CMD="cd ${BOT_DIR} && export BOT_CDP_URL=http://127.0.0.1:${CDP_PORT} && python3 bot.py --name ${BOT_NAME} --mode api"
echo "🤖 Khởi động bot (tmux: bot)..."
if ! tmux has-session -t bot 2>/dev/null; then
  tmux new-session -d -s bot
fi
tmux send-keys -t bot "pkill -f 'bot.py --name ${BOT_NAME}' 2>/dev/null; sleep 1; ${BOT_CMD}" Enter

echo "✅ Xong. Kiểm tra: tmux attach -t bot"
