from fastapi import APIRouter, Depends

from wxhook_api.clients.hook_client import HookClient
from wxhook_api.routes.dependencies import get_hook_client, hook_response
from wxhook_api.schemas.requests import DecodePictureRequest, MessagePictureRequest
from wxhook_api.schemas.responses import RetResponse

router = APIRouter()


@router.post("/GetMessagePic", responses={200: {"model": RetResponse}})
async def get_message_pic(body: MessagePictureRequest, client: HookClient = Depends(get_hook_client)):
    return hook_response(await client.get_message_pic(body))


@router.post("/Decode_Pic", responses={200: {"model": RetResponse}})
async def decode_picture(body: DecodePictureRequest, client: HookClient = Depends(get_hook_client)):
    return hook_response(await client.decode_picture(body))
