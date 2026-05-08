"""WebSocket 路由。"""

from fastapi import APIRouter, Depends, WebSocket, WebSocketDisconnect
from sqlalchemy.ext.asyncio import AsyncSession

from smart_badge_api.api.deps import get_user_from_token, get_websocket_token
from smart_badge_api.api.ws_hub import task_hub
from smart_badge_api.db.session import get_db

router = APIRouter()

WS_UNAUTHORIZED_CODE = 4401


@router.websocket("/ws/tasks")
async def ws_tasks(ws: WebSocket, db: AsyncSession = Depends(get_db)):
    token = get_websocket_token(ws)
    user = await get_user_from_token(token, db) if token else None
    if user is None:
        await ws.accept()
        await ws.close(code=WS_UNAUTHORIZED_CODE, reason="Unauthorized")
        return

    await task_hub.connect(ws)
    try:
        while True:
            await ws.receive_text()
    except WebSocketDisconnect:
        pass
    finally:
        task_hub.disconnect(ws)
