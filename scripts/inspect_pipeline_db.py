import argparse
import json
import sys
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parent.parent
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from pipeline_core.db import DEFAULT_DB_PATH
from pipeline_core.repository import list_video_records


def main() -> None:
    parser = argparse.ArgumentParser(description="Print recent pipeline database rows.")
    parser.add_argument("--db", default=str(DEFAULT_DB_PATH), help="SQLite database path.")
    parser.add_argument("--limit", type=int, default=10, help="Number of rows to show.")
    args = parser.parse_args()

    rows = list_video_records(args.db, args.limit)
    for row in rows:
        print(json.dumps(dict(row), ensure_ascii=True))


if __name__ == "__main__":
    main()
