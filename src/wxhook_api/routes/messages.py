from fastapi import APIRouter, Depends

from wxhook_api.clients.hook_client import HookClient
from wxhook_api.routes.dependencies import get_hook_client, hook_response
from wxhook_api.schemas.requests import FileMessageRequest, ImageMessageRequest, TextMessageRequest, VoiceMessageRequest
from wxhook_api.schemas.responses import RetResponse

router = APIRouter()


@router.post("/SendVoiceMsg", responses={200: {"model": RetResponse}})
async def send_voice(body: VoiceMessageRequest, client: HookClient = Depends(get_hook_client)):
    return hook_response(await client.send_voice(body))


@router.post("/SendTextMsg", responses={200: {"model": RetResponse}})
async def send_text(body: TextMessageRequest, client: HookClient = Depends(get_hook_client)):
    if not body.wxidorgid:
        return {"ret": -1, "retmsg": "wxidorgid is empty"}
    return hook_response(await client.send_text(body))


@router.post("/SendImgMsg", responses={200: {"model": RetResponse}})
async def send_image(body: ImageMessageRequest, client: HookClient = Depends(get_hook_client)):
    return hook_response(await client.send_image(body))


@router.post("/send_file_msg", responses={200: {"model": RetResponse}})
async def send_file(body: FileMessageRequest, client: HookClient = Depends(get_hook_client)):
    return hook_response(await client.send_file(body))
