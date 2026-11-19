"""Speech-to-text: ffmpeg prepares the audio, Groq Whisper transcribes it.

Recordings can be hours long and Groq accepts at most 25 MB per request, so we:
  1. drop the video track and re-encode to 16 kHz mono Opus (~0.25 MB per minute)
  2. split into fixed-length chunks
  3. transcribe each chunk and shift its timestamps by the chunk's start time
"""
import logging
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from ..config import Settings

log = logging.getLogger(__name__)


class TranscriptionError(RuntimeError):
    """Raised with a message that is safe to show users."""


@dataclass
class TranscriptionResult:
    text: str
    segments: list[dict] = field(default_factory=list)  # [{"start": 0.0, "end": 4.2, "text": "..."}]
    language: str | None = None
    provider: str | None = None


def _run(cmd: list[str]) -> str:
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=1800)
    if proc.returncode != 0:
        log.warning("%s failed: %s", cmd[0], proc.stderr[-2000:])
        raise TranscriptionError("Could not read the audio in this recording. Is it a valid audio/video file?")
    return proc.stdout


def probe_duration(path: Path) -> float:
    out = _run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(path)])
    try:
        return float(out.strip())
    except ValueError:
        return 0.0


def prepare_chunks(src: Path, workdir: Path, chunk_seconds: int) -> list[Path]:
    """Convert to compact mono speech audio and split into chunks of `chunk_seconds`."""
    if not src.exists():
        raise TranscriptionError("The recording file is missing on the server. Please upload it again.")
    _run([
        "ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error", "-i", str(src),
        "-vn", "-ac", "1", "-ar", "16000", "-c:a", "libopus", "-b:a", "32k",
        "-f", "segment", "-segment_time", str(chunk_seconds), "-reset_timestamps", "1",
        str(workdir / "chunk_%04d.ogg"),
    ])
    chunks = sorted(workdir.glob("chunk_*.ogg"))
    if not chunks:
        raise TranscriptionError("The recording has no audio track.")
    return chunks


def transcribe_with_groq(settings: Settings, audio_path: Path, hint: str | None = None) -> TranscriptionResult:
    if not settings.groq_api_key:
        raise TranscriptionError("Transcription needs GROQ_API_KEY in .env.")

    from groq import Groq

    client = Groq(api_key=settings.groq_api_key, timeout=settings.ai_timeout_seconds, max_retries=2)
    model = settings.groq_transcription_model
    segments: list[dict] = []
    language = None
    offset = 0.0

    with tempfile.TemporaryDirectory(prefix="meetmind-audio-") as tmp:
        chunks = prepare_chunks(audio_path, Path(tmp), settings.transcription_chunk_minutes * 60)
        log.info("Transcribing %s in %d chunk(s) with %s", audio_path.name, len(chunks), model)
        for chunk in chunks:
            resp = client.audio.transcriptions.create(
                file=(chunk.name, chunk.read_bytes()),
                model=model,
                response_format="verbose_json",
                timestamp_granularities=["segment"],
                temperature=0,
                # Whisper uses the prompt as spelling context (names, product terms)
                prompt=(hint or None) and hint[:600],
            )
            data = resp.model_dump() if hasattr(resp, "model_dump") else dict(resp)
            language = language or data.get("language")
            for seg in data.get("segments") or []:
                text = (seg.get("text") or "").strip()
                if text:
                    segments.append({
                        "start": round(offset + float(seg.get("start", 0)), 2),
                        "end": round(offset + float(seg.get("end", 0)), 2),
                        "text": text,
                    })
            if not data.get("segments") and data.get("text"):
                segments.append({"start": round(offset, 2), "end": None, "text": data["text"].strip()})
            offset += probe_duration(chunk)

    return TranscriptionResult(
        text=" ".join(s["text"] for s in segments).strip(),
        segments=segments,
        language=language,
        provider=f"groq:{model}",
    )


def format_timestamp(seconds: float | None) -> str:
    s = int(seconds or 0)
    return f"{s // 3600:02d}:{s % 3600 // 60:02d}:{s % 60:02d}"


def format_transcript(segments: list[dict] | None, text: str) -> str:
    """Transcript as '[hh:mm:ss] Speaker: text' lines, so the AI can cite when (and by whom) things were said.
    Speaker names appear once diarization is added (Stage C)."""
    if not segments:
        return text
    return "\n".join(
        f"[{format_timestamp(s.get('start'))}] " + (f"{s['speaker']}: " if s.get("speaker") else "") + s.get("text", "")
        for s in segments
    )
