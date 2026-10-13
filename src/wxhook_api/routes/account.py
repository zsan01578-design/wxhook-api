from fastapi import APIRouter, Depends

from wxhook_api.clients.hook_client import HookClient
from wxhook_api.routes.dependencies import get_hook_client, hook_response, optional_body
from wxhook_api.schemas.responses import FileSavePathResponse, PBResponse, RetResponse

router = APIRouter()


@router.get("/GetFileSavePath", responses={200: {"model": FileSavePathResponse}})
async def get_file_save_path(client: HookClient = Depends(get_hook_client)):
    return hook_response(await client.get_file_save_path())


@router.post("/GetDecodeKey", responses={200: {"model": RetResponse}})
async def get_decode_key(_: dict = Depends(optional_body), client: HookClient = Depends(get_hook_client)):
    return hook_response(await client.get_decode_key())


@router.post("/GetSelfProfile", responses={200: {"model": PBResponse}})
async def get_self_profile(_: dict = Depends(optional_body), client: HookClient = Depends(get_hook_client)):
    return hook_response(await client.get_self_profile())
