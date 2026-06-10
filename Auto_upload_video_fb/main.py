"""
Facebook Video Posting Tool - FastAPI
Dùng Meta Graph API chính thức để đăng video lên Fanpage Facebook.
Yêu cầu: Page ID + Page Access Token hợp lệ.
"""

import json
import os
import re
import unicodedata
import asyncio
from datetime import datetime
from io import BytesIO
from pathlib import Path
from typing import Optional, AsyncGenerator

import pandas as pd
import pytz
import requests
from fastapi import FastAPI, File, Form, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

# ─────────────────────────────────────────────
# Config
# ─────────────────────────────────────────────
BASE_DIR = Path(__file__).parent
GRAPH_API_VERSION = "v19.0"
HISTORY_FILE = BASE_DIR / "history_post.json"
VIDEOS_DIR = BASE_DIR / "videos"
DOWNLOADS_DIR = BASE_DIR / "downloads"

VIDEOS_DIR.mkdir(exist_ok=True)
DOWNLOADS_DIR.mkdir(exist_ok=True)

app = FastAPI(title="Facebook Video Tool")
app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))


# ─────────────────────────────────────────────
# Helpers: History
# ─────────────────────────────────────────────

def load_post_history() -> list:
    if not HISTORY_FILE.exists():
        return []
    try:
        with open(HISTORY_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
            return data if isinstance(data, list) else []
    except Exception:
        return []


def save_post_history(items: list):
    safe_items = []
    for item in items:
        safe = {k: v for k, v in item.items() if "token" not in k.lower()}
        safe_items.append(safe)
    with open(HISTORY_FILE, "w", encoding="utf-8") as f:
        json.dump(safe_items, f, ensure_ascii=False, indent=2)


# ─────────────────────────────────────────────
# Helpers: Data
# ─────────────────────────────────────────────

def read_excel_or_csv(file_bytes: bytes, filename: str) -> list[dict]:
    suffix = Path(filename).suffix.lower()
    if suffix == ".csv":
        df = pd.read_csv(BytesIO(file_bytes), dtype=str, encoding="utf-8-sig")
    elif suffix == ".xlsx":
        df = pd.read_excel(BytesIO(file_bytes), dtype=str, engine="openpyxl")
    else:
        raise ValueError("Chỉ hỗ trợ file .xlsx hoặc .csv.")

    df.columns = [c.strip().lower().replace(" ", "_") for c in df.columns]
    df = df.fillna("")

    required = {"filename", "title"}
    missing = required - set(df.columns)
    if missing:
        raise ValueError(f"File thiếu cột bắt buộc: {', '.join(sorted(missing))}")

    rows = []
    for _, row in df.iterrows():
        rows.append({
            "filename": str(row.get("filename", "")).strip(),
            "title": str(row.get("title", "")).strip(),
            "hashtag": str(row.get("hashtag", "")).strip(),
            "scheduled_time": str(row.get("scheduled_time", "")).strip(),
        })
    return rows


def remove_vietnamese_accents(text: str) -> str:
    text = unicodedata.normalize("NFD", text)
    text = "".join(c for c in text if unicodedata.category(c) != "Mn")
    return text


def generate_hashtags(title: str) -> str:
    DEFAULT = "#video #viral #xuhuong #phimhay"
    if not title or len(title.strip()) < 5:
        return DEFAULT

    clean = remove_vietnamese_accents(title.lower())
    clean = re.sub(r"[^a-z0-9\s]", "", clean)
    words = [w for w in clean.split() if len(w) >= 3]

    stopwords = {"mot", "cua", "den", "vao", "len", "cho", "khi", "nhu",
                 "hay", "the", "roi", "con", "duoc", "nhung", "that", "neu",
                 "ban", "toi", "anh", "chi", "ong", "ba", "va", "la"}
    keywords = [w for w in words if w not in stopwords][:6]

    if len(keywords) < 3:
        return DEFAULT

    tags = ["#" + w for w in keywords[:5]]
    extras = ["#phimhay", "#viral", "#xuhuong"]
    for e in extras:
        if len(tags) < 5:
            tags.append(e)

    return " ".join(tags)


def sanitize_caption(text: str) -> str:
    text = text.strip()
    text = re.sub(r"\r\n|\r", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text[:63000]


def validate_video_file(path: Path) -> bool:
    return path.exists() and path.is_file() and path.stat().st_size > 0


def safe_video_path(base_path: Path, filename: str) -> Path:
    if not filename:
        raise ValueError("Tên file video bị trống.")
    if Path(filename).name != filename or "/" in filename or "\\" in filename:
        raise ValueError(f"Tên file không hợp lệ: '{filename}'. Chỉ nhập tên file, không nhập đường dẫn.")

    resolved_base = base_path.resolve()
    resolved_path = (resolved_base / filename).resolve()
    if resolved_base not in resolved_path.parents:
        raise ValueError(f"Tên file không hợp lệ: '{filename}'.")
    return resolved_path


def parse_scheduled_time(value: str, timezone: str = "Asia/Ho_Chi_Minh") -> Optional[int]:
    if not value or not value.strip():
        return None
    try:
        tz = pytz.timezone(timezone)
        dt_naive = datetime.strptime(value.strip(), "%Y-%m-%d %H:%M:%S")
        dt_local = tz.localize(dt_naive)
        dt_utc = dt_local.astimezone(pytz.utc)
        now_utc = datetime.now(pytz.utc)
        diff = (dt_utc - now_utc).total_seconds()
        if diff < 600:
            raise ValueError("Thời gian hẹn lịch phải cách ít nhất 10 phút trong tương lai.")
        if diff > 30 * 24 * 60 * 60:
            raise ValueError("Thời gian hẹn lịch không được quá 30 ngày trong tương lai.")
        return int(dt_utc.timestamp())
    except ValueError as e:
        raise e
    except Exception:
        raise ValueError(f"Không thể parse scheduled_time: '{value}'. Format yêu cầu: YYYY-MM-DD HH:MM:SS")


# ─────────────────────────────────────────────
# Core: Upload to Facebook — Resumable Upload API
# Xử lý được video lớn bất kỳ, tránh HTTP 413
# ─────────────────────────────────────────────

# Kích thước mỗi chunk: giữ dưới 10 MB để tránh proxy/server trả HTTP 413.
CHUNK_SIZE = 4 * 1024 * 1024


def _fb_error_message(resp: requests.Response) -> str:
    """Trích lỗi từ response Facebook, trả về chuỗi tiếng Việt dễ đọc."""
    http_status = resp.status_code
    if http_status == 413:
        return "[HTTP 413] Video chunk quá lớn hoặc request bị proxy/server chặn. Hãy thử lại với chunk nhỏ hơn."
    try:
        err_data = resp.json()
        fb_error = err_data.get("error", {})
        code     = fb_error.get("code", 0)
        subcode  = fb_error.get("error_subcode", "")
        msg      = fb_error.get("message", "Lỗi không xác định")

        if code in (190, 102):
            friendly = "Token hết hạn hoặc không hợp lệ. Hãy tạo lại Page Access Token."
        elif code in (200, 10):
            friendly = "Token thiếu quyền. Cần: pages_manage_posts, publish_video."
        elif code == 368:
            friendly = "Fanpage bị hạn chế đăng bài tạm thời."
        elif code == 100:
            # Facebook dùng code=100 cho cả lỗi parameter lẫn permission
            if "permission" in msg.lower():
                friendly = (
                    "Token thiếu quyền đăng video. "
                    "Cần thêm permission: publish_video và pages_manage_posts. "
                    "Vào Graph API Explorer → chọn đúng Page Access Token → tích chọn đủ 2 quyền này."
                )
            else:
                friendly = f"Tham số không hợp lệ: {msg}"
        elif code == 506:
            friendly = "Video trùng lặp — Facebook phát hiện video này đã được đăng trước đó."
        else:
            friendly = msg

        result = f"[HTTP {http_status}] [code={code}] {friendly}"
        if subcode:
            result += f" (subcode={subcode})"
        return result
    except Exception:
        return f"[HTTP {http_status}] {resp.text[:300]}"


def _resumable_upload(
    page_id: str,
    page_access_token: str,
    video_path: Path,
    caption: str,
    scheduled_unix: Optional[int],
    progress_cb=None,           # callback(uploaded_bytes, total_bytes)
) -> dict:
    """
    Upload video theo Resumable Upload API của Facebook.
    Tài liệu: https://developers.facebook.com/docs/graph-api/video-uploads

    Quy trình 3 bước:
      1. START  — khai báo file_size, nhận upload_session_id + start_offset
      2. TRANSFER — upload từng chunk theo offset
      3. FINISH — publish video với caption / scheduled_time
    """
    file_size = video_path.stat().st_size
    base_url  = f"https://graph.facebook.com/{GRAPH_API_VERSION}/{page_id}/videos"

    # ── BƯỚC 1: START ──────────────────────────────────────────────────────
    resp = requests.post(base_url, data={
        "access_token":  page_access_token,
        "upload_phase":  "start",
        "file_size":     str(file_size),
    }, timeout=30)

    if resp.status_code != 200:
        return {"success": False, "video_id": None,
                "error": "START phase: " + _fb_error_message(resp)}

    start_data        = resp.json()
    upload_session_id = start_data.get("upload_session_id")
    start_offset      = int(start_data.get("start_offset", 0))
    end_offset        = int(start_data.get("end_offset", min(CHUNK_SIZE, file_size)))

    if not upload_session_id:
        return {"success": False, "video_id": None,
                "error": f"START phase: không nhận được upload_session_id. Response: {start_data}"}

    # ── BƯỚC 2: TRANSFER (upload từng chunk) ───────────────────────────────
    with open(video_path, "rb") as vf:
        while start_offset < file_size:
            chunk_size  = end_offset - start_offset
            vf.seek(start_offset)
            chunk_data  = vf.read(chunk_size)

            resp = requests.post(base_url, data={
                "access_token":      page_access_token,
                "upload_phase":      "transfer",
                "upload_session_id": upload_session_id,
                "start_offset":      str(start_offset),
            }, files={
                "video_file_chunk": (video_path.name, chunk_data, "application/octet-stream"),
            }, timeout=120)

            if resp.status_code != 200:
                return {"success": False, "video_id": None,
                        "error": f"TRANSFER phase (offset={start_offset}): " + _fb_error_message(resp)}

            transfer_data = resp.json()
            new_start     = int(transfer_data.get("start_offset", end_offset))
            new_end       = int(transfer_data.get("end_offset",   min(new_start + CHUNK_SIZE, file_size)))

            if progress_cb:
                progress_cb(new_start, file_size)

            # Nếu Facebook trả start_offset == end_offset → đã nhận hết
            if new_start == new_end or new_start >= file_size:
                break

            start_offset = new_start
            end_offset   = new_end

    # ── BƯỚC 3: FINISH ─────────────────────────────────────────────────────
    finish_data = {
        "access_token":      page_access_token,
        "upload_phase":      "finish",
        "upload_session_id": upload_session_id,
        "description":       caption,
    }

    if scheduled_unix:
        finish_data["published"]               = "false"
        finish_data["scheduled_publish_time"]  = str(scheduled_unix)
    else:
        finish_data["published"] = "true"

    resp = requests.post(base_url, data=finish_data, timeout=60)

    if resp.status_code == 200:
        result = resp.json()
        if result.get("success") is False:
            return {"success": False, "video_id": None,
                    "error": f"FINISH phase: Facebook không xác nhận publish. Response: {result}"}
        return {"success": True, "video_id": result.get("video_id") or result.get("id"), "error": None}
    else:
        return {"success": False, "video_id": None,
                "error": "FINISH phase: " + _fb_error_message(resp)}


def post_video_to_facebook_page(
    page_id: str,
    page_access_token: str,
    video_path: Path,
    caption: str,
    scheduled_unix: Optional[int] = None,
    progress_cb=None,
) -> dict:
    """
    Entry point upload video. Tự động dùng Resumable Upload cho mọi kích thước.
    progress_cb(uploaded_bytes, total_bytes) — tùy chọn để stream tiến trình.
    """
    try:
        return _resumable_upload(
            page_id=page_id,
            page_access_token=page_access_token,
            video_path=video_path,
            caption=caption,
            scheduled_unix=scheduled_unix,
            progress_cb=progress_cb,
        )
    except requests.exceptions.Timeout:
        return {"success": False, "video_id": None,
                "error": "Timeout: kết nối quá chậm hoặc chunk quá lớn."}
    except requests.exceptions.ConnectionError:
        return {"success": False, "video_id": None,
                "error": "Không kết nối được tới Facebook API. Kiểm tra mạng."}
    except Exception as e:
        return {"success": False, "video_id": None,
                "error": f"Lỗi không mong đợi: {str(e)}"}


# ─────────────────────────────────────────────
# Core: Process Queue — generator cho SSE
# ─────────────────────────────────────────────

def sse_event(data: dict) -> str:
    """Đóng gói dict thành SSE event string."""
    return f"data: {json.dumps(data, ensure_ascii=False)}\n\n"


def process_post_queue_stream(
    rows: list[dict],
    page_id: str,
    page_access_token: str,
    video_dir: str,
    timezone: str,
):
    """Generator: yield SSE events cho từng bước xử lý."""
    if video_dir not in {"videos", "downloads"}:
        video_dir = "videos"
    base_path = DOWNLOADS_DIR if video_dir == "downloads" else VIDEOS_DIR
    results = []
    total = len(rows)

    yield sse_event({"type": "start", "total": total,
                     "msg": f"Bắt đầu xử lý {total} video..."})

    for idx, row in enumerate(rows, start=1):
        filename = row["filename"]
        title = row["title"]

        # Step: đang kiểm tra file
        yield sse_event({
            "type": "step", "idx": idx, "total": total,
            "filename": filename,
            "msg": f"[{idx}/{total}] 🔍 Kiểm tra file: {filename}"
        })

        item = {
            "filename": filename,
            "title": title,
            "hashtag": row["hashtag"],
            "scheduled_time": row["scheduled_time"],
            "status": "pending",
            "facebook_video_id": None,
            "error_message": None,
            "posted_at": None,
        }

        try:
            video_path = safe_video_path(base_path, filename)
        except ValueError as e:
            msg = str(e)
            item["status"] = "failed"
            item["error_message"] = msg
            results.append(item)
            yield sse_event({
                "type": "result", "idx": idx, "status": "failed",
                "filename": filename, "error": msg,
                "msg": f"[{idx}/{total}] ❌ FAILED — {msg}"
            })
            continue

        if not validate_video_file(video_path):
            msg = f"Không tìm thấy file '{filename}' trong thư mục {video_dir}/"
            item["status"] = "failed"
            item["error_message"] = msg
            results.append(item)
            yield sse_event({
                "type": "result", "idx": idx, "status": "failed",
                "filename": filename, "error": msg,
                "msg": f"[{idx}/{total}] ❌ FAILED — {msg}"
            })
            continue

        # Tính kích thước file
        size_mb = video_path.stat().st_size / 1024 / 1024
        yield sse_event({
            "type": "step", "idx": idx, "total": total,
            "filename": filename,
            "msg": f"[{idx}/{total}] 📁 File OK ({size_mb:.1f} MB) — Chuẩn bị caption..."
        })

        # Hashtag
        hashtag = row["hashtag"] if row["hashtag"] else generate_hashtags(title)
        caption = sanitize_caption(f"{title}\n\n{hashtag}")
        item["hashtag"] = hashtag

        # Parse scheduled_time
        scheduled_unix = None
        try:
            if row["scheduled_time"]:
                scheduled_unix = parse_scheduled_time(row["scheduled_time"], timezone)
                yield sse_event({
                    "type": "step", "idx": idx,
                    "filename": filename,
                    "msg": f"[{idx}/{total}] ⏰ Hẹn lịch: {row['scheduled_time']} → Unix={scheduled_unix}"
                })
        except ValueError as e:
            msg = str(e)
            item["status"] = "failed"
            item["error_message"] = msg
            results.append(item)
            yield sse_event({
                "type": "result", "idx": idx, "status": "failed",
                "filename": filename, "error": msg,
                "msg": f"[{idx}/{total}] ❌ FAILED — {msg}"
            })
            continue

        # Upload
        mode_label = f"hẹn lịch {row['scheduled_time']}" if scheduled_unix else "đăng ngay"
        yield sse_event({
            "type": "uploading", "idx": idx,
            "filename": filename,
            "msg": f"[{idx}/{total}] ⬆️  Bắt đầu Resumable Upload ({size_mb:.1f} MB — {mode_label})..."
        })

        # Buffer chứa progress events từ callback (upload chạy blocking)
        _prog_buf = []

        def _make_cb(_idx=idx, _total=total, _filename=filename, _buf=_prog_buf):
            _last = [-1]
            def cb(uploaded, ftotal):
                pct = int(uploaded / ftotal * 100) if ftotal else 0
                if pct != _last[0] and (pct % 10 == 0 or pct >= 100):
                    _last[0] = pct
                    _buf.append(sse_event({
                        "type": "chunk_progress",
                        "idx": _idx,
                        "filename": _filename,
                        "pct": pct,
                        "msg": f"[{_idx}/{_total}]   └─ {uploaded/1024/1024:.1f}/{ftotal/1024/1024:.1f} MB ({pct}%)"
                    }))
            return cb

        upload_result = post_video_to_facebook_page(
            page_id=page_id,
            page_access_token=page_access_token,
            video_path=video_path,
            caption=caption,
            scheduled_unix=scheduled_unix,
            progress_cb=_make_cb(),
        )
        yield from _prog_buf

        item["posted_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        if upload_result["success"]:
            item["facebook_video_id"] = upload_result["video_id"]
            item["status"] = "scheduled" if scheduled_unix else "published"
            status_label = "⏰ SCHEDULED" if scheduled_unix else "✅ PUBLISHED"
            yield sse_event({
                "type": "result", "idx": idx, "status": item["status"],
                "filename": filename,
                "video_id": upload_result["video_id"],
                "msg": f"[{idx}/{total}] {status_label} — video_id={upload_result['video_id']}"
            })
        else:
            item["status"] = "failed"
            item["error_message"] = upload_result["error"]
            yield sse_event({
                "type": "result", "idx": idx, "status": "failed",
                "filename": filename,
                "error": upload_result["error"],
                "msg": f"[{idx}/{total}] ❌ FAILED — {upload_result['error']}"
            })

        results.append(item)

    # Lưu history
    history = load_post_history()
    history.extend(results)
    save_post_history(history)

    summary = {
        "total": total,
        "published": sum(1 for r in results if r["status"] == "published"),
        "scheduled": sum(1 for r in results if r["status"] == "scheduled"),
        "failed": sum(1 for r in results if r["status"] == "failed"),
    }

    yield sse_event({
        "type": "done",
        "summary": summary,
        "results": results,
        "msg": (f"✅ Hoàn tất! "
                f"Đăng ngay: {summary['published']} | "
                f"Hẹn lịch: {summary['scheduled']} | "
                f"Lỗi: {summary['failed']}")
    })


# ─────────────────────────────────────────────
# In-memory job store (simple, no Redis)
# ─────────────────────────────────────────────
_pending_jobs: dict[str, dict] = {}   # job_id → {rows, page_id, ...}


# ─────────────────────────────────────────────
# Routes
# ─────────────────────────────────────────────

@app.get("/", response_class=HTMLResponse)
async def root():
    return RedirectResponse(url="/post-videos")


@app.get("/post-videos", response_class=HTMLResponse)
async def get_post_videos(request: Request):
    return templates.TemplateResponse("post_videos.html", {"request": request})


@app.post("/post-videos", response_class=HTMLResponse)
async def post_videos_action(
    request: Request,
    page_id: str = Form(...),
    page_access_token: str = Form(...),
    video_dir: str = Form("videos"),
    timezone: str = Form("Asia/Ho_Chi_Minh"),
    post_file: UploadFile = File(...),
):
    """Validate file, lưu job vào memory, trả về trang progress."""
    error = None
    job_id = None

    try:
        if not page_id.strip() or not page_access_token.strip():
            raise ValueError("Vui lòng nhập Page ID và Page Access Token.")

        filename = post_file.filename or ""
        suffix = Path(filename).suffix.lower()
        if suffix not in {".xlsx", ".csv"}:
            raise ValueError("Chỉ hỗ trợ file .xlsx hoặc .csv.")

        file_bytes = await post_file.read()
        rows = read_excel_or_csv(file_bytes, filename)

        if not rows:
            raise ValueError("File không có dữ liệu.")

        # Lưu job vào memory (không lưu token ra file)
        import uuid
        job_id = str(uuid.uuid4())
        _pending_jobs[job_id] = {
            "rows": rows,
            "page_id": page_id.strip(),
            "page_access_token": page_access_token.strip(),
            "video_dir": video_dir if video_dir in {"videos", "downloads"} else "videos",
            "timezone": timezone,
        }

    except Exception as e:
        error = str(e)

    return templates.TemplateResponse("post_videos.html", {
        "request": request,
        "job_id": job_id,
        "error": error,
        "total": len(_pending_jobs.get(job_id or "", {}).get("rows", [])),
    })


@app.get("/post-videos/stream/{job_id}")
async def stream_post_videos(job_id: str):
    """SSE endpoint: stream log từng bước upload."""
    job = _pending_jobs.pop(job_id, None)
    if not job:
        async def err_gen():
            yield sse_event({"type": "error", "msg": "Job không tìm thấy hoặc đã hết hạn."})
        return StreamingResponse(err_gen(), media_type="text/event-stream")

    def sync_generator():
        yield from process_post_queue_stream(**job)

    async def async_wrapper():
        import concurrent.futures
        loop = asyncio.get_running_loop()

        # Dùng sentinel vì StopIteration trong run_in_executor bị PEP 479
        # chuyển thành RuntimeError, không thể bắt bằng except StopIteration
        def safe_next(g):
            try:
                return next(g)
            except StopIteration:
                return None

        with concurrent.futures.ThreadPoolExecutor() as pool:
            gen = sync_generator()
            while True:
                chunk = await loop.run_in_executor(pool, safe_next, gen)
                if chunk is None:
                    break
                yield chunk

    return StreamingResponse(
        async_wrapper(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        }
    )


@app.get("/post-history", response_class=HTMLResponse)
async def get_post_history(request: Request):
    history = load_post_history()
    history_reversed = list(reversed(history))
    return templates.TemplateResponse("post_history.html", {
        "request": request,
        "history": history_reversed,
    })


@app.post("/clear-history")
async def clear_history():
    save_post_history([])
    return RedirectResponse(url="/post-history", status_code=303)
