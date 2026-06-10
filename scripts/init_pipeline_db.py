import argparse
import sys
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parent.parent
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from pipeline_core.db import DEFAULT_DB_PATH, init_db


def main() -> None:
    parser = argparse.ArgumentParser(description="Initialize the pipeline SQLite database.")
    parser.add_argument("--db", default=str(DEFAULT_DB_PATH), help="SQLite database path.")
    args = parser.parse_args()

    db_path = init_db(args.db)
    print(f"Initialized pipeline database: {db_path}")


if __name__ == "__main__":
    main()
