import logging
import threading
import uuid
from datetime import datetime
from pathlib import Path
from urllib.parse import urlencode

from fastapi import FastAPI, File, Form, Request, UploadFile
from fastapi.responses import FileResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from shared.database import DEFAULT_DB_PATH, init_db
from shared.downloader import download_pending
from shared.importers import read_legacy_rows, rows_to_video_records
from shared.publisher import publish_pending
from shared.publisher import resolve_video_path
from shared.repository import get_pipeline_stats, list_recent_videos, save_video_record
from shared.repository import delete_video_ids, delete_videos_for_view, list_videos_for_view
from shared.scheduler import generate_schedule

BASE_DIR = Path(__file__).resolve().parent
PROJECT_DIR = BASE_DIR.parent

logger = logging.getLogger(__name__)

app = FastAPI(title="Unified Video Pipeline")
app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))

_jobs: dict[str, dict] = {}


@app.on_event("startup")
async def startup() -> None:
    init_db(DEFAULT_DB_PATH)


@app.get("/")
async def dashboard(request: Request, msg: str = "", error: str = "", view: str = "action"):
    init_db(DEFAULT_DB_PATH)
    valid_views = {"action", "need_download", "downloaded", "need_publish", "failed", "mock", "empty_source", "all"}
    if view not in valid_views:
        view = "action"
    return templates.TemplateResponse(
        request,
        "dashboard.html",
        context={
            "request": request,
            "stats": get_pipeline_stats(DEFAULT_DB_PATH),
            "videos": list_videos_for_view(DEFAULT_DB_PATH, view=view, limit=120),
            "jobs": _recent_jobs(),
            "message": msg,
            "error": error,
            "db_path": DEFAULT_DB_PATH,
            "view": view,
        },
    )


@app.post("/import-excel")
async def import_excel(import_file: UploadFile = File(...)):
    try:
        filename = import_file.filename or ""
        file_bytes = await import_file.read()
        rows = read_legacy_rows(file_bytes, filename)
        records = rows_to_video_records(rows)
        imported = 0
        suspicious = 0
        for record in records:
            if _suspicious_title(record.title_original) or _suspicious_title(record.title_rewrite):
                suspicious += 1
            save_video_record(record, db_path=DEFAULT_DB_PATH)
            imported += 1
        msg = f"Imported {imported} records from {filename}."
        if suspicious:
            msg += f" Warning: {suspicious} titles contain '?' and should be fixed before publishing."
        return _redirect(msg=msg)
    except Exception as exc:
        logger.exception("Import failed")
        return _redirect(error=str(exc))


@app.post("/download-pending")
async def run_download_pending(
    limit: int = Form(10),
    browser: str = Form("chrome"),
    output_dir: str = Form("data/downloads"),
    dry_run: str = Form(""),
):
    try:
        job_id = _start_download_job(
            limit=limit,
            browser=browser,
            output_dir=output_dir,
            dry_run=_checked(dry_run),
        )
        return RedirectResponse(url=f"/jobs/{job_id}", status_code=303)
    except Exception as exc:
        logger.exception("Download failed")
        return _redirect(error=str(exc))


@app.get("/jobs/{job_id}")
async def job_page(request: Request, job_id: str):
    job = _jobs.get(job_id)
    if not job:
        return _redirect(error="Job not found or server was restarted.")
    return templates.TemplateResponse(
        request,
        "job.html",
        context={"request": request, "job": job, "job_id": job_id},
    )


@app.get("/jobs/{job_id}/status")
async def job_status(job_id: str):
    job = _jobs.get(job_id)
    if not job:
        return {"found": False, "status": "missing", "logs": []}
    return {"found": True, **job}


@app.get("/videos/{video_id}/file")
async def video_file(video_id: int):
    videos = [row for row in list_recent_videos(DEFAULT_DB_PATH, limit=1000) if row["id"] == video_id]
    if not videos:
        return _redirect(error="Video not found.")

    local_filename = videos[0]["local_filename"]
    if not local_filename:
        return _redirect(error="This video has not been downloaded yet.")

    path = resolve_video_path(local_filename).resolve()
    project_root = PROJECT_DIR.resolve()
    if project_root not in path.parents and path != project_root:
        return _redirect(error="File path is outside the project folder.")
    if not path.exists() or not path.is_file():
        return _redirect(error=f"File not found: {local_filename}")

    return FileResponse(path, media_type="video/mp4", filename=path.name)


@app.post("/cleanup")
async def cleanup(view: str = Form(...), confirm: str = Form("")):
    try:
        if confirm != "DELETE":
            return _redirect(error="Type DELETE to confirm cleanup.")
        deleted = delete_videos_for_view(DEFAULT_DB_PATH, view=view)
        return _redirect(msg=f"Deleted {deleted} rows for cleanup view '{view}'.")
    except Exception as exc:
        logger.exception("Cleanup failed")
        return _redirect(error=str(exc))


@app.post("/delete-selected")
async def delete_selected(video_ids: list[int] = Form(default=[]), confirm: str = Form("")):
    try:
        if confirm != "DELETE":
            return _redirect(error="Type DELETE to confirm selected delete.")
        deleted = delete_video_ids(video_ids, DEFAULT_DB_PATH)
        return _redirect(msg=f"Deleted {deleted} selected rows.")
    except Exception as exc:
        logger.exception("Delete selected failed")
        return _redirect(error=str(exc))


