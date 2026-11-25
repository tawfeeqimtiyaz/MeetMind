"""Joining the pieces of a browser recording into one playable, seekable file, and taking still images
of the slides shown in a screen recording."""
import logging
import re
import shutil
import subprocess
from pathlib import Path

log = logging.getLogger(__name__)


def assemble_pieces(folder: Path, target: Path) -> int:
    """Concatenate `000000.part, 000001.part, ...` into `target`, then let ffmpeg rewrite the container.

    MediaRecorder pieces are consecutive slices of ONE stream, so joining the bytes gives a valid file.
    That file has no duration/seek index, so `ffmpeg -c copy` rewrites it (fast, no re-encoding) to make
    the player's timeline and seeking work. Returns the final size in bytes.
    """
    pieces = sorted(folder.glob("*.part"))
    if not pieces:
        raise FileNotFoundError(f"No recorded pieces in {folder}")
    target.parent.mkdir(parents=True, exist_ok=True)
    joined = target.with_suffix(target.suffix + ".joined")
    with joined.open("wb") as out:
        for piece in pieces:
            with piece.open("rb") as src:
                shutil.copyfileobj(src, out, 1024 * 1024)

    remuxed = target.with_suffix(".remux" + target.suffix)
    proc = subprocess.run(
        ["ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error", "-y", "-fflags", "+genpts",
         "-i", str(joined), "-c", "copy", str(remuxed)],
        capture_output=True, text=True, timeout=3600,
    )
    if proc.returncode == 0 and remuxed.exists() and remuxed.stat().st_size > 0:
        remuxed.replace(target)
        joined.unlink(missing_ok=True)
    else:  # keep the raw joined file: still transcribable, just not seekable in the browser
        log.warning("Remux failed for %s: %s", target.name, proc.stderr[-500:])
        remuxed.unlink(missing_ok=True)
        joined.replace(target)
    shutil.rmtree(folder, ignore_errors=True)
    return target.stat().st_size


# ---------- slides: still images of what was on the screen ----------

def _run(args: list[str], timeout: int = 1800) -> subprocess.CompletedProcess:
    return subprocess.run(args, capture_output=True, text=True, timeout=timeout)


def has_video(path: Path) -> bool:
    """True if the file contains a video stream (screen recording or uploaded video)."""
    proc = _run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries", "stream=codec_type",
                 "-of", "csv=p=0", str(path)], timeout=60)
    return proc.returncode == 0 and "video" in proc.stdout


def duration_seconds(path: Path) -> float | None:
    proc = _run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(path)], timeout=60)
    try:
        return float(proc.stdout.strip())
    except ValueError:
        return None


def scene_changes(path: Path, threshold: float) -> list[float]:
    """Seconds at which the picture changes a lot (a new slide). One frame per second is enough for slides
    and keeps this fast even for long recordings."""
    proc = _run(["ffmpeg", "-nostdin", "-hide_banner", "-i", str(path), "-an",
                 "-vf", f"fps=1,select='gt(scene,{threshold})',showinfo", "-f", "null", "-"])
    return [float(t) for t in re.findall(r"pts_time:([\d.]+)", proc.stderr)]


def still_periods(path: Path, min_seconds: float = 2.0, noise: float = 0.003) -> list[tuple[float, float | None]]:
    """Periods in which the picture stands still for at least `min_seconds`: in a presentation, each one is a
    slide being shown. This works even when slides share one template (a "scene change" score is tiny then).
    Returns [(start, end)]; end is None when the still lasts until the end of the video."""
    proc = _run(["ffmpeg", "-nostdin", "-hide_banner", "-i", str(path), "-an",
                 "-vf", f"fps=2,freezedetect=n={noise}:d={min_seconds}", "-f", "null", "-"])
    starts = [float(t) for t in re.findall(r"freeze_start: ([\d.]+)", proc.stderr)]
    ends = [float(t) for t in re.findall(r"freeze_end: ([\d.]+)", proc.stderr)]
    return [(s, ends[i] if i < len(ends) else None) for i, s in enumerate(starts)]


def pick_still_times(periods: list[tuple[float, float | None]], duration: float | None, min_gap: float,
                     limit: int) -> list[float]:
    """One moment inside each still period (1.5 s in, or the middle of a short one)."""
    times: list[float] = []
    for start, end in periods:
        stop = end if end is not None else (duration if duration else start + 3.0)
        at = start + min(1.5, max(0.0, stop - start) / 2)
        if not times or at - times[-1] >= min_gap:
            times.append(at)
    if len(times) > limit:
        step = len(times) / limit
        times = [times[int(i * step)] for i in range(limit)]
    return [round(t, 2) for t in times]


def pick_slide_times(changes: list[float], duration: float | None, min_gap: float, limit: int) -> list[float]:
    """When to take a still: the start, then a moment after each change (so animations have settled).
    Changes closer than `min_gap` are skipped; at most `limit` stills, spread evenly if there are more."""
    settle = 1.5
    end = duration if duration else None
    times = [min(1.0, end / 2) if end else 1.0]
    for t in sorted(changes):
        at = t + settle
        if end is not None and at > end - 0.2:
            at = max(t, end - 0.5)
        if at - times[-1] >= min_gap:
            times.append(at)
    if len(times) > limit:
        step = len(times) / limit
        times = [times[int(i * step)] for i in range(limit)]
    return [round(t, 2) for t in times]


def grab_frame(path: Path, at: float, target: Path, max_width: int = 1280) -> bool:
    target.parent.mkdir(parents=True, exist_ok=True)
    proc = _run(["ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error", "-y", "-ss", f"{at:.2f}", "-i", str(path),
                 "-frames:v", "1", "-vf", f"scale='min({max_width},iw)':-2", "-q:v", "3", str(target)], timeout=120)
    return proc.returncode == 0 and target.exists() and target.stat().st_size > 0


def normalize_image(source: Path, target: Path, max_width: int = 1600) -> bool:
    """Phone photos can be 4000px and several MB: shrink to a JPEG the vision models accept quickly."""
    proc = _run(["ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error", "-y", "-i", str(source),
                 "-frames:v", "1", "-vf", f"scale='min({max_width},iw)':-2", "-q:v", "3", str(target)], timeout=120)
    return proc.returncode == 0 and target.exists() and target.stat().st_size > 0
