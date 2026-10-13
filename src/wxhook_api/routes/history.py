from fastapi import APIRouter, Depends

from wxhook_api.clients.hook_client import HookClient
from wxhook_api.routes.dependencies import get_hook_client, hook_response
from wxhook_api.schemas.requests import HistoryRequest

router = APIRouter()


@router.post("/Message/history", response_model=None, responses={200: {"description": "Hook JSON response, forwarded unchanged"}})
async def get_history(body: HistoryRequest, client: HookClient = Depends(get_hook_client)):
    return hook_response(await client.get_history(body))
