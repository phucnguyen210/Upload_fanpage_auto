import hashlib
import logging
import re
from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Iterable
from urllib.parse import parse_qs, urlencode, urlparse, urlunparse

from .db import DEFAULT_DB_PATH
from .models import VideoRecord
from .repository import find_video_by_source, insert_video_record, update_video_fields

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


class YtDlpFacebookScanner(VideoScanner):
    """Metadata scanner backed by yt-dlp.

    This does not download videos. It asks yt-dlp to extract playlist/profile/page
    entries, then stores only records matching the requested date range.
    """

    def __init__(self, browser: str = "chrome", progress_cb=None):
        self.browser = browser
        self.progress_cb = progress_cb

    def scan(self, scan_input: ScanInput) -> Iterable[VideoRecord]:
        _validate_scan_input(scan_input)

        try:
            import yt_dlp
        except ImportError as exc:
            raise RuntimeError("yt-dlp is not installed. Run: pip install yt-dlp") from exc

        ydl_opts = {
            "extract_flat": "in_playlist",
            "ignoreerrors": True,
            "quiet": True,
            "no_warnings": True,
            "skip_download": True,
        }
        if self.browser:
            ydl_opts["cookiesfrombrowser"] = (self.browser,)

        emitted = 0
        seen: set[str] = set()
        source_urls = _candidate_source_urls(scan_input.source_page_url)
        self._emit(
            "scan_start",
            f"Scanning {len(source_urls)} source URL(s) with yt-dlp. Browser cookie={self.browser or 'none'}.",
            sources=source_urls,
        )

        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            for source_url in source_urls:
                self._emit("scan_source", f"Extracting metadata from: {source_url}", source_url=source_url)
                try:
                    info = ydl.extract_info(source_url, download=False)
                except Exception as exc:
                    logger.exception("yt-dlp scan failed for source_url=%s", source_url)
                    self._emit("scan_error", f"yt-dlp failed for {source_url}: {exc}", source_url=source_url)
                    continue

                entries = list(_flatten_entries(info))
                self._emit("scan_entries", f"yt-dlp returned {len(entries)} candidate entries.", count=len(entries))

                for entry in entries:
                    record = _entry_to_video_record(entry, scan_input.source_page_url)
                    if not record.source_url and not record.source_video_id:
                        continue

                    dedupe_key = record.source_url or record.source_video_id
                    if dedupe_key in seen:
                        continue
                    seen.add(dedupe_key)

                    created_date = _record_created_date(record)
                    if created_date is None:
                        self._emit(
                            "item_skip",
                            f"Skipped undated video: {record.title_original or record.source_url}",
                            source_url=record.source_url,
                        )
                        continue
                    if created_date < scan_input.date_from or created_date > scan_input.date_to:
                        continue

                    record.download_status = "discovered"
                    record.publish_status = "pending"
                    emitted += 1
                    self._emit(
                        "item_found",
                        f"Found video {emitted}: {record.title_original or record.source_url}",
                        source_url=record.source_url,
                        created_at=record.created_at,
                        source_video_id=record.source_video_id,
                    )
                    yield record

                    if scan_input.limit is not None and emitted >= scan_input.limit:
                        self._emit("scan_limit", f"Reached scan limit: {scan_input.limit}", limit=scan_input.limit)
                        return

        if emitted == 0:
            self._emit(
                "scan_warning",
                "No videos were discovered. Facebook may be hiding profile videos from yt-dlp, "
                "the selected browser may not be logged in, or the profile URL may not expose a video list.",
            )

    def _emit(self, event: str, message: str, **payload) -> None:
        logger.info("%s %s", event, message)
        if self.progress_cb:
            self.progress_cb({"event": event, "message": message, **payload})


