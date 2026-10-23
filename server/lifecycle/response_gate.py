"""Confirm controller responses only at the outer ASGI transport boundary.

BaseHTTPMiddleware buffers inner sends. An inner response callback therefore
cannot prove that its final body reached the server's send callable. Custody of
the one-use pipe remains with this pure ASGI wrapper until that outer send.
"""
from __future__ import annotations

import asyncio

from starlette.responses import JSONResponse


_CONTEXT = 'streamhome.controller_response_gate'


class _ResponseGate:
    def __init__(self, receive):
        self.receive = receive
        self.lock = asyncio.Lock()
        self.handoff = None
        self.observer = None
        self.disconnected = False
        self.finished = False
        self.confirmed = False
        self.status = None

    async def observed_receive(self):
        # Streaming middleware may also listen for a disconnect. Serialize reads
        # and record it whichever reader receives the transport event first.
        async with self.lock:
            try:
                message = await self.receive()
            except asyncio.CancelledError:
                raise
            except BaseException:
                if not self.finished:self.disconnected = True
                raise
            if message['type'] == 'http.disconnect' and not self.finished:
                self.disconnected = True
            return message

    async def observe(self):
        # Registration happens after the endpoint has completed its request
        # work. No subsequent request-body consumer depends on these messages.
        while not self.finished:
            message = await self.observed_receive()
            if message['type'] == 'http.disconnect':return
            if message['type'] != 'http.request':
                self.disconnected = True
                return
            await asyncio.sleep(0)

    def register(self, handoff):
        if self.handoff is not None:
            handoff.abort_response()
            raise RuntimeError('A controller response gate is already registered.')
        self.handoff = handoff
        self.observer = asyncio.create_task(self.observe())


class ControllerResponseGateMiddleware:
    """Install outside every middleware that can buffer or replace a response."""
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if (scope['type'] != 'http' or scope.get('method') != 'POST'
                or not scope.get('path', '').startswith('/api/backup/restore/')):
            await self.app(scope, receive, send)
            return
        context = _ResponseGate(receive)
        scope[_CONTEXT] = context

        async def observed_send(message):
            if context.handoff is not None:
                await asyncio.sleep(0)
                if context.disconnected:return
            if message['type'] == 'http.response.start':
                context.status = message['status']
            await send(message)
            if message['type'] == 'http.response.body' and not message.get('more_body', False):
                # Uvicorn returns disconnect after normal completion as well.
                # Set this immediately after its final send, without a yield.
                context.finished = True
                if context.handoff is not None and context.status == 202 and not context.disconnected:
                    context.handoff.release_after_response()
                    context.confirmed = True

        try:
            await self.app(scope, context.observed_receive, observed_send)
        finally:
            context.finished = True
            if context.handoff is not None and not context.confirmed:
                context.handoff.abort_response()
            if context.observer is not None:
                context.observer.cancel()
                await asyncio.gather(context.observer, return_exceptions=True)
            scope.pop(_CONTEXT, None)


class ControllerResponse(JSONResponse):
    def __init__(self, content, *, handoff, status_code=202):
        super().__init__(content=content, status_code=status_code)
        self.handoff = handoff

    async def __call__(self, scope, receive, send):
        context = scope.get(_CONTEXT)
        if not isinstance(context, _ResponseGate):
            self.handoff.abort_response()
            raise RuntimeError('The outer controller response middleware is required.')
        context.register(self.handoff)
        await super().__call__(scope, receive, send)
