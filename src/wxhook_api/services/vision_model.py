"""Independent image-understanding profiles using the shared chat transport."""
from __future__ import annotations

import os
from pathlib import Path
from typing import Literal

from wxhook_api.core.config import PROJECT_ROOT
from wxhook_api.services.chat_model import ChatModelConfiguration, ModelDefinition


class VisionModelDefinition(ModelDefinition):
    supports_vision: Literal[True] = True


# A local, synthetic red square verifies that the endpoint accepts image input.
TEST_IMAGE = (
    "data:image/png;base64,"
    "iVBORw0KGgoAAAANSUhEUgAAACAAAAAgCAIAAAD8GO2jAAAANUlEQVR4nO3QsQ0AMAzDsLT//9yeoCkbeYAN6LzZ"
    "dZf3x0GSKEmUJEoSJYmSREmiJFGSaMoHo8QBPwYSAhsAAAAASUVORK5CYII="
)


class VisionModelConfiguration(ChatModelConfiguration):
    def __init__(self, chatbot, *, budget=None):
        path = Path(os.getenv("WXHOOK_VISION_MODEL_CONFIG") or PROJECT_ROOT / "data" / "vision-model.local.json")
        super().__init__(chatbot, path=path, provider_attribute="vision_provider", budget=budget)
        if any(not profile.supports_vision for profile in self.models.values()):
            self.models = {}
            self.active_id = ""
            self.fallback_ids = []
            chatbot.vision_provider = None
            self.load_error = "视觉模型配置须支持图片理解，请重新保存。"

    async def save_profile(self, requested: ModelDefinition, model_id: str | None = None) -> dict:
        requested = VisionModelDefinition.model_validate(requested.model_dump())
        return await super().save_profile(requested, model_id)

    async def _test_provider(self, provider) -> str:
        return await provider.answer("这是图片理解连接测试，请简短描述图片的颜色和内容。", image_data_url=TEST_IMAGE)
