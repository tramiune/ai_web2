#!/bin/bash
# Chạy TRÊN VPS sau khi main đã push (UI tự deploy qua Cloudflare Pages).
set -euo pipefail

cd "${HOME}/ai_web2"
echo "==> git pull main"
git fetch origin
git checkout main
git pull origin main

ENV_FILE=".env"
touch "$ENV_FILE"
if [ -f "$ENV_FILE" ]; then
  sed -i 's/\r$//' "$ENV_FILE" 2>/dev/null || sed -i '' 's/\r$//' "$ENV_FILE" 2>/dev/null || true
  echo "  đã chuẩn hóa CRLF trong .env"
fi
ensure_env() {
  local key="$1"
  local val="$2"
  if grep -q "^${key}=" "$ENV_FILE" 2>/dev/null; then
    echo "  .env có ${key}"
  else
    echo "${key}=${val}" >> "$ENV_FILE"
    echo "  + thêm ${key}"
  fi
}

echo "==> kiểm tra .env XiaoYang"
ensure_env "XIAOYANG_DIRECT_WORKER_URL" "https://xiaoyang-direct-media.traderfinn0312.workers.dev"
if grep -q "^XIAOYANG_MODAL_KEY=" "$ENV_FILE" 2>/dev/null; then
  sed -i 's/^XIAOYANG_MODAL_KEY=.*/XIAOYANG_MODAL_KEY=motion_v26/' "$ENV_FILE"
  echo "  cập nhật XIAOYANG_MODAL_KEY=motion_v26 (fallback; turbo dùng motion_v30 theo modelId)"
else
  ensure_env "XIAOYANG_MODAL_KEY" "motion_v26"
fi
ensure_env "XIAOYANG_OPTION_KEY" "default"
ensure_env "XIAOYANG_MOTION_ORIENTATION" "video"
if ! grep -q "^XIAOYANG_API_KEY=" "$ENV_FILE" 2>/dev/null; then
  echo "  !! Thiếu XIAOYANG_API_KEY — thêm tay vào .env rồi chạy lại"
fi
if ! grep -q "^BOT_MIN_RENDER_SEC=" "$ENV_FILE" 2>/dev/null; then
  ensure_env "BOT_MIN_RENDER_SEC" "300"
fi

echo "==> restart bot tmux"
tmux kill-session -t bot-http 2>/dev/null || true
tmux new-session -d -s bot-http \
  "bash -lc 'set -a; [ -f ~/ai_web2/.env ] && . ~/ai_web2/.env; set +a; export BOT_POLL_ACTIVE_SEC=30; cd ~/ai_web2 && exec python3 bot.py --name nhaycloud_vps_bot --mode http'"

sleep 2
tmux ls
ps aux | grep nhaycloud_vps_bot | grep -v grep || true
echo "==> xong. Admin web: Bots -> chọn XiaoYang cho đơn mới"
