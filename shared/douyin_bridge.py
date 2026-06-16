from __future__ import annotations

import sqlite3
from datetime import datetime
from pathlib import Path

from pipeline_core.db import DEFAULT_DB_PATH as DEFAULT_PIPELINE_DB_PATH
from pipeline_core.models import VideoRecord
from pipeline_core.repository import upsert_video_record
from shared.title_from_srt import generate_title_from_srt

BASE_DIR = Path(__file__).resolve().parent.parent
DEFAULT_DOUYIN_PROJECT_DIR = BASE_DIR.parent / "douyin_downloader"
DEFAULT_DOUYIN_DB_PATH = DEFAULT_DOUYIN_PROJECT_DIR / "data" / "app.db"


def sync_douyin_finals(
    douyin_db_path: str | Path = DEFAULT_DOUYIN_DB_PATH,
    pipeline_db_path: str | Path = DEFAULT_PIPELINE_DB_PATH,
    limit: int = 100,
    dry_run: bool = False,
    progress_cb=None,
) -> dict:
    """Import completed Douyin final videos into the Facebook publish database."""
    douyin_db = Path(douyin_db_path)
    if not douyin_db.exists():
        raise FileNotFoundError(f"Douyin database not found: {douyin_db}")

    rows = _merge_douyin_db_and_output_finals(douyin_db, limit=limit)
    summary = {"total": len(rows), "imported": 0, "skipped": 0, "dry_run": dry_run, "items": []}
    _emit(progress_cb, "start", f"Found {len(rows)} Douyin final video(s).", total=len(rows))

    for index, row in enumerate(rows, start=1):
        final_path = _resolve_final_path(row, douyin_db)
        if not final_path.exists() or not final_path.is_file():
            summary["skipped"] += 1
            item = {
                "source_video_id": _get(row, "source_video_id"),
                "status": "skipped",
                "message": f"Final video missing: {final_path}",
            }
            summary["items"].append(item)
            _emit(progress_cb, "item_skip", f"[{index}/{len(rows)}] {item['message']}", **item)
            continue

        title = _title_for_publish(row, final_path, douyin_db, dry_run=dry_run, progress_cb=progress_cb, index=index, total=len(rows))
        record = _row_to_publish_record(row, final_path, title=title)
        item = {
            "source_video_id": record.source_video_id,
            "local_filename": record.local_filename,
            "title": record.title_original,
            "status": "dry_run" if dry_run else "imported",
        }
        if not dry_run:
            item["pipeline_video_id"] = upsert_video_record(record, db_path=pipeline_db_path)
            summary["imported"] += 1
        summary["items"].append(item)
        _emit(
            progress_cb,
            "item_done",
            f"[{index}/{len(rows)}] {'Dry run' if dry_run else 'Imported'} {final_path.name} | title={record.title_original}",
            **item,
        )

    _emit(progress_cb, "done", f"Sync finished. imported={summary['imported']}, skipped={summary['skipped']}", summary=summary)
    return summary


def _merge_douyin_db_and_output_finals(db_path: Path, limit: int) -> list[sqlite3.Row | dict]:
    rows = _list_douyin_final_rows(db_path, limit=limit)
    project_dir = db_path.parent.parent
    seen_paths = {str(_resolve_final_path(row, db_path).resolve()).lower() for row in rows if _get(row, "final_video_path")}

    final_dir = project_dir / "output" / "final"
    if final_dir.exists():
        files = sorted(final_dir.glob("*.mp4"), key=lambda path: path.stat().st_mtime, reverse=True)
        for final_path in files:
            resolved = str(final_path.resolve()).lower()
            if resolved in seen_paths:
                continue
            source_id = _strip_final_suffix(final_path.stem)
            rows.append({
                "source_video_id": source_id,
                "source_video_url": str(final_path),
                "local_video_path": str(final_path),
                "title": source_id,
                "final_video_path": str(final_path),
                "updated_at": datetime.fromtimestamp(final_path.stat().st_mtime).isoformat(timespec="seconds"),
                "created_at": datetime.fromtimestamp(final_path.stat().st_ctime).isoformat(timespec="seconds"),
            })
            seen_paths.add(resolved)
            if len(rows) >= limit:
                break
    return rows[:limit]


def _list_douyin_final_rows(db_path: Path, limit: int) -> list[sqlite3.Row]:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        return list(
            conn.execute(
                """
                SELECT * FROM videos
                WHERE COALESCE(final_video_path, '') != ''
                  AND video_merge_status = 'completed'
                ORDER BY updated_at DESC, id DESC
                LIMIT ?
                """,
                (int(limit),),
            )
        )
    finally:
        conn.close()


