#!/usr/bin/env bash
# Chạy nhay bot local để test Xây kênh tự động (Firestore production).
# Terminal khác: cd ai_web2 && npm run static  → http://localhost:8080
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

BOT_NAME="${BOT_NAME:-nhaycloud_local_bot}"
BOT_MODE="${BOT_MODE:-http}"

missing=()
for f in serviceAccountKey.json .env; do
  [[ -f "$ROOT/$f" ]] || missing+=("$f")
done
if ((${#missing[@]})); then
  echo "❌ Thiếu: ${missing[*]}" >&2
  exit 1
fi

# shellcheck disable=SC1091
set -a
source "$ROOT/.env"
set +a

if [[ -z "${XIAOYANG_ACCOUNTS:-}" && ( -z "${XIAOYANG_EMAIL:-}" || -z "${XIAOYANG_PASSWORD:-}" ) ]]; then
  echo "❌ .env thiếu XIAOYANG_ACCOUNTS hoặc XIAOYANG_EMAIL/PASSWORD (batch cần nick web XiaoYang)" >&2
  exit 1
fi

export BATCH_RUN_NOW_USE_TEST="${BATCH_RUN_NOW_USE_TEST:-1}"
export PYTHONUNBUFFERED=1

echo "==> Nhay local bot"
echo "    BOT_NAME=$BOT_NAME  mode=$BOT_MODE"
echo "    BATCH_RUN_NOW_USE_TEST=$BATCH_RUN_NOW_USE_TEST (Tạo video ngay → 1 video thử)"
echo "    FE: mở terminal khác → npm run static → http://localhost:8080"
echo "    ⚠️  Tắt bot VPS nhaycloud_vps_bot nếu không muốn 2 bot cùng nạp đơn."
echo ""

pkill -f "bot.py --name ${BOT_NAME}" 2>/dev/null || true
sleep 1
rm -f "$ROOT/.run/bot-${BOT_NAME//[^a-zA-Z0-9_-]/_}.lock" 2>/dev/null || true

exec python3 "$ROOT/bot.py" --name "$BOT_NAME" --mode "$BOT_MODE"
