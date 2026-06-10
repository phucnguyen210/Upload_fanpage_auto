"""
Facebook Video Downloader — yt-dlp Edition
Dùng yt-dlp + cookie trình duyệt để tải video private Facebook.
Không cần cột direct_url trong CSV.
"""

import os
import json
import csv
import re
import io
import subprocess
import sys
from datetime import datetime, date
from pathlib import Path
from typing import List

from fastapi import FastAPI, Request, UploadFile, File, Form
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

# ──────────────────────────────────────────────
# Khởi tạo app
# ──────────────────────────────────────────────
app = FastAPI(title="FB Video Downloader")

BASE_DIR = Path(__file__).parent
DOWNLOADS_DIR = BASE_DIR / "downloads"
HISTORY_FILE = BASE_DIR / "history.json"

DOWNLOADS_DIR.mkdir(exist_ok=True)
(BASE_DIR / "static").mkdir(exist_ok=True)
(BASE_DIR / "templates").mkdir(exist_ok=True)

app.mount("/static", StaticFiles(directory=BASE_DIR / "static"), name="static")
templates = Jinja2Templates(directory=BASE_DIR / "templates")

# Trình duyệt yt-dlp sẽ lấy cookie từ (chrome/firefox/edge/brave)
COOKIE_BROWSER = "chrome"


# ──────────────────────────────────────────────
# Helper functions
# ──────────────────────────────────────────────

def safe_filename(name: str) -> str:
    name = re.sub(r'[\\/:*?"<>|]', "_", name)
    name = name.strip(". ")
    return name[:80] if name else "video"


def load_history() -> list:
    try:
        if HISTORY_FILE.exists():
            with open(HISTORY_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
                return data if isinstance(data, list) else []
    except (json.JSONDecodeError, IOError):
        pass
    return []


def save_history(records: list) -> None:
    with open(HISTORY_FILE, "w", encoding="utf-8") as f:
        json.dump(records, f, ensure_ascii=False, indent=2, default=str)


def parse_csv(content: bytes) -> List[dict]:
    encodings = ["utf-8-sig", "utf-8", "cp1252", "latin-1"]
    text = None
    for enc in encodings:
        try:
            text = content.decode(enc)
            break
        except (UnicodeDecodeError, LookupError):
            continue
    if text is None:
        raise ValueError("Không thể đọc file CSV.")
    reader = csv.DictReader(io.StringIO(text))
    return [{k.strip(): v.strip() for k, v in row.items() if k} for row in reader]


def filter_by_date(rows: List[dict], start: date, end: date) -> List[dict]:
    DATE_FORMATS = [
        "%Y-%m-%d %H:%M:%S", "%Y-%m-%d",
        "%m/%d/%Y %H:%M",    "%m/%d/%Y",
        "%d/%m/%Y %H:%M:%S", "%d/%m/%Y",
    ]
    result = []
    for row in rows:
        raw = row.get("created_at", "").strip()
        for fmt in DATE_FORMATS:
            try:
                d = datetime.strptime(raw, fmt).date()
                if start <= d <= end:
                    result.append(row)
                break
            except ValueError:
                continue
    return result


def download_with_ytdlp(source_url: str, save_dir: Path, title_hint: str, browser: str) -> tuple[bool, str, str]:
    """
    Dùng yt-dlp tải video từ source_url.
    Dùng cookie từ trình duyệt để xử lý video private.
    Trả về: (success, local_path, error_message)
    """
    save_dir.mkdir(parents=True, exist_ok=True)

    fname = safe_filename(title_hint) if title_hint else "%(title)s"
    output_template = str(save_dir / f"{fname}.%(ext)s")

    cmd = [
        sys.executable, "-m", "yt_dlp",
        "--cookies-from-browser", browser,
        "--format", "bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best",
        "--merge-output-format", "mp4",
        "--output", output_template,
        "--no-playlist",
        "--quiet",
        "--no-warnings",
        source_url,
    ]

    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=300,  # 5 phút timeout
        )

        if result.returncode == 0:
            # Tìm file vừa tải trong save_dir
            mp4_files = sorted(save_dir.glob(f"{safe_filename(title_hint)}*.mp4"), key=lambda f: f.stat().st_mtime, reverse=True)
            if not mp4_files:
                # Fallback: lấy file mp4 mới nhất trong thư mục
                mp4_files = sorted(save_dir.glob("*.mp4"), key=lambda f: f.stat().st_mtime, reverse=True)

            if mp4_files:
                local_path = str(mp4_files[0].relative_to(BASE_DIR))
                return True, local_path, ""
            else:
                return False, "", "yt-dlp chạy thành công nhưng không tìm thấy file output."
        else:
            err = result.stderr.strip() or result.stdout.strip()
            # Rút gọn thông báo lỗi phổ biến
            if "Cookies" in err or "login" in err.lower():
                err = f"Cần đăng nhập. Hãy đảm bảo bạn đã đăng nhập Facebook trên trình duyệt {browser.title()} và thử lại."
            elif "Private" in err or "private" in err:
                err = "Video private — không thể truy cập. Kiểm tra cookie trình duyệt."
            elif "Unsupported URL" in err:
                err = "URL không được hỗ trợ. Kiểm tra lại source_url."
            elif err == "":
                err = f"yt-dlp thất bại (exit code {result.returncode})."
            return False, "", err[:300]

    except subprocess.TimeoutExpired:
        return False, "", "Timeout sau 5 phút. Video quá lớn hoặc kết nối chậm."
    except FileNotFoundError:
        return False, "", "Không tìm thấy yt-dlp. Chạy: pip install yt-dlp"
    except Exception as e:
        return False, "", f"Lỗi không xác định: {str(e)}"


