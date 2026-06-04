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
XIAOYANG_MODAL_KEY=motion_v30
XIAOYANG_OPTION_KEY=default
BOT_MIN_RENDER_SEC=300
```

`motion_v30` = Motion Control v3.0 (Kling 3.0). `motion_v26` = v2.6 (~72 credits vs ~206 credits/task).

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
