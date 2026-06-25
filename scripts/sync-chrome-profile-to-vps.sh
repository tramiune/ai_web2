#!/usr/bin/env bash
# Mac: copy profile Chrome (Google Auth) → VPS cho bot web2.
#
# Cách dùng (401 / hết session):
#   1. Thoát hết Chrome trên Mac (Cmd+Q)
#   2. ./scripts/sync-chrome-profile-to-vps.sh
#
# Chỉ đẩy profile bot sẵn có (không copy từ Chrome thật):
#   ./scripts/sync-chrome-profile-to-vps.sh --no-refresh
#
# Cron mỗi Chủ nhật 3h (chỉ chạy nếu Chrome đã tắt):
#   0 3 * * 0 /Users/qtee/Documents/Tramiune/ai_web2/scripts/sync-chrome-profile-to-vps.sh >>/tmp/sync-chrome-vps.log 2>&1

set -euo pipefail

VPS_USER="${VPS_USER:-root}"
VPS_HOST="${VPS_HOST:-165.101.46.68}"
VPS="${VPS_USER}@${VPS_HOST}"

REAL_CHROME="${REAL_CHROME:-$HOME/Library/Application Support/Google/Chrome}"
REAL_PROFILE="${REAL_PROFILE:-Profile 14}"
BOT_PROFILE="${BOT_PROFILE:-$HOME/.chrome-aidancing-wallpaper}"

ARCHIVE="${ARCHIVE:-$HOME/chrome-profile-sync.tar.gz}"
REFRESH=1

usage() {
  sed -n '2,12p' "$0" | sed 's/^# \?//'
  exit "${1:-0}"
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    -h|--help) usage 0 ;;
    --no-refresh) REFRESH=0; shift ;;
    *) echo "Unknown option: $1"; usage 1 ;;
  esac
done

chrome_running() {
  pgrep -x "Google Chrome" >/dev/null 2>&1 || pgrep -f "Google Chrome.app" >/dev/null 2>&1
}

if [[ "$REFRESH" -eq 1 ]]; then
  if chrome_running || [[ -f "$REAL_CHROME/SingletonLock" ]]; then
    echo "❌ Cần thoát hết Chrome (Cmd+Q) trước khi --refresh (copy Profile 14 mới)."
    exit 1
  fi
else
  if [[ -f "$BOT_PROFILE/SingletonLock" ]]; then
    echo "❌ Bot profile đang bị Chrome giữ (SingletonLock)."
    echo "   Đóng cửa sổ Chrome port 9223 / bot profile rồi chạy lại."
    exit 1
  fi
fi

if [[ "$REFRESH" -eq 1 ]]; then
  if [[ ! -d "$REAL_CHROME/$REAL_PROFILE" ]]; then
    echo "❌ Không thấy $REAL_CHROME/$REAL_PROFILE"
    exit 1
  fi
  echo "📋 Copy Profile 14 (Lan Trần) → $BOT_PROFILE ..."
  rm -rf "$BOT_PROFILE"
  mkdir -p "$BOT_PROFILE"
  cp "$REAL_CHROME/Local State" "$BOT_PROFILE/"
  cp -R "$REAL_CHROME/$REAL_PROFILE" "$BOT_PROFILE/"
  echo "✅ Đã refresh profile từ Chrome Mac"
fi

if [[ ! -d "$BOT_PROFILE/$REAL_PROFILE" ]]; then
  echo "❌ Thiếu $BOT_PROFILE/$REAL_PROFILE — chạy không có --no-refresh"
  exit 1
fi

echo "📦 Nén profile (bỏ cache nặng)..."
rm -f "$ARCHIVE"
tar czf "$ARCHIVE" -C "$BOT_PROFILE" \
  --exclude='*/Cache' \
  --exclude='*/Code Cache' \
  --exclude='*/GPUCache' \
  --exclude='*/GrShaderCache' \
  --exclude='*/ShaderCache' \
  --exclude='*/Service Worker/CacheStorage' \
  --exclude='*/OptimizationGuidePredictionModels' \
  .

SIZE=$(du -h "$ARCHIVE" | cut -f1)
echo "   Kích thước: $SIZE"

echo "📤 Upload → $VPS ..."
scp "$ARCHIVE" "${VPS}:~/chrome-profile-sync.tar.gz"
scp "$(dirname "$0")/vps-install-chrome-profile.sh" "${VPS}:~/vps-install-chrome-profile.sh"

echo "🚀 Cài profile trên VPS..."
ssh "$VPS" "chmod +x ~/vps-install-chrome-profile.sh && bash ~/vps-install-chrome-profile.sh ~/chrome-profile-sync.tar.gz"

echo ""
echo "✅ Sync xong. Bot web2 sẽ dùng session Google mới từ Mac."
echo "   Nếu vẫn 401: mở aidancing.net trên Mac Profile 14, đăng nhập Google, rồi chạy lại script."
