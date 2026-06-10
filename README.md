# Facebook Video Pipeline

Tool local để quản lý quy trình video Facebook:

1. Import danh sách video từ Excel/CSV vào SQLite.
2. Tải video pending bằng `yt-dlp`.
3. Tạo lịch đăng.
4. Upload/lên lịch video lên Facebook Fanpage bằng Meta Graph API.

Project vẫn giữ 2 tool cũ trong:

- `files/`: tool tải video cũ.
- `Auto_upload_video_fb/`: tool đăng/lên lịch cũ.

Pipeline mới nằm ở root project và dùng chung database:

```text
data/pipeline.sqlite3
```

## Yêu cầu

- Python 3.10+
- Windows/macOS/Linux
- Browser đã đăng nhập Facebook nếu cần tải video private bằng cookie
- Page ID và Page Access Token nếu muốn publish lên Fanpage

## Cài đặt

Tạo môi trường ảo:

```powershell
python -m venv venv
.\venv\Scripts\activate
```

Cài thư viện:

```powershell
pip install -r requirements.txt
```

Nếu muốn đóng gói thành `.exe`:

```powershell
pip install pyinstaller pywebview
```

## Chạy Web Pipeline

```powershell
python -m uvicorn pipeline_web.main:app --host 127.0.0.1 --port 8010
```

Mở:

```text
http://127.0.0.1:8010
```

Trên dashboard có các phần:

- `Import Excel/CSV`
- `Download Pending`
- `Generate Schedule`
- `Publish Pending`
- `Recent Jobs`
- Bảng video có filter và checkbox xóa dòng

## Chạy dạng Desktop App

```powershell
python desktop_app.py
```

Nếu có `pywebview`, tool sẽ mở trong cửa sổ app riêng. Nếu chưa có, nó sẽ mở bằng browser.

Build `.exe`:

```powershell
.\scripts\build_desktop_exe.ps1
```

File build ra ở:

```text
dist/VideoPipeline.exe
```

## Format File Import

### 1. CSV/Excel để tải video từ Facebook

Cột khuyến nghị:

```text
video_id,title,source_url,created_at
```

Ví dụ:

```csv
video_id,title,source_url,created_at
1,"Tiêu đề video","https://www.facebook.com/share/v/xxxx/","2026-06-01 10:30"
```

Cột quan trọng:

- `source_url`: link video Facebook.
- `title`: tiêu đề/caption.
- `created_at`: ngày tạo.
- `video_id`: chỉ là số thứ tự, không dùng làm khóa Facebook.

Lưu ý encoding:

- Nên dùng `.xlsx` nếu có tiếng Việt.
- Nếu dùng CSV, hãy lưu dạng `CSV UTF-8`.
- Nếu CSV đã có dấu `?` trong tiêu đề, app không thể tự khôi phục chính xác. Hãy sửa file nguồn hoặc thêm cột `title_rewrite`.

### 2. Excel/CSV để đăng video có sẵn

Cột hỗ trợ:

```text
filename,title,hashtag,scheduled_time
```

Ví dụ:

```text
video1.mp4 | Tiêu đề video | #viral | 2026-06-11 08:00:00
```

Video có thể nằm trong:

```text
Auto_upload_video_fb/videos/
Auto_upload_video_fb/downloads/
data/downloads/
```

## Quy trình sử dụng Web

1. Import file Excel/CSV.
2. Vào tab `Cần tải`, kiểm tra danh sách.
3. Chạy `Download Pending`.
   - Tick `Dry run` để xem thử.
   - Bỏ tick `Dry run` để tải thật.
4. Sau khi tải, kiểm tra cột `Local File`.
5. Chạy `Generate Schedule`.
6. Vào tab `Cần đăng`, xóa các dòng không muốn publish.
7. Chạy `Publish Pending`.
   - Tick `Dry run` để kiểm tra danh sách.
   - Bỏ tick `Dry run` để gửi thật lên Facebook.

## CLI Commands

Import:

```powershell
python main.py import-excel files\sample_videos.csv
```

Download pending:

```powershell
python main.py download-pending --limit 10 --browser firefox
```

Generate schedule:

```powershell
python main.py generate-schedule --start-time "2026-06-11 08:00:00" --interval-minutes 60 --limit 20
```

Publish pending:

```powershell
python main.py publish-pending --limit 5 --page-id YOUR_PAGE_ID --page-access-token YOUR_TOKEN
```

Hoặc dùng biến môi trường:

```powershell
$env:FB_PAGE_ID="YOUR_PAGE_ID"
$env:FB_PAGE_ACCESS_TOKEN="YOUR_TOKEN"
python main.py publish-pending --limit 5
```

## Database

SQLite database mặc định:

```text
data/pipeline.sqlite3
```

Init database:

```powershell
python scripts\init_pipeline_db.py
```

Xem dữ liệu:

```powershell
python scripts\inspect_pipeline_db.py --limit 20
```

Không nên commit database thật lên GitHub.

## Bảo mật

Không commit:

- Page Access Token
- SQLite database thật
- Video tải về
- File log/chứa dữ liệu riêng tư

Page Access Token chỉ nhập khi publish hoặc truyền qua biến môi trường.

## Ghi chú về Facebook API

Để publish video, token cần quyền phù hợp, thường gồm:

- `pages_manage_posts`
- `pages_read_engagement`
- `publish_video`

Nếu thiếu quyền hoặc token hết hạn, log Publish Progress sẽ hiển thị lỗi từ Facebook.
