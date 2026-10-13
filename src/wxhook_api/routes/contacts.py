from fastapi import APIRouter, Depends

from wxhook_api.clients.hook_client import HookClient
from wxhook_api.routes.dependencies import get_hook_client, hook_response
from wxhook_api.schemas.requests import ContactSearchRequest

router = APIRouter()


@router.post("/Contact/search", response_model=None, responses={200: {"description": "Hook JSON response, forwarded unchanged"}})
async def search_contacts(body: ContactSearchRequest, client: HookClient = Depends(get_hook_client)):
    return hook_response(await client.search_contacts(body))
