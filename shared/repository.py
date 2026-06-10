from pipeline_core.repository import (
    find_video_by_source,
    list_video_records,
    save_video_record,
    upsert_video_record,
)
from shared.database import DEFAULT_DB_PATH, get_connection, init_db

__all__ = [
    "find_video_by_source",
    "list_video_records",
    "save_video_record",
    "upsert_video_record",
    "list_download_pending",
    "list_publish_pending",
    "list_recent_videos",
    "list_videos_for_view",
    "get_pipeline_stats",
    "delete_videos_for_view",
    "delete_video_ids",
    "update_video_fields",
]


def list_download_pending(db_path=DEFAULT_DB_PATH, limit: int = 50):
    init_db(db_path)
    with get_connection(db_path) as conn:
        return list(
            conn.execute(
                """
                SELECT * FROM videos
                WHERE source_url != ''
                  AND COALESCE(local_filename, '') = ''
                  AND download_status IN ('discovered', 'pending', 'download_failed')
                ORDER BY created_at ASC, id ASC
                LIMIT ?
                """,
                (limit,),
            )
        )


def list_publish_pending(db_path=DEFAULT_DB_PATH, limit: int = 50):
    init_db(db_path)
    with get_connection(db_path) as conn:
        return list(
            conn.execute(
                """
                SELECT * FROM videos
                WHERE COALESCE(local_filename, '') != ''
                  AND COALESCE(schedule_time, '') != ''
                  AND publish_status IN ('pending', 'post_failed')
                ORDER BY schedule_time ASC, id ASC
                LIMIT ?
                """,
                (limit,),
            )
        )


def list_recent_videos(db_path=DEFAULT_DB_PATH, limit: int = 100):
    init_db(db_path)
    with get_connection(db_path) as conn:
        return list(
            conn.execute(
                "SELECT * FROM videos ORDER BY id DESC LIMIT ?",
                (limit,),
            )
        )


def list_videos_for_view(db_path=DEFAULT_DB_PATH, view: str = "action", limit: int = 100):
    init_db(db_path)
    where_sql = _view_where_sql(view)
    sql = f"SELECT * FROM videos {where_sql} ORDER BY id DESC LIMIT ?"
    with get_connection(db_path) as conn:
        return list(conn.execute(sql, (limit,)))


def get_pipeline_stats(db_path=DEFAULT_DB_PATH) -> dict:
    init_db(db_path)
    with get_connection(db_path) as conn:
        total = conn.execute("SELECT COUNT(*) AS count FROM videos").fetchone()["count"]
        download = {
            row["download_status"]: row["count"]
            for row in conn.execute(
                """
                SELECT download_status, COUNT(*) AS count
                FROM videos
                GROUP BY download_status
                """
            )
        }
        publish = {
            row["publish_status"]: row["count"]
            for row in conn.execute(
                """
                SELECT publish_status, COUNT(*) AS count
                FROM videos
                GROUP BY publish_status
                """
            )
        }
        pending_download = conn.execute(
            """
            SELECT COUNT(*) AS count FROM videos
            WHERE source_url != ''
              AND COALESCE(local_filename, '') = ''
              AND download_status IN ('discovered', 'pending', 'download_failed')
            """
        ).fetchone()["count"]
        pending_publish = conn.execute(
            """
            SELECT COUNT(*) AS count FROM videos
            WHERE COALESCE(local_filename, '') != ''
              AND COALESCE(schedule_time, '') != ''
              AND publish_status IN ('pending', 'post_failed')
            """
        ).fetchone()["count"]

    return {
        "total": total,
        "download": download,
        "publish": publish,
        "pending_download": pending_download,
        "pending_publish": pending_publish,
    }


def delete_videos_for_view(db_path=DEFAULT_DB_PATH, view: str = "mock") -> int:
    init_db(db_path)
    where_sql = _view_where_sql(view)
    if view not in {"mock", "empty_source", "failed", "all"}:
        raise ValueError("Unsupported delete view.")
    with get_connection(db_path) as conn:
        count = conn.execute(f"SELECT COUNT(*) AS count FROM videos {where_sql}").fetchone()["count"]
        conn.execute(f"DELETE FROM videos {where_sql}")
        conn.commit()
        return int(count)


def delete_video_ids(video_ids: list[int], db_path=DEFAULT_DB_PATH) -> int:
    ids = [int(video_id) for video_id in video_ids if str(video_id).strip()]
    if not ids:
        return 0

    init_db(db_path)
    placeholders = ", ".join("?" for _ in ids)
    with get_connection(db_path) as conn:
        count = conn.execute(
            f"SELECT COUNT(*) AS count FROM videos WHERE id IN ({placeholders})",
            ids,
        ).fetchone()["count"]
        conn.execute(f"DELETE FROM videos WHERE id IN ({placeholders})", ids)
        conn.commit()
        return int(count)


def update_video_fields(video_id: int, fields: dict, db_path=DEFAULT_DB_PATH) -> None:
    if not fields:
        return

    init_db(db_path)
    allowed = {
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
    }
    clean = {key: value for key, value in fields.items() if key in allowed}
    if not clean:
        return

    assignments = ", ".join(f"{key} = :{key}" for key in clean)
    params = {**clean, "id": video_id}
    with get_connection(db_path) as conn:
        conn.execute(f"UPDATE videos SET {assignments} WHERE id = :id", params)
        conn.commit()


def _view_where_sql(view: str) -> str:
    if view == "all":
        return ""
    if view == "action":
        return """
        WHERE (
            source_url != ''
            AND COALESCE(local_filename, '') = ''
            AND download_status IN ('discovered', 'pending', 'download_failed')
        ) OR (
            COALESCE(local_filename, '') != ''
            AND publish_status IN ('pending', 'post_failed')
        )
        """
    if view == "need_download":
        return """
        WHERE source_url != ''
          AND COALESCE(local_filename, '') = ''
          AND download_status IN ('discovered', 'pending', 'download_failed')
        """
    if view == "downloaded":
        return "WHERE download_status = 'downloaded'"
    if view == "need_publish":
        return """
        WHERE COALESCE(local_filename, '') != ''
          AND COALESCE(schedule_time, '') != ''
          AND publish_status IN ('pending', 'post_failed')
        """
    if view == "failed":
        return "WHERE download_status LIKE '%failed%' OR publish_status LIKE '%failed%'"
    if view == "mock":
        return "WHERE source_video_id LIKE 'mock_%' OR source_url LIKE '%/example.page/%'"
    if view == "empty_source":
        return "WHERE COALESCE(source_url, '') = ''"
    return _view_where_sql("action")
