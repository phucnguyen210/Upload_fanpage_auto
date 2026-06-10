import argparse
import logging
from pathlib import Path

from shared.database import DEFAULT_DB_PATH, init_db
from shared.downloader import download_pending
from shared.importers import read_legacy_rows, rows_to_video_records
from shared.publisher import publish_pending
from shared.repository import save_video_record
from shared.scheduler import generate_schedule


def main() -> None:
    parser = argparse.ArgumentParser(description="Unified video pipeline CLI.")
    parser.add_argument("--db", default=str(DEFAULT_DB_PATH), help="SQLite database path.")
    parser.add_argument("--log-level", default="INFO", help="Logging level.")

    subparsers = parser.add_subparsers(dest="command", required=True)

    import_parser = subparsers.add_parser("import-excel", help="Import legacy Excel/CSV rows into the shared database.")
    import_parser.add_argument("input_file", help="Input .xlsx or .csv file.")

    download_parser = subparsers.add_parser("download-pending", help="Download discovered/pending videos from the shared database.")
    download_parser.add_argument("--limit", type=int, default=10)
    download_parser.add_argument("--browser", default="chrome")
    download_parser.add_argument("--output-dir", default="data/downloads")
    download_parser.add_argument("--dry-run", action="store_true")

    schedule_parser = subparsers.add_parser("generate-schedule", help="Generate schedule_time for downloaded videos.")
    schedule_parser.add_argument("--start-time", default="", help="Format: YYYY-MM-DD HH:MM:SS. Defaults to one hour from now.")
    schedule_parser.add_argument("--interval-minutes", type=int, default=60)
    schedule_parser.add_argument("--limit", type=int, default=50)
    schedule_parser.add_argument("--dry-run", action="store_true")

    publish_parser = subparsers.add_parser("publish-pending", help="Publish/schedule videos from the shared database.")
    publish_parser.add_argument("--limit", type=int, default=10)
    publish_parser.add_argument("--page-id", default="")
    publish_parser.add_argument("--page-access-token", default="")
    publish_parser.add_argument("--dry-run", action="store_true")

    args = parser.parse_args()
    logging.basicConfig(
        level=getattr(logging, args.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s - %(message)s",
    )
    init_db(args.db)

    if args.command == "import-excel":
        result = command_import_excel(args.input_file, args.db)
    elif args.command == "download-pending":
        result = download_pending(
            db_path=args.db,
            output_dir=args.output_dir,
            browser=args.browser,
            limit=args.limit,
            dry_run=args.dry_run,
        )
    elif args.command == "generate-schedule":
        result = generate_schedule(
            db_path=args.db,
            start_time=args.start_time,
            interval_minutes=args.interval_minutes,
            limit=args.limit,
            dry_run=args.dry_run,
        )
    elif args.command == "publish-pending":
        result = publish_pending(
            db_path=args.db,
            page_id=args.page_id,
            page_access_token=args.page_access_token,
            limit=args.limit,
            dry_run=args.dry_run,
        )
    else:
        parser.error(f"Unsupported command: {args.command}")
        return

    print(result)


def command_import_excel(input_file: str, db_path: str) -> dict:
    input_path = Path(input_file)
    if not input_path.exists():
        raise FileNotFoundError(f"Input file not found: {input_path}")

    rows = read_legacy_rows(input_path.read_bytes(), input_path.name)
    records = rows_to_video_records(rows)
    imported = 0
    for record in records:
        save_video_record(record, db_path=db_path)
        imported += 1
    return {"input_file": str(input_path), "imported": imported}


if __name__ == "__main__":
    main()
