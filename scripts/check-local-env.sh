#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

ok=0
fail=0
check() {
  if eval "$2"; then
    echo "✅ $1"
    ok=$((ok + 1))
  else
    echo "❌ $1"
    fail=$((fail + 1))
  fi
}

check "serviceAccountKey.json" "[[ -f serviceAccountKey.json ]]"
check ".env" "[[ -f .env ]]"
if [[ -f .env ]]; then
  # shellcheck disable=SC1091
  set -a; source .env; set +a
  check "XiaoYang nick" "[[ -n \"${XIAOYANG_ACCOUNTS:-}\" ]] || [[ -n \"${XIAOYANG_EMAIL:-}\" && -n \"${XIAOYANG_PASSWORD:-}\" ]]"
  check "Firebase init" "python3 -c 'import firebase_admin; from firebase_admin import credentials; cred=credentials.Certificate(\"serviceAccountKey.json\"); firebase_admin.initialize_app(cred); print(\"ok\")' 2>/dev/null | grep -q ok"
  check "XiaoYang login" "python3 -c \"
from project_env import load_project_env
load_project_env()
from batch_channel import get_batch_xy_client
get_batch_xy_client()
print('ok')
\" 2>/dev/null | tail -1 | grep -q ok"
fi

echo ""
echo "OK: $ok  FAIL: $fail"
exit "$([[ $fail -eq 0 ]] && echo 0 || echo 1)"
