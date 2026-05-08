"""WebSocket 连接管理 — 向所有在线客户端广播任务进度事件。"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

from fastapi import WebSocket

logger = logging.getLogger(__name__)

# 每个连接的发送队列容量；满则丢弃最旧的非关键消息，避免慢客户端拖垮广播。
_PER_CLIENT_QUEUE_SIZE = 256


class _ClientChannel:
    __slots__ = ("ws", "queue", "writer_task", "closed")

    def __init__(self, ws: WebSocket) -> None:
        self.ws = ws
        self.queue: asyncio.Queue[str] = asyncio.Queue(maxsize=_PER_CLIENT_QUEUE_SIZE)
        self.writer_task: asyncio.Task | None = None
        self.closed: bool = False


class TaskHub:
    """进程内 WebSocket 广播中心。每个连接独立队列 + 写者任务，慢客户端不阻塞广播。"""

    def __init__(self) -> None:
        self._clients: dict[WebSocket, _ClientChannel] = {}

    async def connect(self, ws: WebSocket) -> None:
        await ws.accept()
        channel = _ClientChannel(ws)
        channel.writer_task = asyncio.create_task(self._writer_loop(channel))
        self._clients[ws] = channel
        logger.info("WS client connected (%d total)", len(self._clients))

    def disconnect(self, ws: WebSocket) -> None:
        channel = self._clients.pop(ws, None)
        if channel is not None:
            channel.closed = True
            if channel.writer_task is not None and not channel.writer_task.done():
                channel.writer_task.cancel()
        logger.info("WS client disconnected (%d total)", len(self._clients))

    async def _writer_loop(self, channel: _ClientChannel) -> None:
        try:
            while not channel.closed:
                payload = await channel.queue.get()
                try:
                    await channel.ws.send_text(payload)
                except Exception:
                    channel.closed = True
                    return
        except asyncio.CancelledError:
            return

    async def broadcast(self, event: str, data: dict[str, Any]) -> None:
        """向所有在线客户端广播一条 JSON 消息（非阻塞投递）。"""
        if not self._clients:
            return
        payload = json.dumps({"event": event, "data": data}, ensure_ascii=False)
        for ws, channel in list(self._clients.items()):
            if channel.closed:
                continue
            try:
                channel.queue.put_nowait(payload)
            except asyncio.QueueFull:
                # 慢客户端：丢弃最旧一条再投递，确保新事件优先。
                try:
                    _ = channel.queue.get_nowait()
                    channel.queue.task_done()
                except asyncio.QueueEmpty:
                    pass
                try:
                    channel.queue.put_nowait(payload)
                except asyncio.QueueFull:
                    logger.warning("WS client queue full after drop, skip event=%s", event)


# 全局单例
task_hub = TaskHub()
