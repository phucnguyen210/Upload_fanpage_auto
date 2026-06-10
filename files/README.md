# FB Video Downloader — Local Tool

Web app chạy local để **lọc và tải video** từ file CSV có `direct_url` hợp lệ.

---

## ⚠️ Compliance

- ❌ Không auto login Facebook
- ❌ Không dùng Facebook cookie
- ❌ Không scrape profile/page
- ❌ Không vượt captcha hoặc checkpoint
- ✅ Chỉ tải khi CSV có `direct_url` bắt đầu bằng `http`

---
cd D:\Workspcae\MMO\DownVidFB\Auto_upload_video_fb
venv\Scripts\python.exe -m uvicorn main:app --host 127.0.0.1 --port 8001
## 📁 Cấu trúc project

```
facebook_video_downloader/
├── main.py                  # FastAPI app chính
├── templates/
│   ├── index.html           # Trang tải video
│   └── history.html         # Trang lịch sử
├── static/
│   └── style.css            # Giao diện dark
├── downloads/               # Video được tải về đây (downloads/YYYY-MM-DD/)
├── sample_videos.csv        # File CSV mẫu để test
├── history.json             # Lịch sử tải (tự tạo)
├── requirements.txt
└── README.md
```

---

## 🚀 Hướng dẫn chạy

### Bước 1 — Tạo môi trường ảo

```bash
python -m venv venv
```

### Bước 2 — Kích hoạt môi trường ảo

**Windows:**
```bash
venv\Scripts\activate
```

**macOS / Linux:**
```bash
source venv/bin/activate
```

### Bước 3 — Cài thư viện

```bash
pip install -r requirements.txt
```

### Bước 4 — Chạy server

```bash
uvicorn main:app --reload
```

### Bước 5 — Mở trình duyệt

```
http://127.0.0.1:8000
```

---

## 🧪 Cách test với sample_videos.csv

1. Mở `http://127.0.0.1:8000`
2. Điền Link Profile: `https://facebook.com/yourpage` (tuỳ chọn)
3. Ngày bắt đầu: `2026-06-01`
4. Ngày kết thúc: `2026-06-05`
5. Upload file `sample_videos.csv`
6. Nhấn **Lọc & Tải Video**

**Kết quả mong đợi:**
- Video 1, 3, 6 → Tải thành công (có `direct_url` hợp lệ)
- Video 2 → Thất bại (thiếu `direct_url`)
- Video 4 → Thất bại (link Facebook không phải direct link)
- Video 5 → Không xuất hiện (ngoài khoảng ngày lọc)

---

## 📋 Format CSV

```csv
video_id,title,source_url,direct_url,created_at
1,"Tên video","https://facebook.com/videos/123","https://example.com/video.mp4","2026-06-01 10:30:00"
```

| Cột | Mô tả | Bắt buộc |
|-----|-------|----------|
| `video_id` | ID video | Không |
| `title` | Tiêu đề | Có |
| `source_url` | Link Facebook gốc | Không |
| `direct_url` | Link tải thẳng (http/https) | **Có** |
| `created_at` | Ngày tạo (YYYY-MM-DD) | Có |

---

## 📂 Video được lưu ở đâu?

```
downloads/
└── 2026-06-05/
    ├── 1_Video_demo_1.mp4
    └── 3_Video_demo_3.mp4
```

---

## 🗂 Xem lịch sử

Truy cập `http://127.0.0.1:8000/history`

Hoặc mở file `history.json` để xem raw data.
