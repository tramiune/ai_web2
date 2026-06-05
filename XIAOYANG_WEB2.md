# XiaoYang trên ai_web2 — Web session (không API key)

## Admin

**Admin → tab Bots → Engine render**

- `Aidancing` / `XiaoYang` — ghi Firestore `bots/{name}.activeRenderProvider`
- Đơn **processing** giữ `renderProvider` đã gắn lúc nạp
- Đơn **pending** mới dùng engine đang chọn

## Bot VPS `.env`

```env
BOT_MODE=api
XIAOYANG_EMAIL=motionaistudio@gmail.com
XIAOYANG_PASSWORD=...
XIAOYANG_ENHANCE_4K=1
XIAOYANG_OPTION_KEY=default
XIAOYANG_MOTION_ORIENTATION=video
BOT_MIN_RENDER_SEC=300
```

Cookie session lưu tại `xiaoyang_session.json` (tự login lại khi hết hạn).

**Modal theo gói web (tự động):**

| Web | `modelId` | XiaoYang |
|-----|-----------|----------|
| Model thường (Fast) | `124` / `125` | `motion_v26` + HD 2K (~75 CR) |
| Model Turbo 2K | `117` | `motion_v30` + HD 2K |

## Luồng XiaoYang Web

1. `pending` → tải ảnh/video từ đơn → upload `/api/upload` → `POST /api/tasks`
2. `processing` + `xiaoyangTaskId` — poll sau `BOT_MIN_RENDER_SEC`
3. Poll `QUEUED` / `PROCESSING` → `SUCCESS` → tải `/api/tasks/{id}/file` → R2 → `completed`
4. `FAIL` → hoàn coin + Telegram

Không cần Chrome, không cần `XIAOYANG_API_KEY`, không cần direct worker mirror.

## Chạy bot

```bash
python bot.py --name your-bot --mode api
```
