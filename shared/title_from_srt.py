from __future__ import annotations

import os
import re
from pathlib import Path

from openai import OpenAI

BASE_DIR = Path(__file__).resolve().parent.parent
SIBLING_DOUYIN_DIR = BASE_DIR.parent / "douyin_downloader"
SRT_TIME_RE = re.compile(r"\d{2}:\d{2}:\d{2}[,.]\d{3}\s+-->\s+\d{2}:\d{2}:\d{2}[,.]\d{3}")
SPACES_RE = re.compile(r"\s+")

TITLE_PROMPT = """
Bạn là biên tập viên tiêu đề cho video reup phim ngắn/drama tiếng Việt trên Facebook.

Hãy đọc transcript phụ đề và viết 1 tiêu đề tiếng Việt để đăng Fanpage.

Quy tắc:
- Chỉ trả về đúng 1 dòng tiêu đề, không giải thích.
- Không thêm "Tiêu đề:" hoặc Markdown.
- Không dùng emoji, không hashtag.
- Tối đa 100 ký tự.
- Đúng nội dung transcript, không bịa tình tiết.
- Có điểm gây tò mò, hợp drama/phim ngắn.
- Nếu nội dung là mẹ chồng nàng dâu, tổng tài, thiếu gia, hào môn, con dâu, thân phận, trả thù thì có thể dùng đúng các từ đó.
- Nếu là cổ trang/huyền huyễn thì dùng văn phong cổ trang nhẹ, dễ hiểu.
""".strip()


def generate_title_from_srt(srt_path: str | Path) -> str:
    api_key = _get_config("OPENAI_API_KEY")
    if not api_key:
        raise RuntimeError(
            "OPENAI_API_KEY is not set. Add it to DownVidFB/.env or douyin_downloader/.env."
        )

    srt_path = Path(srt_path)
    if not srt_path.exists():
        raise FileNotFoundError(f"SRT file not found: {srt_path}")

    transcript = srt_to_text(srt_path.read_text(encoding="utf-8", errors="ignore"))
    if not transcript:
        raise RuntimeError(f"SRT has no usable text: {srt_path}")

    max_chars = int(_get_config("TITLE_FROM_SRT_MAX_CHARS", "8000") or "8000")
    max_title = int(_get_config("TITLE_MAX_CHARS", "100") or "100")
    model = _get_config("OPENAI_TITLE_MODEL") or _get_config("OPENAI_MODEL") or "gpt-4.1-mini"

    client = OpenAI(api_key=api_key)
    response = client.responses.create(
        model=model,
        instructions=TITLE_PROMPT,
        input="Hãy viết tiêu đề tiếng Việt cho video dựa trên transcript sau:\n\n" + transcript[:max_chars],
    )
    return clean_title(response.output_text or "", max_title=max_title)


def srt_to_text(srt_text: str) -> str:
    lines: list[str] = []
    for raw_line in (srt_text or "").splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if line.isdigit():
            continue
        if SRT_TIME_RE.search(line):
            continue
        lines.append(line)
    return SPACES_RE.sub(" ", " ".join(lines)).strip()


def clean_title(value: str, max_title: int = 100) -> str:
    title = (value or "").strip().strip('"\'`')
    for prefix in ("Tiêu đề:", "Tựa đề:", "Title:"):
        if title.lower().startswith(prefix.lower()):
            title = title[len(prefix):].strip()
    title = title.replace("#", "").strip()
    title = SPACES_RE.sub(" ", title)
    if len(title) > max_title:
        title = title[:max_title].rstrip(" ,.;:-")
    return title


def _get_config(name: str, default: str = "") -> str:
    value = os.getenv(name, "").strip()
    if value:
        return value
    for env_path in _candidate_env_files():
        value = _read_env_value(env_path, name)
        if value:
            os.environ.setdefault(name, value)
            return value
    return default


def _candidate_env_files() -> list[Path]:
    return [
        BASE_DIR / ".env",
        SIBLING_DOUYIN_DIR / ".env",
    ]


def _read_env_value(env_path: Path, name: str) -> str:
    if not env_path.exists():
        return ""
    try:
        for raw_line in env_path.read_text(encoding="utf-8", errors="ignore").splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            if key.strip() == name:
                return value.strip().strip('"\'')
    except Exception:
        return ""
    return ""