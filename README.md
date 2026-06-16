# DownVidFB - Facebook Video Pipeline

Local pipeline for managing videos before posting/scheduling them to a Facebook Fanpage.

This project can work with:

- Facebook video links imported from Excel/CSV.
- Facebook source pages/profiles discovered by scanner.
- Final rendered videos synced from the sibling `douyin_downloader` project.

The project stores metadata in SQLite and exposes both CLI and local web/desktop UI.

## Main Features

- Import Excel/CSV video lists.
- Scan Facebook source profile/page metadata.
- Download pending Facebook videos.
- Sync completed Douyin final videos into the publishing database.
- Generate schedule times.
- Publish or schedule pending videos to Facebook Fanpage.
- Local dashboard with job logs, filters, checkbox delete, and dry-run controls.
- Keeps old downloader/publisher folders available for legacy use.

## Requirements

- Python 3.10+
- FFmpeg if using ASR/transcribe helpers
- Playwright browser if using browser scanner/downloader
- Facebook Page ID and Page Access Token for real publishing

## Install

```powershell
cd D:\Workspcae\MMO\AUTO_RENDER_UPLOAD_FB\DownVidFB
python -m venv venv
.\venv\Scripts\activate
python -m pip install -U pip
pip install -r requirements.txt
python -m playwright install chromium
```

Create local config:

```powershell
copy .env.example .env
```

Fill `.env` only on your machine. Never commit real tokens.

## Run Web Dashboard

```powershell
.\venv\Scripts\activate
python -m uvicorn pipeline_web.main:app --host 127.0.0.1 --port 8010
```

Open:

```text
http://127.0.0.1:8010
```

Dashboard sections:

- Scan Facebook Source
- Import Excel/CSV
- Sync Douyin Finals
- Download Pending
- Generate Schedule
- Publish Pending
- Recent Jobs and live logs
- Videos table with filters and row selection

## Run Desktop Wrapper

```powershell
python desktop_app.py
```

If `pywebview` is installed, it opens as a desktop window. Otherwise it opens in your browser.

Build exe:

```powershell
.\scripts\build_desktop_exe.ps1
```

## Common Workflow

### A. Publish videos rendered by Douyin project

1. Finish processing videos in `douyin_downloader`.
2. Confirm final files are H.264/AAC in:

```text
..\douyin_downloader\output\final\
```

3. In DownVidFB dashboard, click `Sync Douyin Finals`.
4. Keep `Dry run` checked first to preview.
5. Uncheck `Dry run` and sync for real.
6. Generate schedule.
7. Review rows in the table.
8. Run `Publish Pending` with dry-run first.
9. Uncheck dry-run only when ready to post/schedule to Facebook.

CLI equivalent:

```powershell
python main.py sync-douyin-finals --dry-run --limit 20
python main.py sync-douyin-finals --limit 20
python main.py generate-schedule --start-time "2026-06-15 08:00:00" --interval-minutes 60 --limit 20
python main.py publish-pending --dry-run --limit 5
```

### B. Import Excel/CSV list

Supported columns for source links:

```text
video_id,title,source_url,created_at
```

Recommended: use `.xlsx` for Vietnamese text. If using CSV, export as `CSV UTF-8`.

Example:

```powershell
python main.py import-excel files\sample_videos.csv
```

### C. Scan Facebook source

```powershell
python main.py scan-source --source-page-url "https://www.facebook.com/page-or-profile" --date-from 2026-06-01 --date-to 2026-06-10 --limit 20 --scanner browser
```

Test scanner without Facebook:

```powershell
python main.py scan-source --source-page-url "https://www.facebook.com/example" --date-from 2026-06-01 --date-to 2026-06-10 --limit 3 --scanner mock
```

## CLI Commands

Initialize DB:

```powershell
python scripts\init_pipeline_db.py
```

Import Excel/CSV:

```powershell
python main.py import-excel files\sample_videos.csv
```

Download pending:

```powershell
python main.py download-pending --limit 10 --browser chrome
```

Generate schedule:

```powershell
python main.py generate-schedule --start-time "2026-06-15 08:00:00" --interval-minutes 60 --limit 50
```

Publish pending:

```powershell
python main.py publish-pending --limit 5 --page-id YOUR_PAGE_ID --page-access-token YOUR_PAGE_ACCESS_TOKEN
```

Or use environment variables:

```powershell
$env:FB_PAGE_ID="YOUR_PAGE_ID"
$env:FB_PAGE_ACCESS_TOKEN="YOUR_PAGE_ACCESS_TOKEN"
python main.py publish-pending --limit 5
```

Transcribe large media safely:

```powershell
python main.py transcribe "data\downloads\video.mp4" --dry-run
```

## Database

Default SQLite database:

```text
data/pipeline.sqlite3
```

Main table: `videos`.

Important fields:

```text
source_url, source_video_id, source_page_url,
title_original, title_rewrite, created_at,
local_filename, download_status, publish_status,
schedule_time, fb_post_id, error_message
```

The database is local runtime data and is ignored by Git.

## GitHub Safety

The `.gitignore` excludes:

- `.env`, tokens, cookies, browser sessions
- SQLite databases and runtime data
- downloaded/uploaded/generated videos
- Excel/CSV user data except safe sample files
- logs, cache, virtualenvs, and build artifacts

Before pushing:

```powershell
git status --ignored
```

Make sure only source code, safe sample files, docs, and requirements are staged.

## Notes

- Always test publishing with `Dry run` first.
- Do not commit Page Access Tokens or personal cookies.
- Generated videos can be very large and should stay out of Git.
- If Facebook upload shows audio-only, verify the video is H.264/AAC before syncing/publishing.
## Title from Douyin SRT

When videos are processed in `douyin_downloader`, the Vietnamese SRT can be used to generate a Facebook-ready title with OpenAI. The generated title is saved in:

```text
..\douyin_downloader\data\app.db -> videos.title
```

When you run `Sync Douyin Finals`, DownVidFB reads that title and stores it in the Facebook pipeline database as:

```text
title_original
title_rewrite
```

So the publish step can use the generated title instead of the filename.

For old Douyin videos, run this in the Douyin project first:

```powershell
cd ..\douyin_downloader
python tools\generate_titles_from_srt.py --limit 20
```

Then return to DownVidFB and sync again:

```powershell
cd ..\DownVidFB
python main.py sync-douyin-finals --limit 20
```