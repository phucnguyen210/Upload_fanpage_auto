import logging

from shared.database import DEFAULT_DB_PATH
from shared.repository import list_title_enrich_candidates, update_video_fields

logger = logging.getLogger(__name__)


def enrich_titles(
    db_path=DEFAULT_DB_PATH,
    browser: str = "chrome",
    limit: int = 20,
    dry_run: bool = False,
    progress_cb=None,
) -> dict:
    rows = list_title_enrich_candidates(db_path=db_path, limit=limit)
    summary = {"total": len(rows), "updated": 0, "failed": 0, "dry_run": dry_run, "items": []}
    _emit(progress_cb, "start", f"Found {len(rows)} videos with missing/bad titles.", total=len(rows))

    for index, row in enumerate(rows, start=1):
        _emit(
            progress_cb,
            "item_start",
            f"[{index}/{len(rows)}] Reading metadata for id={row['id']}",
            id=row["id"],
            index=index,
            total=len(rows),
            source_url=row["source_url"],
        )
        title, error = extract_title_with_ytdlp(row["source_url"], browser=browser)
        if not title:
            summary["failed"] += 1
            summary["items"].append({"id": row["id"], "status": "failed", "message": error})
            _emit(progress_cb, "item_error", f"[{index}/{len(rows)}] Could not get title: {error}", id=row["id"])
            continue

        if dry_run:
            status = "dry_run"
        else:
            update_video_fields(row["id"], {"title_original": title, "error_message": ""}, db_path=db_path)
            summary["updated"] += 1
            status = "updated"

        summary["items"].append({"id": row["id"], "status": status, "title": title})
        _emit(progress_cb, "item_done", f"[{index}/{len(rows)}] {status}: {title}", id=row["id"], title=title)

    _emit(progress_cb, "done", f"Done. updated={summary['updated']}, failed={summary['failed']}", summary=summary)
    return summary


def extract_title_with_ytdlp(source_url: str, browser: str = "chrome") -> tuple[str, str]:
    try:
        import yt_dlp
    except ImportError:
        return "", "yt-dlp is not installed."

    options = {
        "quiet": True,
        "no_warnings": True,
        "skip_download": True,
        "ignoreerrors": True,
    }
    if browser:
        options["cookiesfrombrowser"] = (browser,)

    try:
        with yt_dlp.YoutubeDL(options) as ydl:
            info = ydl.extract_info(source_url, download=False)
    except Exception as exc:
        logger.exception("Title metadata extraction failed for %s", source_url)
        return "", str(exc)

    if not info:
        return "", "No metadata returned."

    title = _best_title(info)
    if not title:
        return "", "Metadata did not contain a usable title."
    return title, ""


def _best_title(info: dict) -> str:
    for key in ("title", "fulltitle", "description"):
        value = str(info.get(key) or "").strip()
        if _usable_title(value):
            return value[:240]
    return ""


def _usable_title(value: str) -> bool:
    if not value:
        return False
    lowered = value.lower()
    bad_parts = ["bản xem trước", "thước phim", "preview", "facebook video"]
    return not any(part in lowered for part in bad_parts)


def _emit(progress_cb, event: str, message: str, **payload) -> None:
    if progress_cb:
        progress_cb({"event": event, "message": message, **payload})
