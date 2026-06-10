import sqlite3
from pathlib import Path


BASE_DIR = Path(__file__).resolve().parent.parent
DEFAULT_DB_PATH = BASE_DIR / "data" / "pipeline.sqlite3"


SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS videos (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source_url TEXT NOT NULL DEFAULT '',
    source_video_id TEXT NOT NULL DEFAULT '',
    source_page_url TEXT NOT NULL DEFAULT '',
    title_original TEXT NOT NULL DEFAULT '',
    title_rewrite TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL DEFAULT '',
    local_filename TEXT NOT NULL DEFAULT '',
    download_status TEXT NOT NULL DEFAULT 'pending',
    publish_status TEXT NOT NULL DEFAULT 'pending',
    schedule_time TEXT NOT NULL DEFAULT '',
    fb_post_id TEXT NOT NULL DEFAULT '',
    error_message TEXT NOT NULL DEFAULT '',
    inserted_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_videos_source_url
ON videos(source_url)
WHERE source_url != '';

CREATE UNIQUE INDEX IF NOT EXISTS idx_videos_local_schedule
ON videos(local_filename, schedule_time)
WHERE source_url = '' AND local_filename != '' AND schedule_time != '';

CREATE INDEX IF NOT EXISTS idx_videos_download_status
ON videos(download_status);

CREATE INDEX IF NOT EXISTS idx_videos_publish_status
ON videos(publish_status);

CREATE INDEX IF NOT EXISTS idx_videos_schedule_time
ON videos(schedule_time);

CREATE TRIGGER IF NOT EXISTS trg_videos_updated_at
AFTER UPDATE ON videos
FOR EACH ROW
BEGIN
    UPDATE videos SET updated_at = datetime('now') WHERE id = OLD.id;
END;
"""

REQUIRED_COLUMNS = {
    "source_video_id": "TEXT NOT NULL DEFAULT ''",
}

POST_MIGRATION_SQL = """
CREATE UNIQUE INDEX IF NOT EXISTS idx_videos_source_video_id
ON videos(source_video_id)
WHERE source_video_id != '';
"""


def get_connection(db_path: str | Path = DEFAULT_DB_PATH) -> sqlite3.Connection:
    path = Path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    return conn


def init_db(db_path: str | Path = DEFAULT_DB_PATH) -> Path:
    path = Path(db_path)
    with get_connection(path) as conn:
        conn.executescript(SCHEMA_SQL)
        _migrate_columns(conn)
        conn.executescript(POST_MIGRATION_SQL)
        conn.commit()
    return path


def _migrate_columns(conn: sqlite3.Connection) -> None:
    existing = {
        row["name"]
        for row in conn.execute("PRAGMA table_info(videos)").fetchall()
    }
    for column, definition in REQUIRED_COLUMNS.items():
        if column not in existing:
            conn.execute(f"ALTER TABLE videos ADD COLUMN {column} {definition}")
