import logging
import os
import shutil
import subprocess
import uuid
from pathlib import Path

logger = logging.getLogger(__name__)

ASR_API_LIMIT_BYTES = 25 * 1024 * 1024
SAFE_ASR_UPLOAD_BYTES = 23 * 1024 * 1024


def prepare_audio_chunks(
    input_path: str | Path,
    chunks_dir: str | Path = "data/asr_chunks",
    segment_seconds: int = 600,
    audio_bitrate: str = "32k",
    max_upload_bytes: int = SAFE_ASR_UPLOAD_BYTES,
    keep_existing: bool = False,
) -> list[Path]:
    source = Path(input_path)
    if not source.exists() or not source.is_file():
        raise FileNotFoundError(f"Input media file not found: {source}")
    if segment_seconds <= 0:
        raise ValueError("segment_seconds must be greater than 0.")

    _ensure_ffmpeg()

    run_id = uuid.uuid4().hex[:8]
    output_dir = Path(chunks_dir) / f"{_safe_stem(source)}_{run_id}"
    if output_dir.exists() and not keep_existing:
        shutil.rmtree(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    output_pattern = str(output_dir / "chunk_%03d.mp3")
    cmd = [
        "ffmpeg",
        "-y",
        "-hide_banner",
        "-loglevel",
        "error",
        "-i",
        str(source),
        "-vn",
        "-ac",
        "1",
        "-ar",
        "16000",
        "-b:a",
        audio_bitrate,
        "-f",
        "segment",
        "-segment_time",
        str(segment_seconds),
        "-reset_timestamps",
        "1",
        output_pattern,
    ]

    logger.info("Preparing ASR chunks: %s", " ".join(cmd))
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=1800)
    if result.returncode != 0:
        error = result.stderr.strip() or result.stdout.strip() or f"ffmpeg failed with exit code {result.returncode}"
        raise RuntimeError(f"Could not prepare ASR audio chunks: {error}")

    chunks = sorted(output_dir.glob("chunk_*.mp3"))
    if not chunks:
        raise RuntimeError("ffmpeg completed but no ASR audio chunks were created.")

    oversized = [chunk for chunk in chunks if chunk.stat().st_size > max_upload_bytes]
    if oversized:
        details = ", ".join(f"{chunk.name}={chunk.stat().st_size}" for chunk in oversized[:5])
        raise RuntimeError(
            "ASR chunk is still too large after compression. "
            f"Reduce --segment-seconds. Oversized: {details}"
        )
    return chunks


def transcribe_media_openai(
    input_path: str | Path,
    model: str = "gpt-4o-mini-transcribe",
    language: str = "",
    prompt: str = "",
    output_file: str | Path = "",
    chunks_dir: str | Path = "data/asr_chunks",
    segment_seconds: int = 600,
    keep_chunks: bool = False,
    dry_run: bool = False,
    progress_cb=None,
) -> dict:
    source = Path(input_path)
    chunks = prepare_audio_chunks(
        source,
        chunks_dir=chunks_dir,
        segment_seconds=segment_seconds,
    )
    summary = {
        "input_file": str(source),
        "chunks": [str(chunk) for chunk in chunks],
        "chunk_count": len(chunks),
        "dry_run": dry_run,
        "transcript": "",
        "output_file": str(output_file) if output_file else "",
    }

    _emit(progress_cb, "chunks_ready", f"Prepared {len(chunks)} ASR chunk(s).", chunks=summary["chunks"])
    for chunk in chunks:
        size = chunk.stat().st_size
        _emit(progress_cb, "chunk_ready", f"{chunk.name}: {_format_bytes(size)}", chunk=str(chunk), bytes=size)

    if dry_run:
        _emit(progress_cb, "dry_run", "Dry run is ON. No ASR API call will be made.")
        return summary

    try:
        from openai import OpenAI
    except ImportError as exc:
        raise RuntimeError("openai package is not installed. Run: pip install -r requirements.txt") from exc

    if not os.environ.get("OPENAI_API_KEY"):
        raise RuntimeError("OPENAI_API_KEY is not set.")

    client = OpenAI()
    texts: list[str] = []
    for index, chunk in enumerate(chunks, start=1):
        size = chunk.stat().st_size
        if size > SAFE_ASR_UPLOAD_BYTES:
            raise RuntimeError(f"ASR chunk exceeds safe upload limit: {chunk} ({size} bytes)")

        _emit(progress_cb, "asr_start", f"[{index}/{len(chunks)}] Transcribing {chunk.name} ({_format_bytes(size)})")
        params = {"model": model}
        if language:
            params["language"] = language
        if prompt:
            params["prompt"] = prompt

        with chunk.open("rb") as audio_file:
            response = client.audio.transcriptions.create(file=audio_file, **params)
        text = _response_text(response)
        texts.append(text)
        _emit(progress_cb, "asr_done", f"[{index}/{len(chunks)}] Done. {len(text)} chars.", chars=len(text))

    transcript = "\n\n".join(part.strip() for part in texts if part.strip())
    summary["transcript"] = transcript

    if output_file:
        out = Path(output_file)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(transcript, encoding="utf-8")
        summary["output_file"] = str(out)
        _emit(progress_cb, "saved", f"Transcript saved to {out}")

    if not keep_chunks:
        _cleanup_chunk_dir(chunks)
    return summary


def _response_text(response) -> str:
    if isinstance(response, str):
        return response
    text = getattr(response, "text", None)
    if text is not None:
        return str(text)
    if isinstance(response, dict):
        return str(response.get("text", ""))
    return str(response)


def _cleanup_chunk_dir(chunks: list[Path]) -> None:
    if not chunks:
        return
    parent = chunks[0].parent
    try:
        shutil.rmtree(parent)
    except OSError:
        logger.warning("Could not remove ASR chunks directory: %s", parent)


def _ensure_ffmpeg() -> None:
    if shutil.which("ffmpeg"):
        return
    raise RuntimeError("ffmpeg was not found in PATH. Install ffmpeg before running ASR.")


def _safe_stem(path: Path) -> str:
    stem = path.stem.strip() or "media"
    return "".join(char if char.isalnum() or char in {"-", "_"} else "_" for char in stem)[:80]


def _format_bytes(value: int) -> str:
    return f"{value / 1024 / 1024:.2f} MiB"


def _emit(progress_cb, event: str, message: str, **payload) -> None:
    if progress_cb:
        progress_cb({"event": event, "message": message, **payload})
