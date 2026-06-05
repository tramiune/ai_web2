# XiaoYang trên ai_web2 (nhánh `feat/pure-http-aidancing`)

## Admin

**Admin → tab Bots → Engine render**

- `Aidancing` / `XiaoYang` — ghi Firestore `settings/render.activeProvider`
- Đơn **processing** giữ `renderProvider` đã gắn lúc nạp
- Đơn **pending** mới dùng engine đang chọn

## Bot VPS `.env`

```env
BOT_MODE=api
XIAOYANG_API_KEY=xy_...
XIAOYANG_DIRECT_WORKER_URL=https://xiaoyang-direct-media.traderfinn0312.workers.dev
XIAOYANG_OPTION_KEY=default
BOT_MIN_RENDER_SEC=300
```

**Modal theo gói web (tự động, không cần đổi tay mỗi đơn):**

| Web | `modelId` | XiaoYang |
|-----|-----------|----------|
| Model thường (Fast) | `124` / `125` | `motion_v26` (~72 CR) |
| Model Turbo 2K | `117` | `motion_v30` (~206 CR) |

`XIAOYANG_MODAL_KEY` trong `.env` chỉ là fallback khi `modelId` lạ.

## Luồng XiaoYang

1. `pending` → mirror Workers → direct URL → `POST /api/v1/tasks`
2. `processing` + `xiaoyangTaskId` — **không poll** trước `BOT_MIN_RENDER_SEC` (giống Aidancing)
3. Poll `QUEUED` / `PENDING` / `PROCESSING` → `SUCCESS` → tải → R2 → `completed`
4. `try_delete_task` sau khi trả hàng (nếu API hỗ trợ DELETE)
5. `FAIL` → hoàn coin + Telegram

## Chạy bot

```bash
python bot.py --name your-bot --mode api
```
