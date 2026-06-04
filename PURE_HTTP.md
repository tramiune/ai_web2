# Bot Pure HTTP — ai_web2 (nhánh `feat/pure-http-aidancing`)

**NhayCloud / Wallpaper** — không cần Chrome khi `--mode http`.

## Cookie (cấu hình sau)

```bash
cd ~/ai_web2
cp .env.example .env
# AIDANCING_COOKIE=JSESSIONID=...  (account lantran / Profile 14)
```

## Chạy trên VPS (không cần tmux Chrome nữa)

```bash
cd ~/ai_web2
python3 bot.py --name nhaycloud_vps_bot --mode http
```

Admin → Bots → bật `nhaycloud_vps_bot`.

## So với cách cũ

| Cũ (`--mode api` + CDP 9223) | Mới (`--mode http`) |
|---|---|
| Chrome + xvfb + port 9223 | Không |
| `BOT_CDP_URL` | `AIDANCING_COOKIE` trong `.env` |
| Poll fetch in-page | `requests` GET `/api/proxy/jobs` |

`--mode browser` vẫn dùng Playwright (không đổi).

## Poll interval

Dùng biến có sẵn: `BOT_POLL_WAIT_RENDER_SEC`, `BOT_POLL_ACTIVE_SEC` (mặc định ~90–120s khi đang render).
