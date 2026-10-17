from __future__ import annotations

import asyncio
from pathlib import Path
import warnings

from wxhook_api.clients.hook_client import HookClient, HookResponse
from wxhook_api.core.errors import HookTransportError
from wxhook_api.schemas.requests import FileMessageRequest, ImageMessageRequest, TextMessageRequest


class DeliveryError(Exception):
    def __init__(self, message: str, *, uncertain: bool = False):
        super().__init__(message)
        self.uncertain = uncertain


def _confirmed(response: HookResponse) -> None:
    data = response.data
    if 400 <= response.status_code < 500:
        raise DeliveryError(f"Hook 拒绝发送（HTTP {response.status_code}）")
    if response.status_code != 200:
        raise DeliveryError(f"发送结果未确认（HTTP {response.status_code}），请先查看群聊", uncertain=True)
    if not isinstance(data, dict) or type(data.get("ret")) not in {int, str}:
        raise DeliveryError("Hook 未返回明确的发送结果，请先查看群聊", uncertain=True)
    if data["ret"] not in (0, "0"):
        raise DeliveryError("Hook 返回发送失败，请检查微信登录状态和文件是否可用")


def prepare_image(path: Path) -> tuple[Path, str]:
    """Keep original downloads; make PNG copies for less compatible formats."""
    try:
        from PIL import Image, ImageOps

        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(path) as image:
                if image.width * image.height > 40_000_000:
                    raise DeliveryError("图片尺寸过大，已停止自动回传")
                if getattr(image, "n_frames", 1) > 1:
                    return path, "file"  # Preserve animation instead of flattening it.
                if path.suffix.lower() in {".jpg", ".jpeg", ".png"}:
                    return path, "image"
                image = ImageOps.exif_transpose(image)
                converted = image.convert("RGBA" if "A" in image.getbands() else "RGB")
                target = path.with_name(f"send-{path.stem}.png")
                partial = target.with_suffix(".png.part")
                try:
                    converted.save(partial, format="PNG")
                    partial.replace(target)
                finally:
                    partial.unlink(missing_ok=True)
                return target, "image"
    except ImportError as exc:
        raise DeliveryError("缺少图片转换依赖，请在测试机执行 python -m pip install -e . 后重启") from exc
    except DeliveryError:
        raise
    except Exception as exc:
        raise DeliveryError("图片格式转换失败，文件已保存在本机，未发送该图片") from exc


class DouyinMessenger:
    def __init__(self, client: HookClient):
        self.client = client

    async def text(self, group: str, message: str, *, atlist: list[str] | None = None) -> None:
        try:
            response = await self.client.send_text(TextMessageRequest(wxidorgid=group, msg=message, atlist=atlist))
        except HookTransportError as exc:
            raise DeliveryError("群提示发送结果未确认，请检查微信 Hook", uncertain=True) from exc
        _confirmed(response)

    async def prepare(self, path: Path, kind: str) -> tuple[Path, str]:
        path = path.resolve()
        if not path.is_file():
            raise DeliveryError("本地媒体文件不存在，未发送")
        return await asyncio.to_thread(prepare_image, path) if kind == "image" else (path, "file")

    async def send(self, group: str, path: Path, mode: str) -> None:
        try:
            if mode == "image":
                response = await self.client.send_image(ImageMessageRequest(wxidorgid=group, path=str(path)))
            else:
                response = await self.client.send_file(FileMessageRequest(wxid=group, filepath=str(path)))
        except HookTransportError as exc:
            raise DeliveryError("媒体发送结果未确认，可能已发出；已停止后续回传，请先查看群聊", uncertain=True) from exc
        _confirmed(response)
