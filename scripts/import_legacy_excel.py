import argparse
import sys
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parent.parent
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from pipeline_core.db import DEFAULT_DB_PATH, init_db
from pipeline_core.importers import read_legacy_rows, rows_to_video_records
from pipeline_core.repository import save_video_record


def main() -> None:
    parser = argparse.ArgumentParser(description="Import legacy Excel/CSV rows into the pipeline database.")
    parser.add_argument("input_file", help="Legacy .xlsx or .csv file.")
    parser.add_argument("--db", default=str(DEFAULT_DB_PATH), help="SQLite database path.")
    args = parser.parse_args()

    input_path = Path(args.input_file)
    if not input_path.exists():
        raise FileNotFoundError(f"Input file not found: {input_path}")

    init_db(args.db)
    rows = read_legacy_rows(input_path.read_bytes(), input_path.name)
    records = rows_to_video_records(rows)

    imported = 0
    for record in records:
        save_video_record(record, args.db)
        imported += 1

    print(f"Imported {imported} video records into {args.db}")


if __name__ == "__main__":
    main()
