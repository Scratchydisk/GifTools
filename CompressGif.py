"""Compress animated or still GIFs with ffmpeg.

The tool re-encodes a GIF with a freshly generated palette. You can cap the
palette size, turn dithering off for the smallest files, resample the frame
rate, scale the image down, and let the tool search for the largest scale that
still fits a target file size. gifsicle is optional and adds lossy
optimisation when it is installed.
"""

import os
import shutil
import subprocess
import tempfile
import time
import tkinter as tk
from dataclasses import dataclass, replace
from tkinter import filedialog, messagebox, ttk
from typing import List, Optional, Tuple

from PIL import Image, ImageSequence, ImageTk

FFMPEG = "ffmpeg"
MIN_COLOURS = 2
MAX_COLOURS = 256
MIN_SCALE_PERCENT = 10
DEFAULT_SCALE_PERCENT = 100
MIN_TARGET_SCALE = 0.05
MIN_SHORT_SIDE = 16
DEFAULT_TARGET_KB = 500
MAX_TARGET_KB = 102400
MAX_TARGET_ITERATIONS = 5
DEFAULT_LOSSY_LEVEL = 30
MAX_LOSSY_LEVEL = 200
LOSSY_HELP_TEXT = """\
Higher values allow gifsicle to alter colours more aggressively.
The result is a smaller file with more artefacts and colour banding.

- 0: no lossy colour alteration; plain optimisation only.
- 20-40: gentle. The app defaults to 30 (gifsicle defaults to 20).
- 50-80: balanced. Artefacts may show on gradients and photos.
- 100-200: aggressive. Smallest files, most visible artefacts.

The range is 0-200. The number is a lossiness value, not a percentage.

The compressor runs both plain gifsicle optimisation and the lossy
pass, then keeps the smaller result. Enabling lossy therefore cannot
make the file larger. If the lossy pass does not win, you simply get
the smaller plain-optimised result.

For the biggest reduction, lower the maximum colours and/or scale as
well. Fewer colours often matter more than a high lossy level.\
"""
MAX_FPS = 60

DITHER_OPTIONS = {
    "None (smallest)": "none",
    "Bayer (balanced)": "bayer",
    "Sierra 2-4a (best quality)": "sierra2_4a",
}
DEFAULT_DITHER_LABEL = "None (smallest)"

PREVIEW_SIZE = 420
MAX_PREVIEW_UPSCALE = 4.0
RESAMPLE = getattr(Image, "Resampling", Image).LANCZOS

OPEN_FILETYPES = [
    ("GIF files", "*.gif"),
    ("All files", "*.*"),
]


@dataclass
class GifInfo:
    path: str
    size: int
    width: int
    height: int
    frames: int
    loop: Optional[int]
    delays_cs: List[int]
    has_transparency: bool

    @property
    def is_animated(self) -> bool:
        return self.frames > 1

    @property
    def duration_seconds(self) -> float:
        return sum(self.delays_cs) / 100.0

    @property
    def fps(self) -> float:
        duration = self.duration_seconds
        if duration <= 0:
            return 0.0
        return self.frames / duration


@dataclass
class CompressionSettings:
    max_colours: int
    dither: str
    keep_timing: bool
    fps: float
    scale: float
    optimize_diff: bool
    target_bytes: Optional[int]
    lossy_level: Optional[int]


@dataclass
class CompressionResult:
    output_path: str
    source: GifInfo
    output: GifInfo
    scale: float
    elapsed_seconds: float
    target_bytes: Optional[int]
    target_met: bool
    copied_source: bool = False
    timing_patched: bool = True
    lossy_applied: bool = False

    @property
    def saved_bytes(self) -> int:
        return self.source.size - self.output.size

    @property
    def saved_percent(self) -> float:
        if self.source.size <= 0:
            return 0.0
        return 100.0 * self.saved_bytes / self.source.size


class CompressionError(Exception):
    """A user-facing compression failure."""


class TargetNotMetError(CompressionError):
    """Raised when even the smallest attempt is larger than the target."""

    def __init__(self, target_bytes, smallest_size, width, height, scale):
        self.target_bytes = target_bytes
        self.smallest_size = smallest_size
        self.width = width
        self.height = height
        self.scale = scale
        super().__init__(
            "Could not reach {}.\n\n"
            "The smallest result was {} at {} x {} px ({:.0f}% scale).\n\n"
            "Try lowering the max colours, reducing the frame rate, or "
            "choosing a larger target.".format(
                format_size(target_bytes),
                format_size(smallest_size),
                width,
                height,
                scale * 100,
            )
        )


def format_size(num_bytes: int) -> str:
    if num_bytes < 1024:
        return "{} B".format(num_bytes)
    if num_bytes < 1024 * 1024:
        text = "{:.1f}".format(num_bytes / 1024.0)
        if text.endswith(".0"):
            text = text[:-2]
        return text + " KB"
    text = "{:.2f}".format(num_bytes / (1024.0 * 1024.0))
    text = text.rstrip("0").rstrip(".")
    return text + " MB"


def ffmpeg_available() -> bool:
    return shutil.which(FFMPEG) is not None


def _find_gifsicle() -> Optional[str]:
    found = shutil.which("gifsicle")
    if found:
        return found
    local = os.path.expanduser("~/.local/bin/gifsicle")
    if os.path.isfile(local) and os.access(local, os.X_OK):
        return local
    return None


def gifsicle_available() -> bool:
    return _find_gifsicle() is not None


