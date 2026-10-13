from fastapi import APIRouter, Depends

from wxhook_api.clients.hook_client import HookClient
from wxhook_api.routes.dependencies import get_hook_client, hook_response, optional_body
from wxhook_api.schemas.requests import ExecuteQueryRequest
from wxhook_api.schemas.responses import DatabaseResponse, RetResponse, StatusResponse

router = APIRouter()


@router.post("/QueryDB/execute", responses={200: {"model": RetResponse}})
async def execute_query(body: ExecuteQueryRequest, client: HookClient = Depends(get_hook_client)):
    return hook_response(await client.execute_query(body))


@router.post("/QueryDB/GetAllDBName", response_model=None, responses={200: {"model": list[DatabaseResponse]}})
async def get_all_db_names(_: dict = Depends(optional_body), client: HookClient = Depends(get_hook_client)):
    return hook_response(await client.get_all_db_names())


@router.get("/QueryDB/status", response_model=None, responses={200: {"model": StatusResponse}})
async def get_status(client: HookClient = Depends(get_hook_client)):
    return hook_response(await client.get_status())