@app.post("/generate-schedule")
async def run_generate_schedule(
    start_time: str = Form(""),
    interval_minutes: int = Form(60),
    limit: int = Form(50),
    dry_run: str = Form(""),
):
    try:
        result = generate_schedule(
            db_path=DEFAULT_DB_PATH,
            start_time=start_time.strip(),
            interval_minutes=interval_minutes,
            limit=limit,
            dry_run=_checked(dry_run),
        )
        return _redirect(msg=f"Schedule result: total={result['total']}, updated={result['updated']}, dry_run={result['dry_run']}")
    except Exception as exc:
        logger.exception("Schedule generation failed")
        return _redirect(error=str(exc))


@app.post("/publish-pending")
async def run_publish_pending(
    limit: int = Form(10),
    page_id: str = Form(""),
    page_access_token: str = Form(""),
    dry_run: str = Form(""),
):
    try:
        job_id = _start_publish_job(
            limit=limit,
            page_id=page_id.strip(),
            page_access_token=page_access_token.strip(),
            dry_run=_checked(dry_run),
        )
        return RedirectResponse(url=f"/jobs/{job_id}", status_code=303)
    except Exception as exc:
        logger.exception("Publish failed")
        return _redirect(error=str(exc))


def _checked(value: str) -> bool:
    return value in {"on", "true", "1", "yes"}


def _redirect(msg: str = "", error: str = "") -> RedirectResponse:
    query = urlencode({"msg": msg, "error": error})
    return RedirectResponse(url=f"/?{query}", status_code=303)


def _start_download_job(limit: int, browser: str, output_dir: str, dry_run: bool) -> str:
    job_id = str(uuid.uuid4())
    _jobs[job_id] = {
        "id": job_id,
        "type": "download",
        "status": "running",
        "created_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "finished_at": "",
        "logs": [],
        "result": None,
        "error": "",
    }

    def progress(event: dict) -> None:
        _append_job_log(job_id, event)

    def runner() -> None:
        try:
            _append_job_log(job_id, {"event": "job_start", "message": "Download job started."})
            result = download_pending(
                db_path=DEFAULT_DB_PATH,
                output_dir=output_dir,
                browser=browser,
                limit=limit,
                dry_run=dry_run,
                progress_cb=progress,
            )
            _jobs[job_id]["result"] = result
            _jobs[job_id]["status"] = "done"
            _append_job_log(job_id, {"event": "job_done", "message": "Download job finished."})
        except Exception as exc:
            logger.exception("Download job failed")
            _jobs[job_id]["status"] = "failed"
            _jobs[job_id]["error"] = str(exc)
            _append_job_log(job_id, {"event": "job_error", "message": str(exc)})
        finally:
            _jobs[job_id]["finished_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    thread = threading.Thread(target=runner, daemon=True)
    thread.start()
    return job_id


def _start_publish_job(limit: int, page_id: str, page_access_token: str, dry_run: bool) -> str:
    job_id = _create_job("publish")

    def progress(event: dict) -> None:
        _append_job_log(job_id, event)

    def runner() -> None:
        try:
            _append_job_log(job_id, {"event": "job_start", "message": "Publish job started."})
            result = publish_pending(
                db_path=DEFAULT_DB_PATH,
                page_id=page_id,
                page_access_token=page_access_token,
                limit=limit,
                dry_run=dry_run,
                progress_cb=progress,
            )
            _jobs[job_id]["result"] = result
            _jobs[job_id]["status"] = "done"
            _append_job_log(job_id, {"event": "job_done", "message": "Publish job finished."})
        except Exception as exc:
            logger.exception("Publish job failed")
            _jobs[job_id]["status"] = "failed"
            _jobs[job_id]["error"] = str(exc)
            _append_job_log(job_id, {"event": "job_error", "message": str(exc)})
        finally:
            _jobs[job_id]["finished_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    thread = threading.Thread(target=runner, daemon=True)
    thread.start()
    return job_id


def _create_job(job_type: str) -> str:
    job_id = str(uuid.uuid4())
    _jobs[job_id] = {
        "id": job_id,
        "type": job_type,
        "status": "running",
        "created_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "finished_at": "",
        "logs": [],
        "result": None,
        "error": "",
    }
    return job_id


def _append_job_log(job_id: str, event: dict) -> None:
    job = _jobs.get(job_id)
    if not job:
        return
    job["logs"].append({
        "time": datetime.now().strftime("%H:%M:%S"),
        "event": event.get("event", "log"),
        "message": event.get("message", ""),
        "payload": {k: v for k, v in event.items() if k not in {"event", "message"}},
    })


def _recent_jobs(limit: int = 8) -> list[dict]:
    jobs = sorted(
        _jobs.values(),
        key=lambda job: job.get("created_at", ""),
        reverse=True,
    )
    return jobs[:limit]


def _suspicious_title(title: str) -> bool:
    if not title:
        return False
    return "?" in title or "�" in title or "Ã" in title