class PlaywrightFacebookScanner(VideoScanner):
    """Browser-driven Facebook scanner.

    Facebook profile/page video lists are rendered dynamically, so yt-dlp often
    returns zero entries. This scanner opens a real browser, scrolls the profile
    videos/reels pages, and extracts visible Facebook video links from the DOM.
    """

    def __init__(
        self,
        browser: str = "chromium",
        user_data_dir: str | Path = "data/browser_profile",
        max_scrolls: int = 20,
        progress_cb=None,
    ):
        self.browser = browser
        self.user_data_dir = Path(user_data_dir)
        self.max_scrolls = max_scrolls
        self.progress_cb = progress_cb

    def scan(self, scan_input: ScanInput) -> Iterable[VideoRecord]:
        _validate_scan_input(scan_input)
        try:
            from playwright.sync_api import Error as PlaywrightError
            from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
            from playwright.sync_api import sync_playwright
        except ImportError as exc:
            raise RuntimeError(
                "Playwright is not installed. Run: pip install playwright && python -m playwright install chromium"
            ) from exc

        emitted = 0
        seen: set[str] = set()
        source_urls = _candidate_source_urls(scan_input.source_page_url)
        scrolls = self.max_scrolls
        if scan_input.limit:
            scrolls = max(8, min(60, scan_input.limit * 2))

        self._emit(
            "scan_start",
            f"Opening browser scanner. It will try {len(source_urls)} source URL(s) and scroll up to {scrolls} times each.",
            sources=source_urls,
        )
        self._emit(
            "scan_note",
            "If Facebook asks for login, log in inside the opened browser window, then run scan again.",
        )

        self.user_data_dir.mkdir(parents=True, exist_ok=True)
        with sync_playwright() as p:
            browser_type = getattr(p, "chromium")
            context = browser_type.launch_persistent_context(
                str(self.user_data_dir),
                headless=False,
                viewport={"width": 1366, "height": 900},
                locale="vi-VN",
            )
            page = context.new_page()
            try:
                for source_url in source_urls:
                    self._emit("scan_source", f"Opening Facebook page: {source_url}", source_url=source_url)
                    try:
                        page.goto(source_url, wait_until="domcontentloaded", timeout=60000)
                    except PlaywrightTimeoutError:
                        self._emit("scan_warning", f"Timed out while opening {source_url}. Continuing with visible content.")

                    page.wait_for_timeout(2500)
                    before_count = len(seen)
                    for scroll_index in range(1, scrolls + 1):
                        try:
                            candidates = _extract_playwright_candidates(page)
                        except PlaywrightError as exc:
                            self._emit(
                                "scan_warning",
                                f"Facebook navigated/reloaded while reading links. Waiting and retrying once: {exc}",
                            )
                            try:
                                page.wait_for_load_state("domcontentloaded", timeout=10000)
                                page.wait_for_timeout(1500)
                                candidates = _extract_playwright_candidates(page)
                            except PlaywrightError as retry_exc:
                                self._emit(
                                    "scan_warning",
                                    f"Could not read links after navigation. Moving to next source URL: {retry_exc}",
                                )
                                break
                        for item in candidates:
                            record = _candidate_to_video_record(item, scan_input.source_page_url)
                            if not record.source_url:
                                continue
                            self._emit(
                                "scan_debug",
                                f"Title debug video_id={record.source_video_id}: {item.get('titleReason', '')}",
                                video_id=record.source_video_id,
                                source_url=record.source_url,
                                raw_title_candidates=item.get("rawTitleCandidates", []),
                                selected_title=record.title_original,
                                reason=item.get("titleReason", ""),
                            )
                            key = _canonical_video_key(record.source_url) or record.source_url
                            if key in seen:
                                continue
                            seen.add(key)

                            created_date = _record_created_date(record)
                            if created_date and (
                                created_date < scan_input.date_from or created_date > scan_input.date_to
                            ):
                                continue
                            if not created_date:
                                self._emit(
                                    "item_step",
                                    f"Found video with unknown date; keeping it for manual review: {record.source_url}",
                                    source_url=record.source_url,
                                )

                            emitted += 1
                            self._emit(
                                "item_found",
                                f"Found video {emitted}: {record.title_original or record.source_url}",
                                source_url=record.source_url,
                                created_at=record.created_at,
                                source_video_id=record.source_video_id,
                            )
                            yield record

                            if scan_input.limit is not None and emitted >= scan_input.limit:
                                self._emit("scan_limit", f"Reached scan limit: {scan_input.limit}")
                                return

                        try:
                            page.mouse.wheel(0, 1800)
                            page.wait_for_timeout(1200)
                        except PlaywrightError as exc:
                            self._emit(
                                "scan_warning",
                                f"Facebook navigated/reloaded while scrolling. Waiting before continuing: {exc}",
                            )
                            try:
                                page.wait_for_load_state("domcontentloaded", timeout=10000)
                                page.wait_for_timeout(1500)
                            except PlaywrightError:
                                break
                        self._emit(
                            "scan_scroll",
                            f"Scroll {scroll_index}/{scrolls}; collected {len(seen)} unique video link(s).",
                            scroll=scroll_index,
                            total=scrolls,
                            collected=len(seen),
                        )

                    found_here = len(seen) - before_count
                    self._emit("scan_entries", f"Collected {found_here} new visible video link(s) from this source.")
            finally:
                context.close()

        if emitted == 0:
            self._emit(
                "scan_warning",
                "Browser scanner found no video links. Open the profile manually, switch to Videos/Reels, "
                "confirm videos are visible to this browser profile, then run scan again.",
            )

    def _emit(self, event: str, message: str, **payload) -> None:
        logger.info("%s %s", event, message)
        if self.progress_cb:
            self.progress_cb({"event": event, "message": message, **payload})


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
    progress_cb=None,
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
            updates = _existing_video_updates(existing, record)
            if updates:
                update_video_fields(existing["id"], updates, db_path=db_path)
                _emit(
                    progress_cb,
                    "item_done",
                    f"Updated existing video id={existing['id']} metadata.",
                    id=existing["id"],
                    updates=updates,
                )
            logger.info(
                "Skipping existing video source_url=%s source_video_id=%s existing_id=%s",
                record.source_url,
                record.source_video_id,
                existing["id"],
            )
            _emit(progress_cb, "item_skip", f"Skipped existing video id={existing['id']}", id=existing["id"])
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
        _emit(
            progress_cb,
            "item_done",
            f"Stored discovered video id={new_id}: {record.title_original or record.source_url}",
            id=new_id,
            source_url=record.source_url,
            source_video_id=record.source_video_id,
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
    _emit(
        progress_cb,
        "done",
        f"Scan complete. scanned={summary.scanned}, discovered={summary.discovered}, skipped_existing={summary.skipped_existing}",
        summary={
            "scanned": summary.scanned,
            "discovered": summary.discovered,
            "skipped_existing": summary.skipped_existing,
        },
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


def _candidate_source_urls(source_page_url: str) -> list[str]:
    base = source_page_url.strip().rstrip("/")
    urls = [base]
    parsed = urlparse(base)
    if "facebook.com" not in parsed.netloc:
        return urls

    path = parsed.path.rstrip("/")
    if path.endswith("/videos") or path.endswith("/reels"):
        return urls

    if path.endswith("/profile.php"):
        urls.append(_with_query_param(parsed, "sk", "videos"))
        urls.append(_with_query_param(parsed, "sk", "reels_tab"))
    else:
        urls.append(f"{base}/videos")
        urls.append(f"{base}/reels")

    return list(dict.fromkeys(urls))


def _with_query_param(parsed, key: str, value: str) -> str:
    query = parse_qs(parsed.query, keep_blank_values=True)
    query[key] = [value]
    return urlunparse(
        (
            parsed.scheme,
            parsed.netloc,
            parsed.path,
            parsed.params,
            urlencode(query, doseq=True),
            parsed.fragment,
        )
    )


def _flatten_entries(info: dict | None) -> Iterable[dict]:
    if not info:
        return
    entries = info.get("entries")
    if not entries:
        yield info
        return
    for entry in entries:
        if not entry:
            continue
        nested = entry.get("entries") if isinstance(entry, dict) else None
        if nested:
            yield from _flatten_entries(entry)
        else:
            yield entry


def _entry_to_video_record(entry: dict, source_page_url: str) -> VideoRecord:
    video_id = str(entry.get("id") or "").strip()
    source_url = str(entry.get("webpage_url") or entry.get("url") or "").strip()
    if source_url and source_url.startswith("/"):
        source_url = f"https://www.facebook.com{source_url}"
    if source_url and not source_url.startswith(("http://", "https://")):
        if video_id:
            source_url = f"https://www.facebook.com/watch/?v={video_id}"
        else:
            source_url = ""
    if not source_url and video_id:
        source_url = f"https://www.facebook.com/watch/?v={video_id}"

    created_at = _entry_created_at(entry)
    title = str(entry.get("title") or entry.get("description") or "").strip()
    return VideoRecord(
        source_url=source_url,
        source_video_id=video_id,
        source_page_url=source_page_url,
        title_original=title,
        created_at=created_at,
        download_status="discovered",
        publish_status="pending",
    )


def _extract_playwright_candidates(page) -> list[dict]:
    return page.evaluate(
        """
        () => Array.from(document.querySelectorAll('a[href]')).map((anchor) => {
          const unique = (items) => Array.from(new Set(items.map((item) => (item || '').trim()).filter(Boolean)));
          const isCommentArea = (node) => {
            if (!node || !node.closest) return false;
            const commentSelectors = [
              '[aria-label*="Comment"]',
              '[aria-label*="comment"]',
              '[aria-label*="Bình luận"]',
              '[aria-label*="bình luận"]',
              '[role="form"]',
              '[contenteditable="true"]',
              'textarea',
              'input'
            ];
            return Boolean(node.closest(commentSelectors.join(',')));
          };
          const isBeforeAnchor = (node, anchor) => {
            if (!node || !anchor || node === anchor || node.contains(anchor)) return false;
            return Boolean(node.compareDocumentPosition(anchor) & Node.DOCUMENT_POSITION_FOLLOWING);
          };
          const collectLines = (root, anchor) => {
            if (!root) return [];
            const selectors = [
              '[data-ad-preview="message"]',
              'div[dir="auto"]',
              'span[dir="auto"]',
              'h1',
              'h2',
              'h3'
            ];
            const nodes = Array.from(root.querySelectorAll(selectors.join(',')));
            return unique(nodes
              .filter((node) => !isCommentArea(node))
              .filter((node) => isBeforeAnchor(node, anchor))
              .map((node) => node.innerText || node.textContent || '')
              .filter((text) => text.length <= 500));
          };
          const collectDates = (root, anchor) => {
            if (!root) return [];
            const nodes = Array.from(root.querySelectorAll('a[href*="__cft__"], a[href*="story_fbid"], time, abbr'));
            return unique(nodes
              .filter((node) => !isCommentArea(node))
              .filter((node) => isBeforeAnchor(node, anchor))
              .map((node) => node.innerText || node.textContent || node.getAttribute('aria-label') || '')
              .filter(Boolean));
          };
          const containers = [];
          let node = anchor;
          for (let depth = 0; depth < 6 && node; depth += 1) {
            containers.push(node);
            node = node.parentElement;
          }
          const article = anchor.closest('[role="article"]') || anchor.closest('[data-pagelet]');
          if (article) containers.push(article);
          const lines = unique(containers.flatMap((container) => collectLines(container, anchor)));
          const dateLines = unique(containers.flatMap((container) => collectDates(container, anchor)));
          return {
            href: anchor.href || '',
            text: (anchor.innerText || '').trim(),
            label: (anchor.getAttribute('aria-label') || anchor.title || '').trim(),
            lines,
            dateLines,
            context: article ? (article.innerText || '').trim().slice(0, 1600) : ''
          };
        })
        """
    )


def _extract_playwright_candidates(page) -> list[dict]:
    return page.evaluate(
        r"""
        () => {
          const videoUrlRe = /(\/watch\/|\/videos\/|\/reel\/|\/reels\/|\/share\/v\/|story_fbid=|[?&]v=)/i;
          const unique = (items) => Array.from(new Set(items.map((item) => (item || '').trim()).filter(Boolean)));
          const textOf = (node) => (node && (node.innerText || node.textContent || '') || '').trim();
          const isVideoAnchor = (anchor) => videoUrlRe.test(anchor.href || '');
          const postContainerFor = (anchor) => {
            const selectors = [
              '[role="article"]',
              '[data-pagelet^="FeedUnit"]',
              '[data-pagelet*="FeedUnit"]',
              '[data-pagelet*="ProfileTimeline"]',
              '[data-pagelet*="Permalink"]'
            ];
            for (const selector of selectors) {
              const found = anchor.closest(selector);
              if (found) return found;
            }
            let node = anchor;
            for (let depth = 0; depth < 8 && node; depth += 1) {
              if ((textOf(node).length > 20) && node.querySelectorAll('a[href]').length <= 80) return node;
              node = node.parentElement;
            }
            return anchor.parentElement;
          };
          const scrubClone = (container) => {
            const clone = container.cloneNode(true);
            const removeSelectors = [
              '[aria-label*="Comment"]',
              '[aria-label*="comment"]',
              '[aria-label*="Bình luận"]',
              '[aria-label*="bình luận"]',
              '[role="form"]',
              '[contenteditable="true"]',
              'textarea',
              'input',
              'button',
              '[role="button"]',
              '[role="menu"]',
              '[role="navigation"]',
              '[aria-label*="Reactions"]',
              '[aria-label*="reaction"]',
              '[aria-label*="Thích"]',
              '[aria-label*="Share"]',
              '[aria-label*="Chia sẻ"]',
              'a[href*="/watch/"]',
              'a[href*="/videos/"]',
              'a[href*="/reel/"]',
              'a[href*="/reels/"]',
              'a[href*="/share/v/"]',
              'a[href*="story_fbid="]',
              'a[href*="?v="]',
              'svg',
              'img',
              'video'
            ];
            clone.querySelectorAll(removeSelectors.join(',')).forEach((node) => node.remove());
            Array.from(clone.querySelectorAll('*')).forEach((node) => {
              const text = textOf(node).toLowerCase();
              if (
                text.includes('xem thêm bình luận') ||
                text.includes('viết bình luận') ||
                text.includes('bình luận dưới') ||
                text.includes('reply') ||
                text.includes('trả lời') ||
                text.includes('fan cứng')
              ) {
                node.remove();
              }
            });
            return clone;
          };
          const captionCandidates = (container) => {
            const clone = scrubClone(container);
            const preferredSelectors = [
              '[data-ad-preview="message"]',
              '[data-ad-comet-preview="message"]',
              '[data-ad-rendering-role="message"]',
              '[data-ad-rendering-role="story_message"]'
            ];
            const preferred = unique(preferredSelectors.flatMap((selector) =>
              Array.from(clone.querySelectorAll(selector)).map(textOf)
            ));
            const fallback = unique(Array.from(clone.querySelectorAll('div[dir="auto"], span[dir="auto"]'))
              .map(textOf)
              .filter((text) => text.length <= 500));
            return preferred.length ? preferred.concat(fallback) : fallback;
          };
          const dateCandidates = (container) => unique(Array.from(container.querySelectorAll('a[href*="__cft__"], a[href*="story_fbid"], time, abbr'))
            .map((node) => textOf(node) || node.getAttribute('aria-label') || ''));

          return Array.from(document.querySelectorAll('a[href]'))
            .filter(isVideoAnchor)
            .map((anchor) => {
              const container = postContainerFor(anchor);
              const rawTitleCandidates = captionCandidates(container);
              const dateLines = dateCandidates(container);
              return {
                href: anchor.href || '',
                text: '',
                label: '',
                lines: rawTitleCandidates,
                rawTitleCandidates,
                dateLines,
                containerTextLength: textOf(container).length,
                context: ''
              };
            });
        }
        """
    )


def _candidate_to_video_record(item: dict, source_page_url: str) -> VideoRecord:
    source_url = _normalize_facebook_video_url(str(item.get("href") or ""))
    if not source_url:
        return VideoRecord()

    lines = [str(line or "").strip() for line in item.get("lines") or [] if str(line or "").strip()]
    date_lines = [str(line or "").strip() for line in item.get("dateLines") or [] if str(line or "").strip()]
    context = "\n".join(lines) + "\n" + " ".join(
        part.strip()
        for part in [
            str(item.get("text") or ""),
            str(item.get("label") or ""),
            str(item.get("context") or ""),
        ]
        if part and part.strip()
    )
    source_video_id = _extract_source_video_id(source_url)
    created_at = _date_from_text("\n".join(date_lines) + "\n" + context)
    title, title_reason = _select_title(lines, source_video_id)
    item["titleReason"] = title_reason
    return VideoRecord(
        source_url=source_url,
        source_video_id=source_video_id,
        source_page_url=source_page_url,
        title_original=title,
        created_at=created_at,
        download_status="discovered",
        publish_status="pending",
    )


def _normalize_facebook_video_url(url: str) -> str:
    if not url:
        return ""
    parsed = urlparse(url)
    if "facebook.com" not in parsed.netloc:
        return ""
    if not _looks_like_video_url(url):
        return ""

    query = parse_qs(parsed.query, keep_blank_values=True)
    clean_query = {}
    for key in ("v", "story_fbid", "id"):
        if key in query:
            clean_query[key] = query[key]

    clean_path = parsed.path.rstrip("/") or parsed.path
    return urlunparse(
        (
            "https",
            "www.facebook.com",
            clean_path,
            "",
            urlencode(clean_query, doseq=True),
            "",
        )
    )


def _looks_like_video_url(url: str) -> bool:
    parsed = urlparse(url)
    path = parsed.path.lower()
    query = parse_qs(parsed.query)
    return (
        re.search(r"/videos/\d+", path) is not None
        or re.search(r"/reel/\d+", path) is not None
        or "/watch/" in path
        or re.search(r"/share/v/[^/?#]+", path) is not None
        or "story_fbid" in query
        or "v" in query
    )


def _extract_source_video_id(url: str) -> str:
    parsed = urlparse(url)
    query = parse_qs(parsed.query)
    if query.get("v"):
        return query["v"][0]
    patterns = [
        r"/videos/(\d+)",
        r"/reel/(\d+)",
        r"/share/v/([^/?#]+)",
    ]
    for pattern in patterns:
        match = re.search(pattern, parsed.path)
        if match:
            return match.group(1)
    digest = hashlib.sha1(url.encode("utf-8")).hexdigest()[:12]
    return f"fb_{digest}"


def _canonical_video_key(url: str) -> str:
    video_id = _extract_source_video_id(url)
    if video_id:
        return video_id
    return _normalize_facebook_video_url(url)


def _title_from_context(context: str, source_video_id: str) -> str:
    ignored = {
        "like",
        "comment",
        "share",
        "thích",
        "bình luận",
        "chia sẻ",
        "send",
        "follow",
        "theo dõi",
    }
    lines = []
    for raw_line in context.splitlines():
        line = re.sub(r"\s+", " ", raw_line).strip()
        if not line:
            continue
        if line.lower() in ignored:
            continue
        if len(line) <= 2:
            continue
        lines.append(line)
    if not lines:
        return f"Facebook video {source_video_id}" if source_video_id else "Facebook video"
    return lines[0][:180]


def _title_from_context(context: str, source_video_id: str) -> str:
    lines: list[str] = []
    for raw_line in context.splitlines():
        line = re.sub(r"\s+", " ", raw_line).strip()
        if not line or _is_facebook_ui_line(line):
            continue
        if line not in lines:
            lines.append(line)
    if not lines:
        return f"Facebook video {source_video_id}" if source_video_id else "Facebook video"

    caption_like = [
        line
        for line in lines
        if len(line) >= 12 and not _looks_like_person_or_page_name(line)
    ]
    if caption_like:
        return max(caption_like, key=lambda item: min(len(item), 160))[:180]
    return lines[0][:180]


def _title_from_lines(lines: list[str], context: str, source_video_id: str) -> str:
    candidates: list[str] = []
    for line in lines:
        cleaned = _clean_title_line(line)
        if cleaned and cleaned not in candidates:
            candidates.append(cleaned)

    scored = [
        (_title_score(candidate), index, candidate)
        for index, candidate in enumerate(candidates)
        if _title_score(candidate) > 0
    ]
    if scored:
        scored.sort(key=lambda item: (-item[0], item[1]))
        return scored[0][2][:180]

    fallback = _title_from_context(context, source_video_id)
    if _looks_like_person_or_page_name(fallback) or _is_comment_like_title(fallback):
        return f"Facebook video {source_video_id}" if source_video_id else "Facebook video"
    return fallback


def normalize_title(title: str) -> str:
    title = re.sub(r"\s+", " ", title or "").strip()
    title = re.sub(r"\s+Xem thêm$", "", title, flags=re.IGNORECASE).strip()
    title = re.sub(r"\s+See more$", "", title, flags=re.IGNORECASE).strip()
    if _is_facebook_ui_line(title):
        return ""
    lowered = title.lower()
    ui_parts = [
        "thích",
        "like",
        "bình luận",
        "comment",
        "chia sẻ",
        "share",
        "xem thêm",
        "fan cứng",
        "trả lời",
        "reply",
        "xem thêm bình luận",
    ]
    if any(part in lowered for part in ui_parts):
        if len(title.split()) <= 8:
            return ""
    if "facebook.com/" in lowered or "http://" in lowered or "https://" in lowered:
        return ""
    return title[:120]


def _select_title(raw_candidates: list[str], source_video_id: str) -> tuple[str, str]:
    normalized: list[str] = []
    for candidate in raw_candidates:
        title = normalize_title(candidate)
        if not title:
            continue
        if _is_comment_like_title(title) or _looks_like_person_or_page_name(title):
            continue
        if title not in normalized:
            normalized.append(title)

    scored = [
        (_title_score(candidate), index, candidate)
        for index, candidate in enumerate(normalized)
        if _title_score(candidate) >= 35
    ]
    if scored:
        scored.sort(key=lambda item: (-item[0], item[1]))
        selected = scored[0][2]
        return selected, f"selected highest-confidence caption candidate score={scored[0][0]}"

    fallback = f"Facebook video {source_video_id}" if source_video_id else "Facebook video"
    if raw_candidates:
        return fallback, "fallback: no reliable caption candidate after filtering UI/comment/sidebar text"
    return fallback, "fallback: no caption candidates found in post container"


def _clean_title_line(line: str) -> str:
    line = re.sub(r"\s+", " ", line or "").strip()
    line = re.sub(r"\s+Xem thêm$", "", line, flags=re.IGNORECASE).strip()
    if not line or _is_facebook_ui_line(line):
        return ""
    lowered = line.lower()
    if "facebook.com/" in lowered or "http://" in lowered or "https://" in lowered:
        return ""
    if lowered.replace(" ", "") in {"xemtronbo", "xemtieptronbophim", "tronbo"}:
        return ""
    if lowered.startswith(("xem tiếp", "xem trọn", "trọn bộ", "tron bo")):
        return ""
    if _is_comment_like_title(line):
        return ""
    return line


def _title_score(line: str) -> int:
    lowered = line.lower()
    score = min(len(line), 120)
    if re.search(r"^\d+\s*[-–]", line):
        score += 90
    if any(mark in line for mark in [".", "?", "!", ":", "-", "–"]):
        score += 20
    if len(line.split()) >= 5:
        score += 25
    if re.search(r"\b(tập|tap|phần|phan|danh sách|review|phim|truyện|truyen)\b", lowered):
        score += 35
    if _looks_like_person_or_page_name(line):
        score -= 80
    if "xem" in lowered and len(line.split()) <= 5:
        score -= 60
    return score


def _is_facebook_ui_line(line: str) -> bool:
    normalized = line.strip().lower()
    if _date_from_text(normalized):
        return True
    exact_ignored = {
        "like",
        "comment",
        "share",
        "send",
        "follow",
        "video",
        "reels",
        "thich",
        "binh luan",
        "chia se",
        "gui",
        "theo doi",
        "xem them",
        "phat tat ca",
        "xemtronbo",
        "xem trọn bộ",
        "xem tiếp trọn bộ phim",
        "tron bo",
        "trọn bộ",
    }
    if normalized in exact_ignored:
        return True
    patterns = [
        r"b.n xem tr..c",
        r"th..c phim",
        r"bản xem trước",
        r"thước phim",
        r"\bpreview\b",
        r"\bviews?\b",
        r"l..t xem",
        r"^\d+([,.]\d+)?\s*[kKmM]?\s*$",
        r"^\d+([,.]\d+)?\s*[kKmM]?\s+",
        r"^\d+\s*(gi.|ph.t|ng.y|tu.n|th.ng|n.m|h|m|d|w)\b",
        r"^\d+\s*(comments?|shares?|likes?)\b",
        r"^\d+\s*(b.nh lu.n|chia s.|l..t th.ch)\b",
    ]
    return any(re.search(pattern, normalized) for pattern in patterns)


def _looks_like_person_or_page_name(line: str) -> bool:
    if len(line) > 50:
        return False
    if any(char in line for char in ".!?#"):
        return False
    words = line.split()
    return 1 <= len(words) <= 5


def _is_comment_like_title(line: str) -> bool:
    lowered = line.lower().strip()
    comment_phrases = [
        "cho tôi xem",
        "cho toi xem",
        "xin link",
        "xem phim trọn bộ",
        "xem phim tron bo",
        "hay quá",
        "hay qua",
        "cập nhật trọn bộ",
        "cap nhat tron bo",
        "bộ phim đang hot",
        "bo phim dang hot",
    ]
    return any(phrase in lowered for phrase in comment_phrases)


def _date_from_text(text: str) -> str:
    normalized = re.sub(r"\s+", " ", text or "").lower()
    today = date.today()

    if "hôm qua" in normalized or "yesterday" in normalized:
        return f"{(today - timedelta(days=1)).isoformat()} 00:00:00"

    relative = re.search(r"(\d+)\s*(ngày|day|days)\b", normalized)
    if relative:
        return f"{(today - timedelta(days=int(relative.group(1)))).isoformat()} 00:00:00"

    if re.search(r"(\d+)\s*(giờ|phút|hour|hours|min|mins|minute|minutes)\b", normalized):
        return f"{today.isoformat()} 00:00:00"

    iso = re.search(r"\b(20\d{2})[-/](\d{1,2})[-/](\d{1,2})\b", normalized)
    if iso:
        return _format_date_parts(iso.group(1), iso.group(2), iso.group(3))

    slash = re.search(r"\b(\d{1,2})/(\d{1,2})/(20\d{2})\b", normalized)
    if slash:
        return _format_date_parts(slash.group(3), slash.group(1), slash.group(2))

    vi = re.search(r"\b(\d{1,2})\s*tháng\s*(\d{1,2})(?:\s*,?\s*(20\d{2}))?", normalized)
    if vi:
        return _format_date_parts(vi.group(3) or str(today.year), vi.group(2), vi.group(1))

    return ""


def _format_date_parts(year: str, month: str, day: str) -> str:
    try:
        parsed = date(int(year), int(month), int(day))
        return f"{parsed.isoformat()} 00:00:00"
    except ValueError:
        return ""


def _date_from_text(text: str) -> str:
    normalized = re.sub(r"\s+", " ", text or "").lower()
    today = date.today()

    if "hôm qua" in normalized or "hom qua" in normalized or "yesterday" in normalized:
        return f"{(today - timedelta(days=1)).isoformat()} 00:00:00"

    relative = re.search(r"(\d+)\s*(ngày|ngay|day|days)\b", normalized)
    if relative:
        return f"{(today - timedelta(days=int(relative.group(1)))).isoformat()} 00:00:00"

    if re.search(r"(\d+)\s*(giờ|gio|phút|phut|hour|hours|min|mins|minute|minutes)\b", normalized):
        return f"{today.isoformat()} 00:00:00"

    iso = re.search(r"\b(20\d{2})[-/](\d{1,2})[-/](\d{1,2})(?:\s+(\d{1,2}):(\d{2}))?\b", normalized)
    if iso:
        return _format_datetime_parts(iso.group(1), iso.group(2), iso.group(3), iso.group(4), iso.group(5))

    slash = re.search(r"\b(\d{1,2})/(\d{1,2})/(20\d{2})(?:\s+(\d{1,2}):(\d{2}))?\b", normalized)
    if slash:
        return _format_datetime_parts(slash.group(3), slash.group(1), slash.group(2), slash.group(4), slash.group(5))

    vi = re.search(
        r"\b(\d{1,2})\s*(?:tháng|thang|th.ng)\s*(\d{1,2})(?:\s*,?\s*(20\d{2}))?(?:\s*(?:lúc|luc)\s*(\d{1,2}):(\d{2}))?",
        normalized,
    )
    if vi:
        return _format_datetime_parts(vi.group(3) or str(today.year), vi.group(2), vi.group(1), vi.group(4), vi.group(5))

    return ""


def _format_datetime_parts(year: str, month: str, day: str, hour: str | None = None, minute: str | None = None) -> str:
    try:
        parsed = datetime(int(year), int(month), int(day), int(hour or 0), int(minute or 0))
        return parsed.strftime("%Y-%m-%d %H:%M:%S")
    except ValueError:
        return ""


def _existing_video_updates(existing, record: VideoRecord) -> dict:
    updates = {}
    existing_title = str(existing["title_original"] or "")
    new_title = record.title_original or ""
    if new_title and _should_replace_title(existing_title, new_title):
        updates["title_original"] = new_title

    existing_created_at = str(existing["created_at"] or "")
    if not existing_created_at and record.created_at:
        updates["created_at"] = record.created_at

    return updates


def _should_replace_title(existing_title: str, new_title: str) -> bool:
    if not _clean_title_line(new_title):
        return False
    if new_title.lower().startswith("facebook video"):
        return False
    if not existing_title:
        return True
    if _is_bad_title(existing_title):
        return True
    return _title_score(new_title) > _title_score(existing_title) + 50


def _is_bad_title(title: str) -> bool:
    if not title:
        return True
    lowered = title.lower()
    bad_parts = [
        "bản xem trước",
        "thước phim",
        "facebook video",
        "facebook.com/",
        "xemtronbo",
        "xem tiếp trọn bộ phim",
        "trọn bộ :",
        "tron bo :",
    ]
    return (
        any(part in lowered for part in bad_parts)
        or _is_comment_like_title(title)
        or _looks_like_person_or_page_name(title)
    )


def _entry_created_at(entry: dict) -> str:
    for key in ("timestamp", "release_timestamp", "modified_timestamp"):
        value = entry.get(key)
        if value:
            try:
                return datetime.fromtimestamp(int(value)).strftime("%Y-%m-%d %H:%M:%S")
            except (TypeError, ValueError, OSError):
                pass

    for key in ("upload_date", "release_date", "modified_date"):
        value = str(entry.get(key) or "").strip()
        if len(value) == 8 and value.isdigit():
            return f"{value[0:4]}-{value[4:6]}-{value[6:8]} 00:00:00"
        if len(value) >= 10:
            try:
                parsed = datetime.fromisoformat(value[:10])
                return parsed.strftime("%Y-%m-%d 00:00:00")
            except ValueError:
                pass
    return ""


def _record_created_date(record: VideoRecord) -> date | None:
    if not record.created_at:
        return None
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
        try:
            return datetime.strptime(record.created_at[:19], fmt).date()
        except ValueError:
            continue
    return None


def _emit(progress_cb, event: str, message: str, **payload) -> None:
    if progress_cb:
        progress_cb({"event": event, "message": message, **payload})


def _extract_playwright_candidates(page) -> list[dict]:
    return page.evaluate(
        r"""
        () => {
          const directVideoRe = /(\/watch\/|\/videos\/\d+|\/reel\/\d+|\/reels\/\d+|\/share\/v\/|[?&]v=)/i;
          const permalinkRe = /(permalink\.php|story_fbid=)/i;
          const unique = (items) => Array.from(new Set(items.map((item) => (item || '').trim()).filter(Boolean)));
          const textOf = (node) => (node && (node.innerText || node.textContent || '') || '').trim();
          const postContainerFor = (anchor) => {
            const selectors = [
              '[role="article"]',
              '[data-pagelet^="FeedUnit"]',
              '[data-pagelet*="FeedUnit"]',
              '[data-pagelet*="ProfileTimeline"]',
              '[data-pagelet*="Permalink"]'
            ];
            for (const selector of selectors) {
              const found = anchor.closest(selector);
              if (found) return found;
            }
            let node = anchor;
            for (let depth = 0; depth < 8 && node; depth += 1) {
              if ((textOf(node).length > 20) && node.querySelectorAll('a[href]').length <= 80) return node;
              node = node.parentElement;
            }
            return anchor.parentElement;
          };
          const containerHasVideo = (container) => Boolean(container && (
            container.querySelector('video') ||
            Array.from(container.querySelectorAll('a[href]')).some((link) => directVideoRe.test(link.href || ''))
          ));
          const shouldUseAnchor = (anchor, container) => {
            const href = anchor.href || '';
            if (directVideoRe.test(href)) return true;
            return permalinkRe.test(href) && containerHasVideo(container);
          };
          const scrubClone = (container) => {
            const clone = container.cloneNode(true);
            const removeSelectors = [
              '[aria-label*="Comment"]',
              '[aria-label*="comment"]',
              '[aria-label*="Bình luận"]',
              '[aria-label*="bình luận"]',
              '[role="form"]',
              '[contenteditable="true"]',
              'textarea',
              'input',
              'button',
              '[role="button"]',
              '[role="menu"]',
              '[role="navigation"]',
              '[aria-label*="Reactions"]',
              '[aria-label*="reaction"]',
              '[aria-label*="Thích"]',
              '[aria-label*="Share"]',
              '[aria-label*="Chia sẻ"]',
              'a[href*="/watch/"]',
              'a[href*="/videos/"]',
              'a[href*="/reel/"]',
              'a[href*="/reels/"]',
              'a[href*="/share/v/"]',
              'a[href*="story_fbid="]',
              'a[href*="?v="]',
              'svg',
              'img',
              'video'
            ];
            clone.querySelectorAll(removeSelectors.join(',')).forEach((node) => node.remove());
            Array.from(clone.querySelectorAll('*')).forEach((node) => {
              const text = textOf(node).toLowerCase();
              if (
                text.includes('xem thêm bình luận') ||
                text.includes('viết bình luận') ||
                text.includes('bình luận dưới') ||
                text.includes('reply') ||
                text.includes('trả lời') ||
                text.includes('fan cứng')
              ) {
                node.remove();
              }
            });
            return clone;
          };
          const captionCandidates = (container) => {
            const clone = scrubClone(container);
            const preferredSelectors = [
              '[data-ad-preview="message"]',
              '[data-ad-comet-preview="message"]',
              '[data-ad-rendering-role="message"]',
              '[data-ad-rendering-role="story_message"]'
            ];
            const preferred = unique(preferredSelectors.flatMap((selector) =>
              Array.from(clone.querySelectorAll(selector)).map(textOf)
            ));
            const fallback = unique(Array.from(clone.querySelectorAll('div[dir="auto"], span[dir="auto"]'))
              .map(textOf)
              .filter((text) => text.length <= 500));
            return preferred.length ? preferred.concat(fallback) : fallback;
          };
          const dateCandidates = (container) => unique(Array.from(container.querySelectorAll('a[href*="__cft__"], a[href*="story_fbid"], time, abbr'))
            .map((node) => textOf(node) || node.getAttribute('aria-label') || ''));

          return Array.from(document.querySelectorAll('a[href]'))
            .map((anchor) => {
              const container = postContainerFor(anchor);
              if (!shouldUseAnchor(anchor, container)) return null;
              const rawTitleCandidates = captionCandidates(container);
              const dateLines = dateCandidates(container);
              return {
                href: anchor.href || '',
                text: '',
                label: '',
                lines: rawTitleCandidates,
                rawTitleCandidates,
                dateLines,
                containerTextLength: textOf(container).length,
                context: ''
              };
            })
            .filter(Boolean);
        }
        """
    )
