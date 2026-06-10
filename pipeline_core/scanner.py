import hashlib
import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Iterable

from .db import DEFAULT_DB_PATH
from .models import VideoRecord
from .repository import find_video_by_source, insert_video_record

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class ScanInput:
    source_page_url: str
    date_from: date
    date_to: date
    limit: int | None = None


@dataclass(slots=True)
class ScanSummary:
    discovered: int
    skipped_existing: int
    scanned: int


class VideoScanner(ABC):
    @abstractmethod
    def scan(self, scan_input: ScanInput) -> Iterable[VideoRecord]:
        """Return metadata-only video records from a source page."""


class MockFanpageScanner(VideoScanner):
    """Deterministic scanner used until a real Facebook API scanner is wired in."""

    def scan(self, scan_input: ScanInput) -> Iterable[VideoRecord]:
        _validate_scan_input(scan_input)
        logger.info(
            "Mock scanning page=%s date_from=%s date_to=%s limit=%s",
            scan_input.source_page_url,
            scan_input.date_from.isoformat(),
            scan_input.date_to.isoformat(),
            scan_input.limit,
        )

        count = 0
        current = scan_input.date_from
        while current <= scan_input.date_to:
            if scan_input.limit is not None and count >= scan_input.limit:
                break

            source_video_id = _mock_video_id(scan_input.source_page_url, current, count + 1)
            source_url = f"{scan_input.source_page_url.rstrip('/')}/videos/{source_video_id}/"
            yield VideoRecord(
                source_url=source_url,
                source_video_id=source_video_id,
                source_page_url=scan_input.source_page_url,
                title_original=f"Mock video discovered on {current.isoformat()}",
                created_at=f"{current.isoformat()} 00:00:00",
                download_status="discovered",
                publish_status="pending",
            )

            count += 1
            current += timedelta(days=1)


def scan_and_store(
    scanner: VideoScanner,
    scan_input: ScanInput,
    db_path: str | Path = DEFAULT_DB_PATH,
) -> ScanSummary:
    _validate_scan_input(scan_input)
    discovered = 0
    skipped_existing = 0
    scanned = 0

    logger.info(
        "Starting scan page=%s date_from=%s date_to=%s limit=%s",
        scan_input.source_page_url,
        scan_input.date_from.isoformat(),
        scan_input.date_to.isoformat(),
        scan_input.limit,
    )

    for record in scanner.scan(scan_input):
        scanned += 1
        record.download_status = record.download_status or "discovered"
        record.publish_status = record.publish_status or "pending"

        existing = find_video_by_source(
            source_url=record.source_url,
            source_video_id=record.source_video_id,
            db_path=db_path,
        )
        if existing:
            skipped_existing += 1
            logger.info(
                "Skipping existing video source_url=%s source_video_id=%s existing_id=%s",
                record.source_url,
                record.source_video_id,
                existing["id"],
            )
            continue

        new_id = insert_video_record(record, db_path=db_path)
        discovered += 1
        logger.info(
            "Discovered video id=%s source_url=%s source_video_id=%s created_at=%s",
            new_id,
            record.source_url,
            record.source_video_id,
            record.created_at,
        )

    summary = ScanSummary(
        discovered=discovered,
        skipped_existing=skipped_existing,
        scanned=scanned,
    )
    logger.info(
        "Finished scan scanned=%s discovered=%s skipped_existing=%s",
        summary.scanned,
        summary.discovered,
        summary.skipped_existing,
    )
    return summary


def parse_scan_date(value: str) -> date:
    return datetime.strptime(value, "%Y-%m-%d").date()


def _validate_scan_input(scan_input: ScanInput) -> None:
    if not scan_input.source_page_url.strip().startswith(("http://", "https://")):
        raise ValueError("source_page_url must be an http/https URL.")
    if scan_input.date_from > scan_input.date_to:
        raise ValueError("date_from must be earlier than or equal to date_to.")
    if scan_input.limit is not None and scan_input.limit <= 0:
        raise ValueError("limit must be greater than 0 when provided.")


def _mock_video_id(source_page_url: str, created_date: date, index: int) -> str:
    raw = f"{source_page_url}|{created_date.isoformat()}|{index}"
    digest = hashlib.sha1(raw.encode("utf-8")).hexdigest()[:12]
    return f"mock_{digest}"
