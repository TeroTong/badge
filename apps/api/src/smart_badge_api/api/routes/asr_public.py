from __future__ import annotations

import mimetypes

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse

from smart_badge_api.asr.tencent_media_proxy import resolve_tencent_media_token

router = APIRouter(prefix="/asr", tags=["ASR"])


@router.api_route("/tencent-media", methods=["GET", "HEAD"])
async def get_tencent_media(token: str):
    try:
        audio_path, filename = resolve_tencent_media_token(token)
    except ValueError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc

    if not audio_path.is_file():
        raise HTTPException(status_code=404, detail="音频文件不存在")

    media_type = mimetypes.guess_type(audio_path.name)[0] or "application/octet-stream"
    return FileResponse(audio_path, media_type=media_type, filename=filename or audio_path.name)