def _gce_info(data) -> List[Tuple[int, int, int]]:
    """Return (delay_cs, packed, delay_byte_offset) for each GIF frame.

    This walks the GIF block structure instead of searching for byte
    patterns, so compressed image data cannot produce false positives.
    """
    if not data.startswith((b"GIF87a", b"GIF89a")):
        raise ValueError("not a GIF file")
    if len(data) < 13:
        raise ValueError("truncated GIF header")

    packed = data[10]
    pos = 13
    if packed & 0x80:
        pos += 3 * (2 ** ((packed & 0x07) + 1))

    def skip_subblocks(p):
        while p < len(data):
            size = data[p]
            p += 1
            if size == 0:
                return p
            p += size
        raise ValueError("truncated GIF sub-blocks")

    infos = []
    while pos < len(data):
        marker = data[pos]
        if marker == 0x3B:
            break
        if marker == 0x21:
            if pos + 1 >= len(data):
                raise ValueError("truncated GIF extension")
            label = data[pos + 1]
            pos += 2
            if label == 0xF9:
                if pos + 6 > len(data):
                    raise ValueError("truncated graphic control extension")
                size = data[pos]
                if size != 4:
                    raise ValueError("bad graphic control extension")
                frame_packed = data[pos + 1]
                delay = data[pos + 2] | (data[pos + 3] << 8)
                infos.append((delay, frame_packed, pos + 2))
                pos += 1 + size
                if pos >= len(data) or data[pos] != 0:
                    raise ValueError("bad graphic control extension")
                pos += 1
            else:
                pos = skip_subblocks(pos)
        elif marker == 0x2C:
            if pos + 10 > len(data):
                raise ValueError("truncated GIF image descriptor")
            image_packed = data[pos + 9]
            pos += 10
            if image_packed & 0x80:
                pos += 3 * (2 ** ((image_packed & 0x07) + 1))
            if pos >= len(data):
                raise ValueError("truncated GIF image data")
            pos += 1  # LZW minimum code size
            pos = skip_subblocks(pos)
        else:
            raise ValueError("unexpected GIF marker {:#x}".format(marker))
    return infos


def _read_gce_info(path) -> List[Tuple[int, int, int]]:
    with open(path, "rb") as handle:
        return _gce_info(handle.read())


