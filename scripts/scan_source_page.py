import argparse
import logging
import sys
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parent.parent
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from pipeline_core.db import DEFAULT_DB_PATH, init_db
from pipeline_core.scanner import (
    MockFanpageScanner,
    ScanInput,
    parse_scan_date,
    scan_and_store,
)


def main() -> None:
    parser = argparse.ArgumentParser(description="Scan source fanpage videos into the pipeline database.")
    parser.add_argument("--source-page-url", required=True, help="Source Facebook page URL.")
    parser.add_argument("--date-from", required=True, help="Start date, format YYYY-MM-DD.")
    parser.add_argument("--date-to", required=True, help="End date, format YYYY-MM-DD.")
    parser.add_argument("--limit", type=int, default=None, help="Optional maximum number of videos.")
    parser.add_argument("--db", default=str(DEFAULT_DB_PATH), help="SQLite database path.")
    parser.add_argument(
        "--scanner",
        default="mock",
        choices=["mock"],
        help="Scanner backend. Only mock is available until Facebook API permissions are configured.",
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s - %(message)s",
    )

    init_db(args.db)
    scan_input = ScanInput(
        source_page_url=args.source_page_url.strip(),
        date_from=parse_scan_date(args.date_from),
        date_to=parse_scan_date(args.date_to),
        limit=args.limit,
    )

    scanner = MockFanpageScanner()
    summary = scan_and_store(scanner, scan_input, db_path=args.db)
    print(
        "Scan complete: "
        f"scanned={summary.scanned}, "
        f"discovered={summary.discovered}, "
        f"skipped_existing={summary.skipped_existing}"
    )


if __name__ == "__main__":
    main()
