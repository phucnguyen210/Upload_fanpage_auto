import logging
import re
import subprocess
import sys
from datetime import date
from pathlib import Path

from shared.database import DEFAULT_DB_PATH
from shared.repository import list_download_pending, update_video_fields

logger = logging.getLogger(__name__)


def safe_filename(name: str) -> str:
    name = re.sub(r'[\\/:*?"<>|]', "_", name or "")
    name = name.strip(". ")
    return name[:80] if name else "video"


def download_pending(
    db_path=DEFAULT_DB_PATH,
    output_dir: str | Path = "data/downloads",
    browser: str = "chrome",
    limit: int = 10,
    dry_run: bool = False,
    progress_cb=None,
) -> dict:
    rows = list_download_pending(db_path=db_path, limit=limit)
    summary = {"total": len(rows), "downloaded": 0, "failed": 0, "dry_run": dry_run, "items": []}
    base_dir = Path(output_dir) / date.today().isoformat()

    _emit(progress_cb, "start", f"Found {len(rows)} pending videos.", total=len(rows))
    if dry_run:
        _emit(progress_cb, "dry_run", "Dry run is ON. No video files will be downloaded.")
    else:
        _emit(progress_cb, "output_dir", f"Downloaded files will be saved under: {base_dir}")

    for index, row in enumerate(rows, start=1):
        logger.info("Download candidate id=%s source_url=%s", row["id"], row["source_url"])
        _emit(
            progress_cb,
            "item_start",
            f"[{index}/{len(rows)}] Preparing video id={row['id']}: {row['title_original'] or row['source_url']}",
            id=row["id"],
            index=index,
            total=len(rows),
            source_url=row["source_url"],
        )
        if dry_run:
            summary["items"].append({
                "id": row["id"],
                "status": "dry_run",
                "source_url": row["source_url"],
                "message": "Dry run only; not downloaded.",
            })
            _emit(progress_cb, "item_done", f"[{index}/{len(rows)}] Dry run: {row['source_url']}", id=row["id"], status="dry_run")
            continue

        title = row["title_rewrite"] or row["title_original"] or row["source_video_id"] or f"video_{row['id']}"
        filename_hint = safe_filename(f"{row['source_video_id']}_{title}" if row["source_video_id"] else title)
        _emit(progress_cb, "item_step", f"[{index}/{len(rows)}] Running yt-dlp with browser={browser}...", id=row["id"])
        success, local_filename, error = download_with_ytdlp(
            source_url=row["source_url"],
            save_dir=base_dir,
            title_hint=filename_hint,
            browser=browser,
        )
        if success:
            update_video_fields(
                row["id"],
                {
                    "local_filename": local_filename,
                    "download_status": "downloaded",
                    "error_message": "",
                },
                db_path=db_path,
            )
            summary["downloaded"] += 1
            summary["items"].append({
                "id": row["id"],
                "status": "downloaded",
                "source_url": row["source_url"],
                "local_filename": local_filename,
                "message": "Downloaded successfully.",
            })
            _emit(progress_cb, "item_done", f"[{index}/{len(rows)}] Downloaded: {local_filename}", id=row["id"], status="downloaded")
        else:
            update_video_fields(
                row["id"],
                {
                    "download_status": "download_failed",
                    "error_message": error,
                },
                db_path=db_path,
            )
            summary["failed"] += 1
            summary["items"].append({
                "id": row["id"],
                "status": "failed",
                "source_url": row["source_url"],
                "message": error,
            })
            _emit(progress_cb, "item_error", f"[{index}/{len(rows)}] Failed: {error}", id=row["id"], status="failed", error=error)

    _emit(
        progress_cb,
        "done",
        f"Done. downloaded={summary['downloaded']}, failed={summary['failed']}, dry_run={summary['dry_run']}",
        summary=summary,
    )
    return summary


def download_with_ytdlp(source_url: str, save_dir: Path, title_hint: str, browser: str) -> tuple[bool, str, str]:
    save_dir.mkdir(parents=True, exist_ok=True)
    output_template = str(save_dir / f"{safe_filename(title_hint)}.%(ext)s")

    cmd = [
        sys.executable,
        "-m",
        "yt_dlp",
        "--cookies-from-browser",
        browser,
        "--format",
        "bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best",
        "--merge-output-format",
        "mp4",
        "--output",
        output_template,
        "--no-playlist",
        "--quiet",
        "--no-warnings",
        source_url,
    ]

    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
        if result.returncode != 0:
            error = result.stderr.strip() or result.stdout.strip() or f"yt-dlp failed with exit code {result.returncode}"
            return False, "", error[:500]

        files = sorted(save_dir.glob(f"{safe_filename(title_hint)}*.mp4"), key=lambda item: item.stat().st_mtime, reverse=True)
        if not files:
            files = sorted(save_dir.glob("*.mp4"), key=lambda item: item.stat().st_mtime, reverse=True)
        if not files:
            return False, "", "yt-dlp completed but no mp4 output was found."
        return True, str(files[0]), ""
    except subprocess.TimeoutExpired:
        return False, "", "Timeout after 5 minutes."
    except Exception as exc:
        return False, "", str(exc)


def _emit(progress_cb, event: str, message: str, **payload) -> None:
    if progress_cb:
        progress_cb({"event": event, "message": message, **payload})
