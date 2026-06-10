import logging
import os
from pathlib import Path

from shared.database import DEFAULT_DB_PATH
from shared.repository import list_publish_pending, update_video_fields

logger = logging.getLogger(__name__)


def publish_pending(
    db_path=DEFAULT_DB_PATH,
    page_id: str = "",
    page_access_token: str = "",
    limit: int = 10,
    dry_run: bool = False,
    progress_cb=None,
) -> dict:
    rows = list_publish_pending(db_path=db_path, limit=limit)
    summary = {"total": len(rows), "published": 0, "scheduled": 0, "failed": 0, "dry_run": dry_run, "items": []}

    _emit(progress_cb, "start", f"Found {len(rows)} publish candidates.", total=len(rows))

    if dry_run:
        _emit(progress_cb, "dry_run", "Dry run is ON. No video will be sent to Facebook.")
        for index, row in enumerate(rows, start=1):
            logger.info("Publish candidate id=%s local_filename=%s schedule_time=%s", row["id"], row["local_filename"], row["schedule_time"])
            message = f"[{index}/{len(rows)}] Dry run: id={row['id']} file={row['local_filename']} schedule={row['schedule_time']}"
            summary["items"].append({
                "id": row["id"],
                "status": "dry_run",
                "local_filename": row["local_filename"],
                "schedule_time": row["schedule_time"],
                "message": "Dry run only; not published.",
            })
            _emit(progress_cb, "item_done", message, id=row["id"], index=index, total=len(rows), status="dry_run")
        _emit(progress_cb, "done", "Publish dry run finished.", summary=summary)
        return summary

    page_id = page_id or os.environ.get("FB_PAGE_ID", "")
    page_access_token = page_access_token or os.environ.get("FB_PAGE_ACCESS_TOKEN", "")
    if not page_id or not page_access_token:
        message = "page_id/page_access_token are required. Pass CLI args or set FB_PAGE_ID and FB_PAGE_ACCESS_TOKEN."
        _emit(progress_cb, "job_error", message)
        raise ValueError(message)

    from Auto_upload_video_fb.main import parse_scheduled_time, post_video_to_facebook_page, sanitize_caption

    for index, row in enumerate(rows, start=1):
        title = row["title_rewrite"] or row["title_original"]
        caption = sanitize_caption(title)
        video_path = resolve_video_path(row["local_filename"])
        _emit(
            progress_cb,
            "item_start",
            f"[{index}/{len(rows)}] Preparing publish id={row['id']} file={video_path} schedule={row['schedule_time']}",
            id=row["id"],
            index=index,
            total=len(rows),
            local_filename=row["local_filename"],
            schedule_time=row["schedule_time"],
        )
        try:
            if not video_path.exists():
                raise FileNotFoundError(f"Video file not found: {video_path}")

            scheduled_unix = parse_scheduled_time(row["schedule_time"])
            _emit(progress_cb, "item_step", f"[{index}/{len(rows)}] Parsed schedule unix={scheduled_unix}", id=row["id"])

            last_progress = {"pct": -1}

            def upload_progress(uploaded, total):
                pct = int(uploaded / total * 100) if total else 0
                if pct != last_progress["pct"] and (pct % 10 == 0 or pct >= 100):
                    last_progress["pct"] = pct
                    _emit(
                        progress_cb,
                        "upload_progress",
                        f"[{index}/{len(rows)}] Uploading {uploaded/1024/1024:.1f}/{total/1024/1024:.1f} MB ({pct}%)",
                        id=row["id"],
                        pct=pct,
                        uploaded=uploaded,
                        total_bytes=total,
                    )

            _emit(progress_cb, "item_step", f"[{index}/{len(rows)}] Sending video to Facebook...", id=row["id"])
            result = post_video_to_facebook_page(
                page_id=page_id,
                page_access_token=page_access_token,
                video_path=video_path,
                caption=caption,
                scheduled_unix=scheduled_unix,
                progress_cb=upload_progress,
            )
        except Exception as exc:
            result = {"success": False, "video_id": None, "error": str(exc)}

        if result.get("success"):
            status = "scheduled" if row["schedule_time"] else "published"
            update_video_fields(
                row["id"],
                {
                    "publish_status": status,
                    "fb_post_id": result.get("video_id") or "",
                    "error_message": "",
                },
                db_path=db_path,
            )
            if row["schedule_time"]:
                summary["scheduled"] += 1
            else:
                summary["published"] += 1
            summary["items"].append({
                "id": row["id"],
                "status": status,
                "local_filename": row["local_filename"],
                "schedule_time": row["schedule_time"],
                "fb_post_id": result.get("video_id") or "",
                "message": "Published successfully.",
            })
            _emit(
                progress_cb,
                "item_done",
                f"[{index}/{len(rows)}] {status}: fb_post_id={result.get('video_id') or '-'}",
                id=row["id"],
                status=status,
                fb_post_id=result.get("video_id") or "",
            )
        else:
            error = result.get("error") or "Unknown publish error."
            update_video_fields(
                row["id"],
                {
                    "publish_status": "post_failed",
                    "error_message": error,
                },
                db_path=db_path,
            )
            summary["failed"] += 1
            summary["items"].append({
                "id": row["id"],
                "status": "failed",
                "local_filename": row["local_filename"],
                "schedule_time": row["schedule_time"],
                "message": error,
            })
            _emit(progress_cb, "item_error", f"[{index}/{len(rows)}] Failed: {error}", id=row["id"], status="failed", error=error)

    _emit(
        progress_cb,
        "done",
        f"Done. published={summary['published']}, scheduled={summary['scheduled']}, failed={summary['failed']}, dry_run={summary['dry_run']}",
        summary=summary,
    )
    return summary


def resolve_video_path(local_filename: str) -> Path:
    path = Path(local_filename)
    if path.exists():
        return path

    candidates = [
        Path("Auto_upload_video_fb") / "videos" / local_filename,
        Path("Auto_upload_video_fb") / "downloads" / local_filename,
        Path("data") / "downloads" / local_filename,
    ]
    for candidate in candidates:
        if candidate.exists():
            return candidate

    return path


def _emit(progress_cb, event: str, message: str, **payload) -> None:
    if progress_cb:
        progress_cb({"event": event, "message": message, **payload})
