"""Convert downloaded multi-frame images into playable H.264 MP4 files."""

from __future__ import annotations

import math
from pathlib import Path
import subprocess
from tempfile import TemporaryDirectory
import warnings

from PIL import Image


MAX_FRAMES = 1000
MAX_SECONDS = 180
MAX_TEMP_BYTES = 500 * 1024 * 1024
CONVERSION_TIMEOUT = 120


class MediaConversionError(Exception):
    pass


def animation_to_mp4(path: Path, *, max_bytes: int) -> Path | None:
    """Keep the source and publish a numbered MP4 only after full conversion.

    Static images return None. Decode WebP/APNG with Pillow as FFmpeg builds
    differ in their support for animated WebP. A local concat list preserves
    frame durations, without allowing the encoder to fetch remote resources.
    """
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(path) as image:
                frames = getattr(image, "n_frames", 1)
                first = 1 if image.info.get("default_image") else 0
                if frames - first <= 1:
                    return None
                if frames > MAX_FRAMES or image.width * image.height > 40_000_000:
                    raise MediaConversionError("动态图尺寸或帧数过大，已停止转视频，原文件仍保存在本机")
                if max_bytes <= 0:
                    raise MediaConversionError("动态图转视频超过作品大小限制，原文件仍保存在本机")
                try:
                    from imageio_ffmpeg import get_ffmpeg_exe
                    executable = get_ffmpeg_exe()
                except (ImportError, RuntimeError) as exc:
                    raise MediaConversionError("缺少动态图转视频依赖，请安装项目依赖后重启") from exc
                with TemporaryDirectory(prefix=".animation-", dir=path.parent) as temporary:
                    root = Path(temporary)
                    entries = []
                    seconds, disk_bytes = 0.0, 0
                    for index in range(first, frames):
                        image.seek(index)
                        # WebP updates its frame duration when decoding, not seeking.
                        frame = image.convert("RGBA")
                        duration = float(image.info.get("duration", 100) or 100) / 1000
                        if not math.isfinite(duration) or duration <= 0:
                            duration = 0.1
                        seconds += duration
                        if seconds > MAX_SECONDS:
                            raise MediaConversionError("动态图时长超过 3 分钟，已停止转视频，原文件仍保存在本机")
                        frame.thumbnail((1920, 1920))
                        background = Image.new("RGB", frame.size, "white")
                        background.paste(frame, mask=frame.getchannel("A"))
                        name = f"frame-{index:04d}.png"
                        frame_path = root / name
                        background.save(frame_path, format="PNG")
                        disk_bytes += frame_path.stat().st_size
                        if disk_bytes > MAX_TEMP_BYTES:
                            raise MediaConversionError("动态图转换临时文件超过大小限制，原文件仍保存在本机")
                        entries.extend([f"file '{name}'", "option framerate 1000", f"duration {duration:.6f}"])
                    # The final concat duration needs a following entry to take effect.
                    entries.extend([f"file '{name}'", "option framerate 1000"])
                    manifest = root / "frames.txt"
                    manifest.write_text("\n".join(entries) + "\n", encoding="ascii")
                    output = root / "output.mp4"
                    result = subprocess.run([
                        executable, "-hide_banner", "-loglevel", "error", "-nostdin", "-y",
                        "-protocol_whitelist", "file,pipe", "-f", "concat", "-safe", "0", "-i", str(manifest),
                        "-t", f"{seconds:.6f}", "-vf", "fps=30,pad=ceil(iw/2)*2:ceil(ih/2)*2,format=yuv420p",
                        "-c:v", "libx264", "-preset", "fast", "-crf", "20", "-threads", "1",
                        "-an", "-movflags", "+faststart", "-fs", str(max_bytes + 1),
                        "-progress", "pipe:1", "-nostats", str(output),
                    ], stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                        timeout=CONVERSION_TIMEOUT, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
                    if result.returncode or not output.is_file() or output.stat().st_size == 0:
                        raise MediaConversionError("动态图转视频失败，原文件仍保存在本机")
                    # FFmpeg can stop at -fs with a successful exit code. Never publish
                    # a truncated animation as a completed conversion.
                    progress = dict(line.split("=", 1) for line in result.stdout.decode("ascii", errors="ignore").splitlines() if "=" in line)
                    encoded_seconds = int(progress.get("out_time_us", "0")) / 1_000_000
                    if output.stat().st_size > max_bytes or encoded_seconds < seconds - 0.1:
                        raise MediaConversionError("动态图转视频超过大小限制或未完成，原文件仍保存在本机")
                    target = path.with_suffix(".mp4")
                    output.replace(target)
                    return target
    except MediaConversionError:
        raise
    except subprocess.TimeoutExpired as exc:
        raise MediaConversionError("动态图转视频超时，原文件仍保存在本机") from exc
    except Exception as exc:
        raise MediaConversionError("动态图读取或转视频失败，原文件仍保存在本机") from exc
