"""Joining the pieces of a browser recording into one playable, seekable file."""
import logging
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
