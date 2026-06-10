# 📹 Facebook Video Tool

Tool đăng và hẹn lịch video hàng loạt lên Fanpage Facebook bằng Meta Graph API chính thức.

---

## ✅ Yêu cầu hệ thống

- Python 3.10+
- Page ID của Fanpage
- Page Access Token hợp lệ (có quyền `pages_manage_posts`, `pages_read_engagement`)

---

## 🚀 Cài đặt & Chạy

```bash
# 1. Tạo môi trường ảo
python -m venv venv

# 2. Kích hoạt môi trường ảo
# Windows:
venv\Scripts\activate
# macOS/Linux:
source venv/bin/activate

# 3. Cài thư viện
pip install -r requirements.txt
cd D:\Workspcae\MMO\DownVidFB\files
venv\Scripts\python.exe -m uvicorn main:app --host 127.0.0.1 --port 8001
# 4. Chạy server
uvicorn main:app --reload

# 5. Mở trình duyệt
# http://127.0.0.1:8000/post-videos
```

---

## 📂 Cấu trúc thư mục

```
facebook_video_tool/
├── main.py                 # FastAPI app chính
├── templates/
│   ├── index.html          # Base layout
│   ├── post_videos.html    # Trang đăng video
│   └── post_history.html   # Trang lịch sử
├── static/
│   └── style.css           # Stylesheet
├── videos/                 # 📁 Đặt video vào đây (mặc định)
├── downloads/              # 📁 Hoặc đây
├── history_post.json       # Lịch sử đăng (tự tạo)
├── sample_posts.csv        # File mẫu CSV
├── requirements.txt
└── README.md
```

---

## 📋 Chuẩn bị file Excel/CSV

### Format bắt buộc

| Cột | Bắt buộc | Mô tả |
|-----|----------|-------|
| `filename` | ✅ | Tên file video (ví dụ: `video1.mp4`) |
| `title` | ✅ | Caption chính của bài đăng |
| `hashtag` | ❌ | Hashtag kèm theo. **Nếu trống → tự sinh từ title** |
| `scheduled_time` | ❌ | Thời gian hẹn lịch. **Nếu trống → đăng ngay** |

### Ví dụ file CSV

```csv
filename,title,hashtag,scheduled_time
video1.mp4,"Cô gái nghèo bất ngờ đổi đời","#phimhay #viral","2026-06-10 08:00:00"
video2.mp4,"Tổng tài che giấu thân phận","","2026-06-10 10:00:00"
video3.mp4,"Một quyết định thay đổi số phận","#xuhuong",""
```

---

## 🎬 Đặt video vào thư mục

Đặt các file `.mp4` vào một trong hai thư mục:

```
videos/           ← Chọn "videos/" trong form (mặc định)
downloads/        ← Chọn "downloads/" trong form
```

**Tên file trong cột `filename` phải khớp chính xác với tên file thực tế.**

---

## 🔑 Lấy Page ID và Page Access Token

### Page ID
1. Vào trang Fanpage của bạn
2. Chọn **Giới thiệu** hoặc **Thông tin trang**
3. Cuộn xuống tìm **Page ID** (dãy số)

Hoặc vào: `https://www.facebook.com/YOUR_PAGE_NAME/about`

### Page Access Token
1. Vào [Meta for Developers](https://developers.facebook.com/)
2. Tạo App → Thêm sản phẩm **Facebook Login**
3. Dùng [Graph API Explorer](https://developers.facebook.com/tools/explorer/)
4. Chọn App → chọn Page → lấy **Page Access Token**
5. Đảm bảo token có các quyền:
   - `pages_manage_posts`
   - `pages_read_engagement`
   - `publish_video` *(nếu cần)*

> ⚠️ **Quan trọng:** Token ngắn hạn hết hạn sau 1-2 giờ.
> Dùng [Access Token Debugger](https://developers.facebook.com/tools/debug/accesstoken/) để extend hoặc tạo token dài hạn.

---

## ⏰ Hẹn lịch đăng (scheduled_time)

- **Format:** `YYYY-MM-DD HH:MM:SS`
- **Timezone:** `Asia/Ho_Chi_Minh` (GMT+7) — có thể đổi trong form
- **Nếu để trống:** đăng ngay lập tức
- **Giới hạn:** thời gian hẹn phải **cách ít nhất 10 phút** trong tương lai
- **Giới hạn Facebook:** tối đa 30 ngày kể từ ngày hôm nay

Ví dụ hợp lệ:
```
2026-06-10 08:30:00
2026-07-01 20:00:00
```

---

## 📊 Trạng thái video

| Trạng thái | Ý nghĩa |
|-----------|---------|
| `pending` | Chờ xử lý |
| `uploading` | Đang upload |
| `published` | Đã đăng thành công |
| `scheduled` | Đã hẹn lịch thành công |
| `failed` | Lỗi (xem cột error_message) |

---

## ⚠️ Giới hạn và lưu ý

- Chỉ đăng được lên **Fanpage bạn có quyền quản trị**
- Token phải có đủ quyền (`pages_manage_posts`, `publish_video`)
- **Không đăng được** nếu token thiếu quyền hoặc hết hạn
- **Không đăng nội dung vi phạm bản quyền** Facebook sẽ tự động gỡ
- Video phải đúng định dạng Facebook hỗ trợ: `.mp4`, `.mov`, `.avi`,...
- Kích thước tối đa: 10GB | Thời lượng tối đa: 4 giờ
- **Token KHÔNG được lưu** vào `history_post.json` hay bất kỳ file nào

---

## 🔒 Bảo mật

- Page Access Token chỉ tồn tại trong bộ nhớ trong quá trình xử lý
- Không in token ra console hoặc log
- Không lưu token vào `history_post.json`
- Không lưu token vào bất kỳ file nào
