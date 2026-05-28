# MotionAI Bot Setup Guide t

Dự án này bao gồm một bot tự động nạp và kiểm tra đơn hàng trên aidancing.net.

## 1. Cài đặt môi trường (Win/Mac)

Yêu cầu: Đã cài đặt **Python 3.10+**.

### Cài đặt thư viện:
```bash
pip install -r requirements.txt
```

### Cài đặt trình duyệt cho Playwright:
```bash
playwright install chromium
```

## 2. Cấu hình quan trọng (Trước khi chạy)

Vì các file bảo mật không được đưa lên Git, bạn cần chuẩn bị:

1.  **Firebase Key:** Copy file `serviceAccountKey.json` vào thư mục gốc.
2.  **Đăng nhập lần đầu:** 
    *   Chạy bot lần đầu: `python bot.py`. 
    *   Cửa sổ Chrome sẽ hiện lên. 
    *   Bạn cần **đăng nhập thủ công** vào `aidancing.net` trên cửa sổ đó.
    *   Sau khi đăng nhập xong, bot sẽ lưu lại phiên đăng nhập vào thư mục `bot_chrome_profile` (thư mục này sẽ được tạo tự động) và các lần sau không cần đăng nhập lại.

## 3. Chạy Bot
```bash
python bot.py
```

## Lưu ý cho Mac:
Nếu gặp lỗi quyền truy cập khi chạy Playwright, hãy thử:
```bash
python3 bot.py
```