def _pillow_delays(path, frames):
    delays = []
    try:
        with Image.open(path) as image:
            for frame in ImageSequence.Iterator(image):
                duration = frame.info.get("duration")
                try:
                    duration = int(round(float(duration)))
                except (TypeError, ValueError, OverflowError):
                    duration = 100
                delays.append(max(1, duration // 10))
    except Exception:
        delays = [10] * frames
    if len(delays) != frames:
        delays = (delays + [10] * frames)[:frames]
    return delays


def read_gif_info(path: str) -> GifInfo:
    path = os.path.expanduser(path)
    if not os.path.isfile(path):
        raise CompressionError("File not found:\n{}".format(path))
    try:
        with Image.open(path) as image:
            if image.format != "GIF":
                raise CompressionError("Choose a GIF file.")
            width, height = image.size
            frames = max(1, getattr(image, "n_frames", 1))
            loop = image.info.get("loop")
            fallback_transparency = image.info.get("transparency") is not None
    except CompressionError:
        raise
    except Exception as exc:
        raise CompressionError(
            "Could not read {}:\n{}".format(os.path.basename(path), exc)
        ) from exc

    try:
        infos = _read_gce_info(path)
        delays = [delay for delay, _packed, _offset in infos]
        has_transparency = any(packed & 0x01 for _delay, packed, _offset in infos)
        if len(delays) != frames:
            delays = _pillow_delays(path, frames)
    except Exception:
        delays = _pillow_delays(path, frames)
        has_transparency = fallback_transparency

    if loop is not None:
        try:
            loop = int(loop)
        except (TypeError, ValueError, OverflowError):
            loop = None
    if loop is not None:
        loop = max(-1, min(loop, 65535))

    return GifInfo(
        path=path,
        size=os.path.getsize(path),
        width=width,
        height=height,
        frames=frames,
        loop=loop,
        delays_cs=delays,
        has_transparency=has_transparency,
    )


def patch_gif_delays(path: str, source_delays: List[int]) -> bool:
    """Rewrite the output's frame delays to match the source exactly."""
    if not source_delays:
        return True
    try:
        with open(path, "r+b") as handle:
            infos = _gce_info(handle.read())
            if len(infos) != len(source_delays):
                return False
            for (_delay, _packed, offset), source_delay in zip(infos, source_delays):
                delay = max(0, min(int(source_delay), 65535))
                handle.seek(offset)
                handle.write(bytes((delay & 0xFF, (delay >> 8) & 0xFF)))
    except (OSError, ValueError):
        return False
    return True


def scaled_dimensions(info: GifInfo, scale: float) -> Tuple[int, int]:
    width = min(info.width, max(1, int(round(info.width * scale))))
    height = min(info.height, max(1, int(round(info.height * scale))))
    return width, height


def _loop_argument(info: GifInfo) -> int:
    if info.loop is None:
        return -1
    return max(-1, min(int(info.loop), 65535))


def _paletteuse(settings: CompressionSettings) -> str:
    diff_mode = "rectangle" if settings.optimize_diff else "none"
    return "paletteuse=dither={}:diff_mode={}".format(settings.dither, diff_mode)


def _pre_filters(info: GifInfo, settings: CompressionSettings, width, height):
    filters = []
    if not settings.keep_timing:
        filters.append("fps={:g}".format(settings.fps))
    if (width, height) != (info.width, info.height):
        filters.append("scale={}:{}:flags=lanczos".format(width, height))
    return filters


def _run(cmd: List[str], message: str, missing_message: Optional[str] = None) -> None:
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
    except OSError as exc:
        raise CompressionError(missing_message or "{}:\n{}".format(message, exc)) from exc
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip()
        if len(detail) > 1500:
            detail = detail[-1500:]
        raise CompressionError("{}:\n{}".format(message, detail))


def encode_gif(
    input_path: str,
    output_path: str,
    palette_path: str,
    info: GifInfo,
    settings: CompressionSettings,
    scale: float,
    progress=None,
):
    width, height = scaled_dimensions(info, scale)
    if progress is not None:
        progress(scale, width, height)

    pre = _pre_filters(info, settings, width, height)
    stats_mode = "diff" if info.is_animated else "full"
    reserve = "1" if info.has_transparency else "0"
    palette_filter = ",".join(
        pre
        + [
            "palettegen=max_colors={}:stats_mode={}:reserve_transparent={}".format(
                int(settings.max_colours), stats_mode, reserve
            )
        ]
    )
    palette_cmd = [
        FFMPEG,
        "-y",
        "-nostdin",
        "-hide_banner",
        "-loglevel",
        "error",
        "-i",
        input_path,
        "-vf",
        palette_filter,
        palette_path,
    ]
    _run(
        palette_cmd,
        "Could not generate a colour palette",
        "ffmpeg was not found. Install ffmpeg and make sure it is on your PATH.",
    )

    if pre:
        filter_complex = ",".join(pre) + "[x];[x][1:v]" + _paletteuse(settings)
    else:
        filter_complex = "[0:v][1:v]" + _paletteuse(settings)

    encode_cmd = [
        FFMPEG,
        "-y",
        "-nostdin",
        "-hide_banner",
        "-loglevel",
        "error",
        "-i",
        input_path,
        "-i",
        palette_path,
        "-filter_complex",
        filter_complex,
        "-loop",
        str(_loop_argument(info)),
        "-f",
        "gif",
        output_path,
    ]
    _run(
        encode_cmd,
        "Could not encode the GIF",
        "ffmpeg was not found. Install ffmpeg and make sure it is on your PATH.",
    )
    return os.path.getsize(output_path), width, height


def apply_gifsicle(path: str, lossy_level: int) -> bool:
    """Run gifsicle optimisation, keeping the smallest plain/lossy result."""
    gifsicle = _find_gifsicle()
    if not gifsicle:
        raise CompressionError(
            "gifsicle was not found. Install gifsicle or turn off lossy optimisation."
        )
    original_size = os.path.getsize(path)
    o3_path = path + ".o3.gif"
    lossy_path = path + ".lossy.gif"
    _remove(o3_path)
    _remove(lossy_path)
    _run(
        [gifsicle, "-O3", path, "-o", o3_path],
        "gifsicle optimisation failed",
        "gifsicle was not found. Install gifsicle or turn off lossy optimisation.",
    )
    _run(
        [
            gifsicle,
            "-O3",
            "--lossy={}".format(int(lossy_level)),
            path,
            "-o",
            lossy_path,
        ],
        "gifsicle lossy optimisation failed",
        "gifsicle was not found. Install gifsicle or turn off lossy optimisation.",
    )
    best_path = path
    best_size = original_size
    lossy_used = False
    o3_size = os.path.getsize(o3_path)
    if o3_size < best_size:
        best_path = o3_path
        best_size = o3_size
    lossy_size = os.path.getsize(lossy_path)
    if lossy_size < best_size:
        best_path = lossy_path
        best_size = lossy_size
        lossy_used = True
    if best_path != path:
        try:
            os.replace(best_path, path)
        except OSError as exc:
            raise CompressionError(
                "Could not apply gifsicle output:\n{}".format(exc)
            ) from exc
    for temp_path in (o3_path, lossy_path):
        if temp_path != best_path:
            _remove(temp_path)
    return lossy_used


def _remove(path: str) -> None:
    try:
        os.remove(path)
    except OSError:
        pass


def _validate_settings(info: GifInfo, settings: CompressionSettings) -> None:
    minimum = 3 if info.has_transparency else MIN_COLOURS
    if not (minimum <= settings.max_colours <= MAX_COLOURS):
        if info.has_transparency:
            raise CompressionError(
                "This GIF contains transparency, so it needs at least 3 colours "
                "(one palette slot is reserved for transparency)."
            )
        raise CompressionError(
            "Max colours must be between {} and {}.".format(MIN_COLOURS, MAX_COLOURS)
        )
    if settings.dither not in ("none", "bayer", "sierra2_4a"):
        raise CompressionError("Choose a valid dither option.")
    if not settings.keep_timing and not (0 < settings.fps <= MAX_FPS):
        raise CompressionError(
            "FPS must be greater than 0 and no more than {}.".format(MAX_FPS)
        )
    if not (0 < settings.scale <= 1.0):
        raise CompressionError("Scale must be between 0 and 100%.")
    if settings.target_bytes is not None and settings.target_bytes <= 0:
        raise CompressionError("Target size must be at least 1 KB.")
    if settings.lossy_level is not None:
        if not (0 <= settings.lossy_level <= MAX_LOSSY_LEVEL):
            raise CompressionError(
                "Lossy level must be between 0 and {}.".format(MAX_LOSSY_LEVEL)
            )
        if not gifsicle_available():
            raise CompressionError(
                "gifsicle was not found. Install gifsicle or turn off lossy optimisation."
            )


def _can_copy(settings: CompressionSettings) -> bool:
    return (
        settings.max_colours >= MAX_COLOURS
        and settings.dither == DITHER_OPTIONS[DEFAULT_DITHER_LABEL]
        and settings.keep_timing
        and settings.scale >= 1.0
        and settings.optimize_diff
        and settings.lossy_level is None
    )


def _same_file(first: str, second: str) -> bool:
    if os.path.abspath(first) == os.path.abspath(second):
        return True
    try:
        return os.path.samefile(first, second)
    except OSError:
        return False


def _encode_attempt(
    input_path: str,
    temp_dir: str,
    attempt_number: int,
    info: GifInfo,
    settings: CompressionSettings,
    scale: float,
    progress=None,
):
    output_path = os.path.join(temp_dir, "attempt_{:02d}.gif".format(attempt_number))
    palette_path = os.path.join(temp_dir, "palette_{:02d}.png".format(attempt_number))
    size, width, height = encode_gif(
        input_path, output_path, palette_path, info, settings, scale, progress
    )
    _remove(palette_path)
    lossy_applied = False
    if settings.lossy_level is not None:
        lossy_applied = apply_gifsicle(output_path, settings.lossy_level)
        size = os.path.getsize(output_path)
    return output_path, size, width, height, lossy_applied


def _search_target(
    input_path: str,
    temp_dir: str,
    info: GifInfo,
    settings: CompressionSettings,
    target_bytes: int,
    progress=None,
):
    max_scale = settings.scale
    min_side = min(info.width, info.height)
    min_scale = max(MIN_TARGET_SCALE, MIN_SHORT_SIDE / float(max(1, min_side)))
    min_scale = min(min_scale, max_scale)
    min_scale = max(0.01, min_scale)

    attempt = 0
    path, size, width, height, lossy_applied = _encode_attempt(
        input_path, temp_dir, attempt, info, settings, max_scale, progress
    )
    attempt += 1
    if size <= target_bytes:
        return path, max_scale, size, width, height, lossy_applied

    _remove(path)
    if min_scale >= max_scale - 1e-9:
        raise TargetNotMetError(target_bytes, size, width, height, max_scale)

    high = max_scale
    low_path, low_size, low_width, low_height, low_lossy = _encode_attempt(
        input_path, temp_dir, attempt, info, settings, min_scale, progress
    )
    attempt += 1
    if low_size > target_bytes:
        _remove(low_path)
        raise TargetNotMetError(
            target_bytes, low_size, low_width, low_height, min_scale
        )

    best_path = low_path
    best_size = low_size
    best_width = low_width
    best_height = low_height
    best_scale = min_scale
    best_lossy = low_lossy
    low = min_scale

    for _ in range(MAX_TARGET_ITERATIONS):
        if high - low < 0.005:
            break
        mid = (low + high) / 2.0
        path, size, width, height, attempt_lossy = _encode_attempt(
            input_path, temp_dir, attempt, info, settings, mid, progress
        )
        attempt += 1
        if size <= target_bytes:
            _remove(best_path)
            best_path = path
            best_size = size
            best_width = width
            best_height = height
            best_scale = mid
            best_lossy = attempt_lossy
            low = mid
        else:
            _remove(path)
            high = mid

    return best_path, best_scale, best_size, best_width, best_height, best_lossy


def compress_gif(
    input_path: str,
    output_path: str,
    settings: CompressionSettings,
    progress=None,
) -> CompressionResult:
    start = time.monotonic()
    input_path = os.path.expanduser(input_path)
    output_path = os.path.expanduser(output_path)
    if not os.path.isfile(input_path):
        raise CompressionError("Input file not found:\n{}".format(input_path))
    if not output_path.lower().endswith(".gif"):
        raise CompressionError("Output file must be a .gif file.")
    output_path = os.path.abspath(output_path)
    output_dir = os.path.dirname(output_path)
    if not os.path.isdir(output_dir):
        raise CompressionError(
            "The output folder does not exist:\n{}".format(output_dir)
        )

    source = read_gif_info(input_path)
    _validate_settings(source, settings)

    if (
        settings.target_bytes is not None
        and source.size <= settings.target_bytes
        and _can_copy(settings)
    ):
        if not _same_file(input_path, output_path):
            try:
                shutil.copy2(input_path, output_path)
            except OSError as exc:
                raise CompressionError(
                    "Could not copy the source GIF:\n{}".format(exc)
                ) from exc
        output = read_gif_info(output_path)
        return CompressionResult(
            output_path=output_path,
            source=source,
            output=output,
            scale=1.0,
            elapsed_seconds=time.monotonic() - start,
            target_bytes=settings.target_bytes,
            target_met=True,
            copied_source=True,
        )

    if not ffmpeg_available():
        raise CompressionError(
            "ffmpeg was not found. Install ffmpeg and make sure it is on your PATH."
        )

    try:
        temp_dir = tempfile.mkdtemp(prefix=".giftools-compress-", dir=output_dir)
    except OSError as exc:
        raise CompressionError(
            "Could not create a temporary folder:\n{}".format(exc)
        ) from exc

    try:
        if settings.target_bytes is not None:
            (
                result_path,
                result_scale,
                _size,
                _width,
                _height,
                lossy_applied,
            ) = _search_target(
                input_path,
                temp_dir,
                source,
                settings,
                settings.target_bytes,
                progress,
            )
            target_met = True
        else:
            (
                result_path,
                _size,
                _width,
                _height,
                lossy_applied,
            ) = _encode_attempt(
                input_path, temp_dir, 0, source, settings, settings.scale, progress
            )
            result_scale = settings.scale
            target_met = False

        timing_patched = True
        if settings.keep_timing and source.delays_cs:
            timing_patched = patch_gif_delays(result_path, source.delays_cs)

        output_info = read_gif_info(result_path)
        if output_info.size > source.size and _same_file(input_path, output_path):
            raise CompressionError(
                "The re-encoded file would be {} (larger than the {} source), "
                "so the input was not overwritten.\n\n"
                "Choose a different output path or reduce the settings.".format(
                    format_size(output_info.size), format_size(source.size)
                )
            )
        try:
            os.replace(result_path, output_path)
        except OSError as exc:
            raise CompressionError(
                "Could not save the output file:\n{}".format(exc)
            ) from exc
        output_info = replace(output_info, path=output_path)

        return CompressionResult(
            output_path=output_path,
            source=source,
            output=output_info,
            scale=result_scale,
            elapsed_seconds=time.monotonic() - start,
            target_bytes=settings.target_bytes,
            target_met=target_met,
            timing_patched=timing_patched,
            lossy_applied=lossy_applied,
        )
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)


class CompressGifApp:
    def __init__(self, root):
        self.root = root
        self.root.title("GIF Compressor")
        try:
            self.root.wm_attributes("-toolwindow", True)
        except tk.TclError:
            pass

        self.source_info = None
        self.open_dir = os.path.expanduser("~")
        self.tk_preview = None
        self.has_gifsicle = gifsicle_available()

        self.input_var = tk.StringVar()
        self.output_var = tk.StringVar()
        self.status_var = tk.StringVar()
        self.preview_title_var = tk.StringVar(value="Preview")
        self.preview_info_var = tk.StringVar(value="Open a GIF to see a preview.")
        self.colours_var = tk.StringVar(value=str(MAX_COLOURS))
        self.colour_range_var = tk.StringVar(value="2-256")
        self.colour_hint_var = tk.StringVar(value="GIF supports up to 256 colours.")
        self.dither_var = tk.StringVar(value=DEFAULT_DITHER_LABEL)
        self.keep_timing_var = tk.BooleanVar(value=True)
        self.fps_var = tk.StringVar(value="15")
        self.scale_var = tk.IntVar(value=DEFAULT_SCALE_PERCENT)
        self.scale_label_var = tk.StringVar(value="Scale:")
        self.scale_hint_var = tk.StringVar(value="The output is scaled to this percentage.")
        self.optimize_var = tk.BooleanVar(value=True)
        self.target_enabled_var = tk.BooleanVar(value=False)
        self.target_kb_var = tk.StringVar(value=str(DEFAULT_TARGET_KB))
        self.lossy_enabled_var = tk.BooleanVar(value=False)
        self.lossy_level_var = tk.StringVar(value=str(DEFAULT_LOSSY_LEVEL))

        self._build_ui()
        self._update_control_states()
        if not ffmpeg_available():
            self.status_var.set(
                "ffmpeg was not found. Install ffmpeg and make sure it is on your PATH."
            )
        else:
            self.status_var.set("Choose a GIF to compress.")

    def _build_ui(self):
        outer = tk.Frame(self.root)
        outer.pack(fill=tk.BOTH, expand=True, padx=8, pady=8)

        controls = tk.Frame(outer)
        controls.pack(side=tk.LEFT, fill=tk.Y, padx=(0, 8))

        files = tk.LabelFrame(controls, text="Files")
        files.pack(fill=tk.X)
        files.columnconfigure(1, weight=1)

        tk.Label(files, text="Input GIF:").grid(
            row=0, column=0, sticky="w", padx=(6, 4), pady=(6, 2)
        )
        tk.Entry(files, textvariable=self.input_var, width=34).grid(
            row=0, column=1, sticky="ew", pady=(6, 2)
        )
        tk.Button(files, text="Browse...", command=self.browse_input).grid(
            row=0, column=2, padx=(4, 6), pady=(6, 2)
        )

        tk.Label(files, text="Output GIF:").grid(
            row=1, column=0, sticky="w", padx=(6, 4), pady=2
        )
        tk.Entry(files, textvariable=self.output_var, width=34).grid(
            row=1, column=1, sticky="ew", pady=2
        )
        tk.Button(files, text="Browse...", command=self.browse_output).grid(
            row=1, column=2, padx=(4, 6), pady=2
        )

        settings = tk.LabelFrame(controls, text="Compression settings")
        settings.pack(fill=tk.X, pady=(8, 0))
        settings.columnconfigure(1, weight=1)

        row = 0
        tk.Label(settings, text="Max colours:").grid(
            row=row, column=0, sticky="w", padx=(6, 4), pady=(6, 2)
        )
        self.colours_spin = tk.Spinbox(
            settings,
            from_=MIN_COLOURS,
            to=MAX_COLOURS,
            increment=1,
            width=6,
            textvariable=self.colours_var,
        )
        self.colours_spin.grid(row=row, column=1, sticky="w", pady=(6, 2))
        tk.Label(settings, textvariable=self.colour_range_var, fg="#555555").grid(
            row=row, column=2, sticky="w", padx=(4, 6), pady=(6, 2)
        )
        row += 1
        tk.Label(
            settings,
            textvariable=self.colour_hint_var,
            fg="#555555",
            justify=tk.LEFT,
            wraplength=300,
        ).grid(row=row, column=0, columnspan=3, sticky="w", padx=(6, 6), pady=(0, 4))
        row += 1

        tk.Label(settings, text="Dither:").grid(
            row=row, column=0, sticky="w", padx=(6, 4), pady=2
        )
        self.dither_combo = ttk.Combobox(
            settings,
            textvariable=self.dither_var,
            values=list(DITHER_OPTIONS.keys()),
            state="readonly",
            width=27,
        )
        self.dither_combo.grid(
            row=row, column=1, columnspan=2, sticky="w", padx=(0, 6), pady=2
        )
        row += 1

        tk.Checkbutton(
            settings,
            text="Keep source timing",
            variable=self.keep_timing_var,
            command=self._update_control_states,
        ).grid(row=row, column=0, columnspan=3, sticky="w", padx=(6, 6), pady=(6, 0))
        row += 1
        tk.Label(settings, text="FPS:").grid(
            row=row, column=0, sticky="w", padx=(6, 4), pady=2
        )
        self.fps_spin = tk.Spinbox(
            settings,
            from_=1,
            to=MAX_FPS,
            increment=1,
            width=6,
            textvariable=self.fps_var,
        )
        self.fps_spin.grid(row=row, column=1, sticky="w", pady=2)
        row += 1
        tk.Label(
            settings,
            text="Uncheck Keep source timing to resample to a constant frame rate.",
            fg="#555555",
            justify=tk.LEFT,
            wraplength=300,
        ).grid(row=row, column=0, columnspan=3, sticky="w", padx=(24, 6), pady=(0, 4))
        row += 1

        tk.Label(settings, textvariable=self.scale_label_var).grid(
            row=row, column=0, columnspan=3, sticky="w", padx=(6, 6), pady=(6, 0)
        )
        row += 1
        self.scale = tk.Scale(
            settings,
            from_=MIN_SCALE_PERCENT,
            to=100,
            resolution=1,
            orient=tk.HORIZONTAL,
            variable=self.scale_var,
            length=240,
            showvalue=True,
        )
        self.scale.grid(row=row, column=0, columnspan=3, sticky="we", padx=(6, 6))
        row += 1
        tk.Label(
            settings,
            textvariable=self.scale_hint_var,
            fg="#555555",
            justify=tk.LEFT,
            wraplength=300,
        ).grid(row=row, column=0, columnspan=3, sticky="w", padx=(6, 6), pady=(0, 4))
        row += 1

        tk.Checkbutton(
            settings,
            text="Optimise unchanged frames",
            variable=self.optimize_var,
        ).grid(row=row, column=0, columnspan=3, sticky="w", padx=(6, 6), pady=(4, 0))
        row += 1

        tk.Checkbutton(
            settings,
            text="Compress to target size",
            variable=self.target_enabled_var,
            command=self._update_control_states,
        ).grid(row=row, column=0, columnspan=3, sticky="w", padx=(6, 6), pady=(6, 0))
        row += 1
        self.target_spin = tk.Spinbox(
            settings,
            from_=1,
            to=MAX_TARGET_KB,
            increment=10,
            width=10,
            textvariable=self.target_kb_var,
        )
        self.target_spin.grid(row=row, column=0, sticky="w", padx=(24, 4), pady=2)
        tk.Label(settings, text="KB", fg="#555555").grid(
            row=row, column=1, sticky="w", pady=2
        )
        row += 1
        tk.Label(
            settings,
            text="Finds the largest scale that fits. If the source already fits, "
            "it is copied unchanged.",
            fg="#555555",
            justify=tk.LEFT,
            wraplength=300,
        ).grid(row=row, column=0, columnspan=3, sticky="w", padx=(24, 6), pady=(0, 4))
        row += 1

        self.lossy_check = tk.Checkbutton(
            settings,
            text="gifsicle lossy optimisation",
            variable=self.lossy_enabled_var,
            command=self._update_control_states,
            state=("normal" if self.has_gifsicle else "disabled"),
        )
        self.lossy_check.grid(
            row=row, column=0, columnspan=3, sticky="w", padx=(6, 6), pady=(6, 0)
        )
        row += 1
        tk.Label(settings, text="Lossy level:").grid(
            row=row, column=0, sticky="w", padx=(24, 4), pady=2
        )
        self.lossy_spin = tk.Spinbox(
            settings,
            from_=0,
            to=MAX_LOSSY_LEVEL,
            increment=5,
            width=6,
            textvariable=self.lossy_level_var,
        )
        self.lossy_spin.grid(row=row, column=1, sticky="w", pady=2)
        self.lossy_info_button = tk.Button(
            settings,
            text="Info",
            command=self.show_lossy_help,
            padx=4,
            pady=0,
        )
        self.lossy_info_button.grid(
            row=row, column=2, sticky="w", padx=(6, 6), pady=2
        )
        row += 1
        if not self.has_gifsicle:
            tk.Label(
                settings,
                text="gifsicle not found. Install it to enable lossy mode.",
                fg="#a05000",
                justify=tk.LEFT,
                wraplength=300,
            ).grid(
                row=row,
                column=0,
                columnspan=3,
                sticky="w",
                padx=(24, 6),
                pady=(0, 4),
            )

        self.compress_button = tk.Button(
            controls, text="Compress GIF", command=self.compress
        )
        self.compress_button.pack(fill=tk.X, pady=(10, 0))
        tk.Label(
            controls,
            textvariable=self.status_var,
            justify=tk.LEFT,
            anchor="w",
            wraplength=330,
            fg="#333333",
        ).pack(fill=tk.X, pady=(6, 0))

        preview_frame = tk.Frame(outer, bd=2, relief=tk.SUNKEN)
        preview_frame.pack(side=tk.RIGHT, padx=(8, 0), fill=tk.BOTH, expand=True)

        tk.Label(
            preview_frame,
            textvariable=self.preview_title_var,
            anchor="w",
        ).pack(fill=tk.X, padx=6, pady=(6, 2))
        self.preview_canvas = tk.Canvas(
            preview_frame,
            width=PREVIEW_SIZE,
            height=PREVIEW_SIZE,
            bg="#2b2b2b",
            highlightthickness=0,
        )
        self.preview_canvas.pack()
        tk.Label(
            preview_frame,
            textvariable=self.preview_info_var,
            justify=tk.LEFT,
            anchor="w",
            wraplength=PREVIEW_SIZE,
        ).pack(fill=tk.X, padx=6, pady=6)
        self._draw_placeholder()

    def _update_control_states(self):
        self.fps_spin.configure(
            state=("disabled" if self.keep_timing_var.get() else "normal")
        )
        self.target_spin.configure(
            state=("normal" if self.target_enabled_var.get() else "disabled")
        )
        if self.target_enabled_var.get():
            self.scale_label_var.set("Maximum scale:")
            self.scale_hint_var.set(
                "The target search can scale below this to fit the file size."
            )
        else:
            self.scale_label_var.set("Scale:")
            self.scale_hint_var.set("The output is scaled to this percentage.")
        if self.has_gifsicle:
            self.lossy_spin.configure(
                state=("normal" if self.lossy_enabled_var.get() else "disabled")
            )
        else:
            self.lossy_enabled_var.set(False)
            self.lossy_spin.configure(state="disabled")

    def show_lossy_help(self):
        dialog = tk.Toplevel(self.root)
        dialog.title("About lossy levels")
        dialog.transient(self.root)
        dialog.resizable(False, False)

        frame = ttk.Frame(dialog, padding=16)
        frame.pack(fill=tk.BOTH, expand=True)
        ttk.Label(
            frame,
            text="Lossy levels",
            font=("", 12, "bold"),
        ).pack(anchor="w")
        ttk.Label(
            frame,
            text=LOSSY_HELP_TEXT,
            justify=tk.LEFT,
            wraplength=460,
        ).pack(anchor="w", pady=(8, 12))
        close_button = ttk.Button(frame, text="Close", command=dialog.destroy)
        close_button.pack(anchor="e")
        close_button.focus_set()

        dialog.bind("<Escape>", lambda event: dialog.destroy())
        dialog.update_idletasks()
        x = (
            self.root.winfo_rootx()
            + (self.root.winfo_width() - dialog.winfo_width()) // 2
        )
        y = (
            self.root.winfo_rooty()
            + (self.root.winfo_height() - dialog.winfo_height()) // 2
        )
        dialog.geometry("+{}+{}".format(max(0, x), max(0, y)))
        dialog.grab_set()
        return dialog

    def _draw_placeholder(self):
        self.preview_canvas.delete("all")
        self.preview_canvas.create_text(
            PREVIEW_SIZE / 2,
            PREVIEW_SIZE / 2,
            text="Open a GIF to see a preview",
            fill="#bbbbbb",
        )

    def browse_input(self):
        initial_dir = self.open_dir
        if not os.path.isdir(initial_dir):
            initial_dir = os.path.expanduser("~")
        path = filedialog.askopenfilename(
            title="Open GIF",
            filetypes=OPEN_FILETYPES,
            initialdir=initial_dir,
        )
        if path:
            self.load_source(path)

    def browse_output(self):
        current = self.output_var.get().strip()
        input_path = self.input_var.get().strip()
        initial_dir = os.path.dirname(current) if current else ""
        if not initial_dir and input_path:
            initial_dir = os.path.dirname(os.path.abspath(input_path))
        options = {
            "title": "Save compressed GIF",
            "defaultextension": ".gif",
            "filetypes": [("GIF files", "*.gif")],
        }
        if initial_dir:
            options["initialdir"] = initial_dir
        if current:
            options["initialfile"] = os.path.basename(current)
        path = filedialog.asksaveasfilename(**options)
        if path:
            self.output_var.set(path)

    def _default_output_path(self, path):
        base, _ext = os.path.splitext(path)
        return base + "_compressed.gif"

    def load_source(self, path):
        path = os.path.expanduser(path)
        try:
            info = read_gif_info(path)
        except CompressionError as exc:
            messagebox.showerror("Could not open GIF", str(exc))
            return

        old_path = self.source_info.path if self.source_info is not None else None
        current_output = self.output_var.get().strip()
        old_default = self._default_output_path(old_path) if old_path else ""
        reset_output = not current_output
        if (
            old_default
            and current_output
            and os.path.abspath(current_output) == os.path.abspath(old_default)
        ):
            reset_output = True
        if (
            old_path
            and current_output
            and os.path.abspath(path) != os.path.abspath(old_path)
            and os.path.abspath(current_output) == os.path.abspath(old_path)
        ):
            # The previous output was an in-place edit of the old source.
            reset_output = True
        if reset_output:
            self.output_var.set(self._default_output_path(path))

        self.source_info = info
        self.open_dir = os.path.dirname(os.path.abspath(info.path))
        self.input_var.set(path)
        self._update_colour_range(info)
        if info.fps > 0:
            self.fps_var.set("{:.1f}".format(info.fps).rstrip("0").rstrip("."))
        else:
            self.fps_var.set("15")
        self.preview_title_var.set("Source preview")
        self._update_info(info)
        self._show_preview(path)
        self.status_var.set("Ready to compress.")

    def _update_colour_range(self, info):
        minimum = 3 if info.has_transparency else MIN_COLOURS
        self.colours_spin.configure(from_=minimum)
        self.colour_range_var.set("{}-{}".format(minimum, MAX_COLOURS))
        if info.has_transparency:
            self.colour_hint_var.set(
                "One palette slot is reserved for transparency."
            )
        else:
            self.colour_hint_var.set("GIF supports up to 256 colours.")
        try:
            current = int(self.colours_var.get())
        except (TypeError, ValueError, OverflowError):
            current = MAX_COLOURS
        if current < minimum:
            self.colours_var.set(str(minimum))
        elif current > MAX_COLOURS:
            self.colours_var.set(str(MAX_COLOURS))

    def _update_info(self, info):
        parts = ["{} x {} px".format(info.width, info.height)]
        if info.is_animated:
            parts.append("{} frames".format(info.frames))
            if info.duration_seconds > 0:
                parts.append("{:.2f} s".format(info.duration_seconds))
            if info.fps > 0:
                parts.append("{:.1f} fps".format(info.fps))
        else:
            parts.append("still image")
        parts.append(format_size(info.size))
        self.preview_info_var.set(
            "{}\n{}".format(os.path.basename(info.path), " · ".join(parts))
        )

    def _show_preview(self, path):
        try:
            with Image.open(path) as image:
                frame = image.convert("RGBA").copy()
        except Exception:
            self._draw_placeholder()
            return
        zoom = min(
            PREVIEW_SIZE / float(max(1, frame.width)),
            PREVIEW_SIZE / float(max(1, frame.height)),
            MAX_PREVIEW_UPSCALE,
        )
        width = max(1, int(round(frame.width * zoom)))
        height = max(1, int(round(frame.height * zoom)))
        if (width, height) != frame.size:
            frame = frame.resize((width, height), RESAMPLE)
        self.tk_preview = ImageTk.PhotoImage(frame)
        self.preview_canvas.delete("all")
        self.preview_canvas.create_image(
            PREVIEW_SIZE / 2,
            PREVIEW_SIZE / 2,
            image=self.tk_preview,
            anchor="center",
        )

    def _read_settings(self):
        if self.source_info is None:
            raise CompressionError("Open a GIF first.")

        try:
            colours = int(float(self.colours_var.get()))
        except (TypeError, ValueError, OverflowError):
            raise CompressionError("Max colours must be a whole number.")
        minimum = 3 if self.source_info.has_transparency else MIN_COLOURS
        if not (minimum <= colours <= MAX_COLOURS):
            if self.source_info.has_transparency:
                raise CompressionError(
                    "This GIF contains transparency, so it needs at least 3 colours."
                )
            raise CompressionError(
                "Max colours must be between {} and {}.".format(
                    MIN_COLOURS, MAX_COLOURS
                )
            )

        try:
            scale_percent = int(float(self.scale_var.get()))
        except (TypeError, ValueError, OverflowError):
            raise CompressionError("Scale must be between 10 and 100%.")
        if not (MIN_SCALE_PERCENT <= scale_percent <= 100):
            raise CompressionError("Scale must be between 10 and 100%.")

        keep_timing = self.keep_timing_var.get()
        fps = 15.0
        if not keep_timing:
            try:
                fps = float(self.fps_var.get())
            except (TypeError, ValueError, OverflowError):
                raise CompressionError("FPS must be a number greater than 0.")
            if not (0 < fps <= MAX_FPS):
                raise CompressionError(
                    "FPS must be greater than 0 and no more than {}.".format(MAX_FPS)
                )

        target_bytes = None
        if self.target_enabled_var.get():
            try:
                target_kb = float(self.target_kb_var.get())
            except (TypeError, ValueError, OverflowError):
                raise CompressionError("Target size must be a number in KB.")
            if not (1 <= target_kb <= MAX_TARGET_KB):
                raise CompressionError(
                    "Target size must be between 1 KB and {} KB.".format(
                        MAX_TARGET_KB
                    )
                )
            target_bytes = int(round(target_kb * 1024))

        lossy_level = None
        if self.lossy_enabled_var.get():
            if not self.has_gifsicle:
                raise CompressionError(
                    "gifsicle was not found. Install gifsicle or turn off lossy optimisation."
                )
            try:
                lossy_level = int(float(self.lossy_level_var.get()))
            except (TypeError, ValueError, OverflowError):
                raise CompressionError("Lossy level must be a whole number.")
            if not (0 <= lossy_level <= MAX_LOSSY_LEVEL):
                raise CompressionError(
                    "Lossy level must be between 0 and {}.".format(MAX_LOSSY_LEVEL)
                )

        dither = DITHER_OPTIONS.get(self.dither_var.get())
        if dither is None:
            raise CompressionError("Choose a valid dither option.")

        return CompressionSettings(
            max_colours=colours,
            dither=dither,
            keep_timing=keep_timing,
            fps=fps,
            scale=scale_percent / 100.0,
            optimize_diff=self.optimize_var.get(),
            target_bytes=target_bytes,
            lossy_level=lossy_level,
        )

    def compress(self):
        input_path = os.path.expanduser(self.input_var.get().strip())
        if not input_path:
            messagebox.showwarning("No input", "Choose a GIF to compress.")
            return
        if not os.path.isfile(input_path):
            messagebox.showerror(
                "Input not found", "File not found:\n{}".format(input_path)
            )
            return

        if self.source_info is None or os.path.abspath(
            self.source_info.path
        ) != os.path.abspath(input_path):
            self.load_source(input_path)
            if self.source_info is None or os.path.abspath(
                self.source_info.path
            ) != os.path.abspath(input_path):
                return

        try:
            settings = self._read_settings()
        except CompressionError as exc:
            messagebox.showerror("Invalid settings", str(exc))
            return

        output_path = os.path.expanduser(self.output_var.get().strip())
        if not output_path:
            output_path = self._default_output_path(input_path)
            self.output_var.set(output_path)
        if not output_path.lower().endswith(".gif"):
            output_path += ".gif"
            self.output_var.set(output_path)
        output_dir = os.path.dirname(os.path.abspath(output_path))
        if not os.path.isdir(output_dir):
            messagebox.showerror(
                "Output folder missing",
                "The output folder does not exist:\n{}".format(output_dir),
            )
            return
        if _same_file(input_path, output_path):
            if not messagebox.askyesno(
                "Overwrite source?",
                "The output is the same file as the input.\n\n"
                "Overwrite the source GIF?",
            ):
                return

        self.compress_button.configure(state="disabled")
        self.root.configure(cursor="watch")
        self.status_var.set("Preparing...")
        self.root.update_idletasks()

        def progress(scale, width, height):
            self.status_var.set(
                "Encoding at {:.0f}% ({} x {} px)...".format(
                    scale * 100, width, height
                )
            )
            self.root.update_idletasks()

        try:
            result = compress_gif(
                input_path, output_path, settings, progress=progress
            )
        except TargetNotMetError as exc:
            self.status_var.set("Target size could not be met.")
            messagebox.showwarning("Target not met", str(exc))
            return
        except CompressionError as exc:
            self.status_var.set("Compression failed.")
            messagebox.showerror("Compression failed", str(exc))
            return
        except Exception as exc:
            self.status_var.set("Compression failed.")
            messagebox.showerror(
                "Compression failed", "Unexpected error:\n{}".format(exc)
            )
            return
        finally:
            self.root.configure(cursor="")
            self.compress_button.configure(state="normal")

        if _same_file(input_path, result.output_path):
            # In-place compression replaced the input, so keep the app's view
            # of the source in sync for the next run.
            self.source_info = result.output
        summary = self._format_result(result)
        self.status_var.set(summary)
        self.preview_title_var.set("Output preview")
        self._show_preview(result.output_path)
        self._update_info(result.output)
        if result.output.size > result.source.size:
            messagebox.showwarning("Compression complete", summary)
        else:
            messagebox.showinfo("Compression complete", summary)

    def _format_result(self, result):
        source = result.source
        output = result.output
        if result.copied_source:
            target = (
                format_size(result.target_bytes)
                if result.target_bytes is not None
                else "target"
            )
            return "\n".join(
                [
                    "The source is already {}, at or below the {} target.".format(
                        format_size(source.size), target
                    ),
                    "No compression needed.",
                    "Output:\n{}".format(result.output_path),
                ]
            )

        lines = []
        if result.saved_bytes > 0:
            lines.append(
                "Compressed {} -> {} ({:.1f}% smaller).".format(
                    format_size(source.size),
                    format_size(output.size),
                    result.saved_percent,
                )
            )
        elif result.saved_bytes == 0:
            lines.append(
                "Compressed {} -> {} (same size).".format(
                    format_size(source.size), format_size(output.size)
                )
            )
        else:
            lines.append(
                "Output is {}, {:.1f}% larger than the source.".format(
                    format_size(output.size), abs(result.saved_percent)
                )
            )

        details = ["{} x {} px".format(output.width, output.height)]
        if output.is_animated:
            details.append("{} frames".format(output.frames))
            if output.duration_seconds > 0:
                details.append("{:.2f} s".format(output.duration_seconds))
            if output.fps > 0:
                details.append("{:.1f} fps".format(output.fps))
        else:
            details.append("still image")
        lines.append(", ".join(details))

        if result.target_bytes is not None:
            lines.append(
                "Target {}: {}.".format(
                    format_size(result.target_bytes),
                    "met" if result.target_met else "not met",
                )
            )
        if result.lossy_applied:
            lines.append("Lossy optimisation applied.")
        if not result.timing_patched:
            lines.append("Note: frame timing could not be matched exactly.")
        if result.saved_bytes < 0:
            lines.append(
                "Try fewer colours, a lower FPS, a smaller scale, or a target size."
            )
        lines.append("Saved to:\n{}".format(result.output_path))
        return "\n".join(lines)


def main():
    root = tk.Tk()
    CompressGifApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
