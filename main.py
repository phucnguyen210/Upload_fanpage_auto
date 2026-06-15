import argparse
import logging
from pathlib import Path

from shared.asr import transcribe_media_openai
from shared.database import DEFAULT_DB_PATH, init_db
from shared.downloader import download_pending
from shared.douyin_bridge import DEFAULT_DOUYIN_DB_PATH, sync_douyin_finals
from shared.importers import read_legacy_rows, rows_to_video_records
from shared.metadata_enricher import enrich_titles
from shared.publisher import publish_pending
from shared.repository import save_video_record
from shared.scheduler import generate_schedule
from pipeline_core.scanner import (
    MockFanpageScanner,
    PlaywrightFacebookScanner,
    ScanInput,
    YtDlpFacebookScanner,
    parse_scan_date,
    scan_and_store,
)


def main() -> None:
    parser = argparse.ArgumentParser(description="Unified video pipeline CLI.")
    parser.add_argument("--db", default=str(DEFAULT_DB_PATH), help="SQLite database path.")
    parser.add_argument("--log-level", default="INFO", help="Logging level.")

    subparsers = parser.add_subparsers(dest="command", required=True)

    import_parser = subparsers.add_parser("import-excel", help="Import legacy Excel/CSV rows into the shared database.")
    import_parser.add_argument("input_file", help="Input .xlsx or .csv file.")

    scan_parser = subparsers.add_parser("scan-source", help="Scan a Facebook profile/page for video metadata.")
    scan_parser.add_argument("--source-page-url", required=True, help="Source Facebook profile/page URL.")
    scan_parser.add_argument("--date-from", required=True, help="Format: YYYY-MM-DD.")
    scan_parser.add_argument("--date-to", required=True, help="Format: YYYY-MM-DD.")
    scan_parser.add_argument("--limit", type=int, default=None)
    scan_parser.add_argument("--browser", default="chrome")
    scan_parser.add_argument("--scanner", choices=["browser", "yt-dlp", "mock"], default="browser")

    download_parser = subparsers.add_parser("download-pending", help="Download discovered/pending videos from the shared database.")
    download_parser.add_argument("--limit", type=int, default=10)
    download_parser.add_argument("--browser", default="chrome")
    download_parser.add_argument("--output-dir", default="data/downloads")
    download_parser.add_argument("--dry-run", action="store_true")
    sync_parser = subparsers.add_parser("sync-douyin-finals", help="Import completed Douyin final videos into the publish database.")
    sync_parser.add_argument("--douyin-db", default=str(DEFAULT_DOUYIN_DB_PATH))
    sync_parser.add_argument("--limit", type=int, default=100)
    sync_parser.add_argument("--dry-run", action="store_true")

    title_parser = subparsers.add_parser("enrich-titles", help="Fix missing/bad titles from video metadata.")
    title_parser.add_argument("--limit", type=int, default=20)
    title_parser.add_argument("--browser", default="chrome")
    title_parser.add_argument("--dry-run", action="store_true")

    transcribe_parser = subparsers.add_parser("transcribe", help="Transcribe a media file safely by splitting ASR chunks under 25 MiB.")
    transcribe_parser.add_argument("input_file", help="Input .mp4/.mp3/etc.")
    transcribe_parser.add_argument("--model", default="gpt-4o-mini-transcribe")
    transcribe_parser.add_argument("--language", default="")
    transcribe_parser.add_argument("--prompt", default="")
    transcribe_parser.add_argument("--output-file", default="")
    transcribe_parser.add_argument("--chunks-dir", default="data/asr_chunks")
    transcribe_parser.add_argument("--segment-seconds", type=int, default=600)
    transcribe_parser.add_argument("--keep-chunks", action="store_true")
    transcribe_parser.add_argument("--dry-run", action="store_true")

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
    elif args.command == "scan-source":
        result = command_scan_source(args, args.db)
    elif args.command == "download-pending":
        result = download_pending(
            db_path=args.db,
            output_dir=args.output_dir,
            browser=args.browser,
            limit=args.limit,
            dry_run=args.dry_run,
        )
    elif args.command == "sync-douyin-finals":
        result = sync_douyin_finals(
            douyin_db_path=args.douyin_db,
            pipeline_db_path=args.db,
            limit=args.limit,
            dry_run=args.dry_run,
        )
    elif args.command == "enrich-titles":
        result = enrich_titles(
            db_path=args.db,
            browser=args.browser,
            limit=args.limit,
            dry_run=args.dry_run,
        )
    elif args.command == "transcribe":
        result = transcribe_media_openai(
            input_path=args.input_file,
            model=args.model,
            language=args.language,
            prompt=args.prompt,
            output_file=args.output_file,
            chunks_dir=args.chunks_dir,
            segment_seconds=args.segment_seconds,
            keep_chunks=args.keep_chunks,
            dry_run=args.dry_run,
            progress_cb=lambda event: logging.info("%s - %s", event.get("event"), event.get("message")),
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


def command_scan_source(args, db_path: str) -> dict:
    scan_input = ScanInput(
        source_page_url=args.source_page_url.strip(),
        date_from=parse_scan_date(args.date_from),
        date_to=parse_scan_date(args.date_to),
        limit=args.limit,
    )
    if args.scanner == "mock":
        scanner = MockFanpageScanner()
    elif args.scanner == "browser":
        scanner = PlaywrightFacebookScanner()
    else:
        scanner = YtDlpFacebookScanner(browser=args.browser)

    summary = scan_and_store(scanner, scan_input, db_path=db_path)
    return {
        "source_page_url": scan_input.source_page_url,
        "date_from": scan_input.date_from.isoformat(),
        "date_to": scan_input.date_to.isoformat(),
        "limit": scan_input.limit,
        "scanner": args.scanner,
        "scanned": summary.scanned,
        "discovered": summary.discovered,
        "skipped_existing": summary.skipped_existing,
    }


if __name__ == "__main__":
    main()
