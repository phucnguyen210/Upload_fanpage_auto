from datetime import datetime, timedelta

from shared.database import DEFAULT_DB_PATH, get_connection, init_db
from shared.repository import update_video_fields


def generate_schedule(
    db_path=DEFAULT_DB_PATH,
    start_time: str = "",
    interval_minutes: int = 60,
    limit: int = 50,
    dry_run: bool = False,
) -> dict:
    if interval_minutes <= 0:
        raise ValueError("interval_minutes must be greater than 0.")

    start = _parse_start_time(start_time)
    rows = _list_schedule_candidates(db_path, limit)
    planned = []

    for index, row in enumerate(rows):
        schedule_time = (start + timedelta(minutes=interval_minutes * index)).strftime("%Y-%m-%d %H:%M:%S")
        planned.append({"id": row["id"], "local_filename": row["local_filename"], "schedule_time": schedule_time})
        if not dry_run:
            update_video_fields(row["id"], {"schedule_time": schedule_time}, db_path=db_path)

    return {"total": len(rows), "updated": 0 if dry_run else len(rows), "dry_run": dry_run, "planned": planned}


def _parse_start_time(value: str) -> datetime:
    if value:
        return datetime.strptime(value, "%Y-%m-%d %H:%M:%S")
    now = datetime.now().replace(second=0, microsecond=0)
    return now + timedelta(hours=1)


def _list_schedule_candidates(db_path, limit: int):
    init_db(db_path)
    with get_connection(db_path) as conn:
        return list(
            conn.execute(
                """
                SELECT * FROM videos
                WHERE COALESCE(local_filename, '') != ''
                  AND COALESCE(schedule_time, '') = ''
                  AND download_status = 'downloaded'
                ORDER BY created_at ASC, id ASC
                LIMIT ?
                """,
                (limit,),
            )
        )
