from dataclasses import dataclass
from typing import Optional


@dataclass(slots=True)
class VideoRecord:
    source_url: str = ""
    source_video_id: str = ""
    source_page_url: str = ""
    title_original: str = ""
    title_rewrite: str = ""
    created_at: str = ""
    local_filename: str = ""
    download_status: str = "pending"
    publish_status: str = "pending"
    schedule_time: str = ""
    fb_post_id: str = ""
    error_message: str = ""
    id: Optional[int] = None
