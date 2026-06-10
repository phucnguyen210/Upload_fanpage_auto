import sqlite3
from dataclasses import asdict
from pathlib import Path

from .db import DEFAULT_DB_PATH, get_connection, init_db
from .models import VideoRecord


UPSERT_COLUMNS = [
    "source_url",
    "source_video_id",
    "source_page_url",
    "title_original",
    "title_rewrite",
    "created_at",
    "local_filename",
    "download_status",
    "publish_status",
    "schedule_time",
    "fb_post_id",
    "error_message",
]


def upsert_video_record(
    record: VideoRecord,
    db_path: str | Path = DEFAULT_DB_PATH,
) -> int:
    if record.source_url:
        return _upsert_by_source_url(record, db_path)
    if record.source_video_id:
        return _upsert_by_source_video_id(record, db_path)
    if record.local_filename and record.schedule_time:
        return _upsert_by_local_schedule(record, db_path)
    return insert_video_record(record, db_path)


def _upsert_by_source_url(
    record: VideoRecord,
    db_path: str | Path = DEFAULT_DB_PATH,
) -> int:
    init_db(db_path)
    data = asdict(record)
    values = {column: data.get(column) or "" for column in UPSERT_COLUMNS}
    values = _drop_conflicting_source_video_id(values, db_path)

    placeholders = ", ".join(":" + column for column in UPSERT_COLUMNS)
    columns_sql = ", ".join(UPSERT_COLUMNS)
    update_sql = ", ".join(
        f"{column}=excluded.{column}"
        for column in UPSERT_COLUMNS
        if column != "source_url"
    )

    sql = f"""
    INSERT INTO videos ({columns_sql})
    VALUES ({placeholders})
    ON CONFLICT(source_url) WHERE source_url != ''
    DO UPDATE SET {update_sql}
    """

    with get_connection(db_path) as conn:
        cur = conn.execute(sql, values)
        conn.commit()

        if cur.lastrowid:
            return int(cur.lastrowid)

        existing = conn.execute(
            "SELECT id FROM videos WHERE source_url = ?",
            (values["source_url"],),
        ).fetchone()
        return int(existing["id"]) if existing else 0


def _upsert_by_source_video_id(
    record: VideoRecord,
    db_path: str | Path = DEFAULT_DB_PATH,
) -> int:
    init_db(db_path)
    data = asdict(record)
    values = {column: data.get(column) or "" for column in UPSERT_COLUMNS}

    placeholders = ", ".join(":" + column for column in UPSERT_COLUMNS)
    columns_sql = ", ".join(UPSERT_COLUMNS)
    update_sql = ", ".join(
        f"{column}=excluded.{column}"
        for column in UPSERT_COLUMNS
        if column != "source_video_id"
    )

    sql = f"""
    INSERT INTO videos ({columns_sql})
    VALUES ({placeholders})
    ON CONFLICT(source_video_id) WHERE source_video_id != ''
    DO UPDATE SET {update_sql}
    """

    with get_connection(db_path) as conn:
        cur = conn.execute(sql, values)
        conn.commit()

        if cur.lastrowid:
            return int(cur.lastrowid)

        existing = conn.execute(
            "SELECT id FROM videos WHERE source_video_id = ?",
            (values["source_video_id"],),
        ).fetchone()
        return int(existing["id"]) if existing else 0


def _upsert_by_local_schedule(
    record: VideoRecord,
    db_path: str | Path = DEFAULT_DB_PATH,
) -> int:
    init_db(db_path)
    data = asdict(record)
    values = {column: data.get(column) or "" for column in UPSERT_COLUMNS}

    placeholders = ", ".join(":" + column for column in UPSERT_COLUMNS)
    columns_sql = ", ".join(UPSERT_COLUMNS)
    update_sql = ", ".join(
        f"{column}=excluded.{column}"
        for column in UPSERT_COLUMNS
        if column not in {"local_filename", "schedule_time"}
    )

    sql = f"""
    INSERT INTO videos ({columns_sql})
    VALUES ({placeholders})
    ON CONFLICT(local_filename, schedule_time)
    WHERE source_url = '' AND local_filename != '' AND schedule_time != ''
    DO UPDATE SET {update_sql}
    """

    with get_connection(db_path) as conn:
        cur = conn.execute(sql, values)
        conn.commit()

        if cur.lastrowid:
            return int(cur.lastrowid)

        existing = conn.execute(
            """
            SELECT id FROM videos
            WHERE source_url = '' AND local_filename = ? AND schedule_time = ?
            """,
            (values["local_filename"], values["schedule_time"]),
        ).fetchone()
        return int(existing["id"]) if existing else 0


def insert_video_record(
    record: VideoRecord,
    db_path: str | Path = DEFAULT_DB_PATH,
) -> int:
    init_db(db_path)
    data = asdict(record)
    values = {column: data.get(column) or "" for column in UPSERT_COLUMNS}

    columns_sql = ", ".join(UPSERT_COLUMNS)
    placeholders = ", ".join(":" + column for column in UPSERT_COLUMNS)

    with get_connection(db_path) as conn:
        cur = conn.execute(
            f"INSERT INTO videos ({columns_sql}) VALUES ({placeholders})",
            values,
        )
        conn.commit()
        return int(cur.lastrowid)


def save_video_record(
    record: VideoRecord,
    db_path: str | Path = DEFAULT_DB_PATH,
) -> int:
    return upsert_video_record(record, db_path)


def list_video_records(
    db_path: str | Path = DEFAULT_DB_PATH,
    limit: int = 50,
) -> list[sqlite3.Row]:
    init_db(db_path)
    with get_connection(db_path) as conn:
        return list(
            conn.execute(
                "SELECT * FROM videos ORDER BY id DESC LIMIT ?",
                (limit,),
            )
        )


def find_video_by_source(
    source_url: str = "",
    source_video_id: str = "",
    db_path: str | Path = DEFAULT_DB_PATH,
) -> sqlite3.Row | None:
    init_db(db_path)
    with get_connection(db_path) as conn:
        if source_url:
            row = conn.execute(
                "SELECT * FROM videos WHERE source_url = ? LIMIT 1",
                (source_url,),
            ).fetchone()
            if row:
                return row

        if source_video_id:
            return conn.execute(
                "SELECT * FROM videos WHERE source_video_id = ? LIMIT 1",
                (source_video_id,),
            ).fetchone()

    return None


def _drop_conflicting_source_video_id(values: dict, db_path: str | Path) -> dict:
    source_url = values.get("source_url", "")
    source_video_id = values.get("source_video_id", "")
    if not source_url or not source_video_id:
        return values

    with get_connection(db_path) as conn:
        existing = conn.execute(
            """
            SELECT id, source_url FROM videos
            WHERE source_video_id = ?
            LIMIT 1
            """,
            (source_video_id,),
        ).fetchone()
    if existing and existing["source_url"] != source_url:
        values = dict(values)
        values["source_video_id"] = ""
    return values
