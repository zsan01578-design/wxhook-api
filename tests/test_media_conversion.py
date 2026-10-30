from io import BytesIO
import subprocess

from imageio_ffmpeg import read_frames
from PIL import Image
import pytest

from tests.test_douyin_delivery import scenario
from wxhook_api.services import media_conversion
from wxhook_api.services.media_conversion import MediaConversionError, animation_to_mp4


def animation(format):
    output = BytesIO()
    frames = [Image.new("RGB", (7, 5), color) for color in ("red", "lime", "blue")]
    frames[0].save(output, format=format, save_all=True, append_images=frames[1:],
                   duration=[100, 200, 300], loop=0, lossless=True)
    return output.getvalue()


@pytest.mark.parametrize("format,suffix", [("GIF", ".gif"), ("WEBP", ".webp"), ("PNG", ".png")])
def test_real_animation_encodes_all_frames_as_h264_with_original_timing(tmp_path, format, suffix):
    source = tmp_path / ("001" + suffix)
    original = animation(format)
    source.write_bytes(original)
    converted = animation_to_mp4(source, max_bytes=1024 * 1024)
    assert converted == tmp_path / "001.mp4"
    assert source.read_bytes() == original
    reader = read_frames(str(converted))
    try:
        meta = next(reader)
        frames = list(reader)
    finally:
        reader.close()
    assert meta["codec"] == "h264"
    assert meta["size"] == (8, 6)  # Odd input dimensions need padding for yuv420p.
    assert meta["fps"] == 30
    assert meta["duration"] == pytest.approx(0.6, abs=0.05)
    assert len(frames) == 18
    # Decode the actual output: three red, six green and nine blue frames.
    assert [max(range(3), key=lambda channel: frame[channel]) for frame in frames] == [0] * 3 + [1] * 6 + [2] * 9
    assert not list(tmp_path.glob(".animation-*"))


def test_static_image_does_not_require_encoder(tmp_path, monkeypatch):
    import imageio_ffmpeg
    monkeypatch.setattr(imageio_ffmpeg, "get_ffmpeg_exe", lambda: pytest.fail("Static images need no encoder"))
    source = tmp_path / "001.png"
    Image.new("RGB", (4, 4), "red").save(source)
    assert animation_to_mp4(source, max_bytes=1000) is None


def test_missing_encoder_is_actionable_and_keeps_source(tmp_path, monkeypatch):
    import imageio_ffmpeg

    def missing():
        raise RuntimeError("No executable")

    monkeypatch.setattr(imageio_ffmpeg, "get_ffmpeg_exe", missing)
    source = tmp_path / "001.gif"
    source.write_bytes(animation("GIF"))
    with pytest.raises(MediaConversionError, match="依赖"):
        animation_to_mp4(source, max_bytes=10000)
    assert source.exists() and not source.with_suffix(".mp4").exists()


def test_conversion_timeout_removes_partial_video_and_frames(tmp_path, monkeypatch):
    def timeout(*args, **kwargs):
        from pathlib import Path
        Path(args[0][-1]).write_bytes(b"partial")
        raise subprocess.TimeoutExpired("ffmpeg", 120)

    monkeypatch.setattr(media_conversion.subprocess, "run", timeout)
    source = tmp_path / "001.gif"
    source.write_bytes(animation("GIF"))
    with pytest.raises(MediaConversionError, match="超时"):
        animation_to_mp4(source, max_bytes=10000)
    assert source.exists() and list(tmp_path.iterdir()) == [source]


def test_output_limit_does_not_publish_a_truncated_mp4(tmp_path):
    source = tmp_path / "001.gif"
    source.write_bytes(animation("GIF"))
    with pytest.raises(MediaConversionError):
        animation_to_mp4(source, max_bytes=100)
    assert source.exists() and list(tmp_path.iterdir()) == [source]


@pytest.mark.parametrize("format,suffix", [("GIF", ".gif"), ("WEBP", ".webp"), ("PNG", ".png")])
async def test_animation_is_downloaded_as_video_served_and_returned_with_cache(tmp_path, monkeypatch, format, suffix):
    async with scenario(tmp_path, monkeypatch, images=1, media=animation(format)) as context:
        for msgid in (1, 2):
            job = await context.submit(msgid=msgid)
            assert job["status"] == "completed", job["error"]
            assert job["delivery_status"] == "sent"
            assert bool(job.get("cache_hit")) == (msgid == 2)
            assert [file["name"] for file in job["files"]] == ["001.mp4"]
            assert job["files"][0]["kind"] == "video"
            response = await context.client.get(f"/api/douyin/files/{job['id']}/001.mp4")
            assert response.status_code == 200 and response.headers["content-type"].startswith("video/mp4")
            assert response.content[4:8] == b"ftyp"
            if msgid == 1:
                assert (context.service.options.output_dir / job["id"] / ("001" + suffix)).exists()
        assert [path for path, _ in context.hook_calls] == ["/SendTextMsg", "/send_file_msg"] * 2


async def test_failed_conversion_preserves_download_without_sending_static_preview(tmp_path, monkeypatch):
    def fail(*args, **kwargs):
        raise MediaConversionError("动态图转视频失败，原文件仍保存在本机")

    monkeypatch.setattr("wxhook_api.services.douyin.animation_to_mp4", fail)
    async with scenario(tmp_path, monkeypatch, images=1, media=animation("GIF")) as context:
        job = await context.submit()
        assert job["status"] == "failed" and "转视频失败" in job["error"]
        assert [file["name"] for file in job["files"]] == ["001.gif"]
        assert context.service.file_path(job["id"], "001.gif").is_file()
        assert not any(path in {"/SendImgMsg", "/send_file_msg"} for path, _ in context.hook_calls)