def _title_for_publish(
    row: sqlite3.Row | dict,
    final_path: Path,
    douyin_db: Path,
    *,
    dry_run: bool,
    progress_cb=None,
    index: int = 0,
    total: int = 0,
) -> str:
    fallback_title = _strip_final_suffix(str(_get(row, "title") or final_path.stem)).strip() or _strip_final_suffix(final_path.stem)
    if not _needs_ai_title(fallback_title, row, final_path):
        return fallback_title

    srt_path = _find_srt_for_row(row, final_path, douyin_db)
    if not srt_path:
        _emit(progress_cb, "title_skip", f"[{index}/{total}] No SRT found for title generation: {final_path.name}")
        return fallback_title

    try:
        _emit(progress_cb, "title_start", f"[{index}/{total}] Generating AI title from SRT: {srt_path.name}")
        title = generate_title_from_srt(srt_path)
        if title:
            _emit(progress_cb, "title_done", f"[{index}/{total}] AI title: {title}")
            if not dry_run:
                _update_douyin_title(row, douyin_db, title)
            return title
    except Exception as exc:
        _emit(progress_cb, "title_error", f"[{index}/{total}] AI title failed: {exc}")
    return fallback_title


def _row_to_publish_record(row: sqlite3.Row | dict, final_path: Path, *, title: str | None = None) -> VideoRecord:
    raw_source_id = _strip_final_suffix(str(_get(row, "source_video_id") or final_path.stem))
    source_video_id = f"douyin_{raw_source_id}"
    publish_title = (title or _strip_final_suffix(str(_get(row, "title") or final_path.stem))).strip() or _strip_final_suffix(final_path.stem)
    created_at = str(_get(row, "updated_at") or _get(row, "created_at") or datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
    return VideoRecord(
        source_url=str(_get(row, "source_video_url") or _get(row, "local_video_path") or final_path),
        source_video_id=source_video_id,
        source_page_url="douyin_downloader",
        title_original=publish_title,
        title_rewrite=publish_title,
        created_at=created_at,
        local_filename=str(final_path.resolve()),
        download_status="downloaded",
        publish_status="pending",
        schedule_time="",
        fb_post_id="",
        error_message="",
    )


def _needs_ai_title(title: str, row: sqlite3.Row | dict, final_path: Path) -> bool:
    title = (title or "").strip()
    if not title:
        return True
    source_id = _strip_final_suffix(str(_get(row, "source_video_id") or final_path.stem))
    if title == source_id or title == _strip_final_suffix(final_path.stem):
        return True
    if "_" in title and len(title) >= 35:
        return True
    if title.lower().startswith("douyin_"):
        return True
    return False


def _find_srt_for_row(row: sqlite3.Row | dict, final_path: Path, db_path: Path) -> Path | None:
    candidates: list[Path] = []
    for key in ("translated_srt_path", "transcript_srt_path"):
        raw = str(_get(row, key) or "").strip()
        if raw:
            path = Path(raw)
            candidates.append(path if path.is_absolute() else db_path.parent.parent / path)

    candidates.extend([
        final_path.with_suffix(".srt"),
        final_path.parent / f"{_strip_final_suffix(final_path.stem)}.final.srt",
        final_path.parent / f"{final_path.stem}.srt",
    ])

    project_dir = db_path.parent.parent
    work_dir = project_dir / "output" / "work"
    stem = _strip_final_suffix(final_path.stem)
    if work_dir.exists():
        candidates.extend(work_dir.glob(f"**/{stem}.vi.transcript.srt"))
        candidates.extend(work_dir.glob(f"**/{stem}.final.vi.transcript.srt"))
        candidates.extend(work_dir.glob(f"**/{stem}*.vi.transcript.srt"))

    seen: set[str] = set()
    for candidate in candidates:
        key = str(candidate.resolve()).lower() if candidate.exists() else str(candidate).lower()
        if key in seen:
            continue
        seen.add(key)
        if candidate.exists() and candidate.is_file():
            return candidate
    return None


def _update_douyin_title(row: sqlite3.Row | dict, db_path: Path, title: str) -> None:
    source_id = str(_get(row, "source_video_id") or "").strip()
    final_path = str(_get(row, "final_video_path") or "").strip()
    if not source_id and not final_path:
        return
    conn = sqlite3.connect(db_path)
    try:
        if source_id:
            conn.execute("UPDATE videos SET title = ?, updated_at = datetime('now') WHERE source_video_id = ?", (title, source_id))
        elif final_path:
            conn.execute("UPDATE videos SET title = ?, updated_at = datetime('now') WHERE final_video_path = ?", (title, final_path))
        conn.commit()
    finally:
        conn.close()


def _resolve_final_path(row: sqlite3.Row | dict, db_path: Path) -> Path:
    raw_path = Path(_get(row, "final_video_path") or "")
    if raw_path.is_absolute():
        return raw_path
    return db_path.parent.parent / raw_path


def _strip_final_suffix(value: str) -> str:
    while value.endswith(".final"):
        value = value[:-6]
    return value


def _get(row: sqlite3.Row | dict, key: str, default=""):
    try:
        value = row[key]
    except Exception:
        value = default
    return value if value is not None else default


def _emit(progress_cb, event: str, log_message: str, **payload) -> None:
    if progress_cb:
        progress_cb({**payload, "event": event, "message": log_message})