# ──────────────────────────────────────────────
# Routes
# ──────────────────────────────────────────────

@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    return templates.TemplateResponse("index.html", {
        "request": request,
        "today": date.today().isoformat(),
        "cookie_browser": COOKIE_BROWSER,
        "browsers": ["chrome", "firefox", "edge", "brave", "safari", "chromium"],
    })


@app.post("/import-and-download", response_class=HTMLResponse)
async def import_and_download(
    request: Request,
    source_profile_url: str = Form(""),
    start_date: str = Form(...),
    end_date: str = Form(...),
    browser: str = Form("chrome"),
    csv_file: UploadFile = File(...),
):
    errors = []
    results = []

    # 1. Validate ngày
    try:
        d_start = datetime.strptime(start_date, "%Y-%m-%d").date()
        d_end   = datetime.strptime(end_date,   "%Y-%m-%d").date()
    except ValueError:
        errors.append("Ngày không đúng định dạng YYYY-MM-DD.")
        return templates.TemplateResponse("index.html", {
            "request": request, "errors": errors,
            "today": date.today().isoformat(), "cookie_browser": browser,
            "browsers": ["chrome", "firefox", "edge", "brave", "safari", "chromium"],
        })

    if d_start > d_end:
        errors.append("Ngày bắt đầu phải ≤ ngày kết thúc.")
        return templates.TemplateResponse("index.html", {
            "request": request, "errors": errors,
            "today": date.today().isoformat(), "cookie_browser": browser,
            "browsers": ["chrome", "firefox", "edge", "brave", "safari", "chromium"],
        })

    # 2. Validate + đọc CSV
    if not csv_file.filename.endswith(".csv"):
        errors.append("Chỉ chấp nhận file .csv.")
        return templates.TemplateResponse("index.html", {
            "request": request, "errors": errors,
            "today": date.today().isoformat(), "cookie_browser": browser,
            "browsers": ["chrome", "firefox", "edge", "brave", "safari", "chromium"],
        })

    try:
        content = await csv_file.read()
        rows = parse_csv(content)
    except Exception as e:
        errors.append(f"Không thể đọc CSV: {str(e)}")
        return templates.TemplateResponse("index.html", {
            "request": request, "errors": errors,
            "today": date.today().isoformat(), "cookie_browser": browser,
            "browsers": ["chrome", "firefox", "edge", "brave", "safari", "chromium"],
        })

    if not rows:
        errors.append("File CSV rỗng.")
        return templates.TemplateResponse("index.html", {
            "request": request, "errors": errors,
            "today": date.today().isoformat(), "cookie_browser": browser,
            "browsers": ["chrome", "firefox", "edge", "brave", "safari", "chromium"],
        })

    # 3. Lọc theo ngày
    filtered = filter_by_date(rows, d_start, d_end)
    if not filtered:
        errors.append(f"Không có video nào trong khoảng {start_date} → {end_date}.")
        return templates.TemplateResponse("index.html", {
            "request": request, "errors": errors,
            "today": date.today().isoformat(), "cookie_browser": browser,
            "browsers": ["chrome", "firefox", "edge", "brave", "safari", "chromium"],
        })

    # 4. Tạo thư mục lưu theo ngày hôm nay
    save_dir = DOWNLOADS_DIR / date.today().isoformat()

    # 5. Tải từng video bằng yt-dlp
    for row in filtered:
        title      = row.get("title", "").strip()
        video_id   = row.get("video_id", "").strip()
        source_url = row.get("source_url", "").strip()
        created_at = row.get("created_at", "").strip()

        record = {
            "video_id": video_id,
            "title": title or f"video_{video_id}",
            "source_url": source_url,
            "created_at": created_at,
            "source_profile_url": source_profile_url,
            "browser": browser,
            "downloaded_at": datetime.now().isoformat(),
            "status": "",
            "local_path": "",
            "error_message": "",
        }

        # Validate source_url
        if not source_url or not source_url.startswith("http"):
            record["status"] = "failed"
            record["error_message"] = "source_url trống hoặc không hợp lệ."
        else:
            fname_hint = f"{video_id}_{title}" if video_id else (title or "video")
            success, local_path, err_msg = download_with_ytdlp(source_url, save_dir, fname_hint, browser)
            record["status"]       = "success" if success else "failed"
            record["local_path"]   = local_path
            record["error_message"] = err_msg

        results.append(record)

    # 6. Lưu history
    history = load_history()
    batch = {
        "batch_id": datetime.now().strftime("%Y%m%d_%H%M%S"),
        "source_profile_url": source_profile_url,
        "date_range": f"{start_date} → {end_date}",
        "csv_filename": csv_file.filename,
        "browser": browser,
        "total":   len(results),
        "success": sum(1 for r in results if r["status"] == "success"),
        "failed":  sum(1 for r in results if r["status"] == "failed"),
        "records": results,
        "created_at": datetime.now().isoformat(),
    }
    history.insert(0, batch)
    save_history(history)

    return templates.TemplateResponse("index.html", {
        "request": request,
        "results": results,
        "batch": batch,
        "today": date.today().isoformat(),
        "start_date": start_date,
        "end_date": end_date,
        "source_profile_url": source_profile_url,
        "cookie_browser": browser,
        "browsers": ["chrome", "firefox", "edge", "brave", "safari", "chromium"],
    })


@app.get("/history", response_class=HTMLResponse)
async def history_page(request: Request):
    history = load_history()
    return templates.TemplateResponse("history.html", {
        "request": request,
        "history": history,
    })