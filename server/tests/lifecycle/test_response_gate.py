import asyncio
import os
import select
import socket
import struct
import threading
import time
import unittest
import uuid

from starlette.background import BackgroundTask
from starlette.responses import JSONResponse

from lifecycle.response_gate import ControllerResponse as _ControllerResponse, ControllerResponseGateMiddleware


def ControllerResponse(*args, **kwargs):
    return ControllerResponseGateMiddleware(_ControllerResponse(*args, **kwargs))
from lifecycle.restore_controller import RestoreHandoff


class ResponseGateTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.read,self.write=os.pipe()
        self.addCleanup(os.close,self.read)
        self.handoff=RestoreHandoff(None,str(uuid.uuid4()),1,self.write)
        self.addCleanup(lambda:self.handoff.abort_response() if not self.handoff._released else None)
        self.scope={'type':'http','method':'POST','path':'/api/backup/restore/test.db',
                    'asgi':{'version':'3.0','spec_version':'2.3'}}

    async def test_old_background_pattern_reproduces_approval_despite_known_disconnect(self):
        async def receive():return {'type':'http.disconnect'}
        async def send(message):pass  # Uvicorn's disconnected send returns normally.
        old=JSONResponse({'status':'accepted'},status_code=202,background=BackgroundTask(self.handoff.release_after_response))
        await old(self.scope,receive,send)
        self.assertEqual(os.read(self.read,1),b'1')

    async def test_known_disconnect_closes_gate_without_authorization_or_response(self):
        messages=[]
        async def receive():return {'type':'http.disconnect'}
        async def send(message):messages.append(message)
        await ControllerResponse({'status':'accepted'},handoff=self.handoff)(self.scope,receive,send)
        self.assertEqual(messages,[])
        self.assertEqual(os.read(self.read,1),b'')

    async def test_successful_final_body_precedes_the_one_use_authorization_byte(self):
        messages=[]
        async def receive():await asyncio.Event().wait()
        async def send(message):
            self.assertFalse(select.select([self.read],[],[],0)[0])
            messages.append(message)
        await ControllerResponse({'status':'accepted'},handoff=self.handoff)(self.scope,receive,send)
        self.assertEqual([m['type'] for m in messages],['http.response.start','http.response.body'])
        self.assertEqual(os.read(self.read,2),b'1')

    async def test_disconnect_after_headers_never_sends_body_or_releases_gate(self):
        gone=asyncio.Event();messages=[]
        async def receive():await gone.wait();return {'type':'http.disconnect'}
        async def send(message):messages.append(message);gone.set()
        await ControllerResponse({'status':'accepted'},handoff=self.handoff)(self.scope,receive,send)
        self.assertEqual([m['type'] for m in messages],['http.response.start'])
        self.assertEqual(os.read(self.read,1),b'')

    async def test_send_error_and_cancellation_close_unconfirmed_gate(self):
        async def receive():await asyncio.Event().wait()
        async def send(message):raise OSError('disconnected transport')
        with self.assertRaises(OSError):
            await ControllerResponse({},handoff=self.handoff)(self.scope,receive,send)
        self.assertEqual(os.read(self.read,1),b'')

    async def test_canceled_response_releases_watcher_and_pipe(self):
        sending=asyncio.Event()
        async def receive():await asyncio.Event().wait()
        async def send(message):sending.set();await asyncio.Event().wait()
        task=asyncio.create_task(ControllerResponse({},handoff=self.handoff)(self.scope,receive,send))
        await asyncio.wait_for(sending.wait(),2);task.cancel()
        with self.assertRaises(asyncio.CancelledError):await task
        self.assertEqual(os.read(self.read,1),b'')

    async def test_normal_server_end_after_final_body_is_not_a_client_disconnect(self):
        response_done=asyncio.Event()
        async def receive():await response_done.wait();return {'type':'http.disconnect'}
        async def send(message):
            if message['type']=='http.response.body':response_done.set()
        await ControllerResponse({},handoff=self.handoff)(self.scope,receive,send)
        self.assertEqual(os.read(self.read,1),b'1')

    async def test_buffered_response_waits_for_outer_final_send(self):
        from starlette.middleware.base import BaseHTTPMiddleware
        async def dispatch(request,call_next):return await call_next(request)
        app=_ControllerResponse({'status':'accepted'},handoff=self.handoff)
        for _ in range(5):app=BaseHTTPMiddleware(app,dispatch=dispatch)
        messages=[]
        async def receive():await asyncio.Event().wait()
        async def send(message):
            self.assertFalse(select.select([self.read],[],[],0)[0])
            messages.append(message)
        await ControllerResponseGateMiddleware(app)(self.scope,receive,send)
        self.assertFalse(messages[-1].get('more_body',False))
        self.assertEqual(os.read(self.read,1),b'1')

    async def test_outer_status_replacement_cannot_confirm_inner_202(self):
        async def inner(scope,receive,send):
            async def replaced(message):
                if message['type']=='http.response.start':message={**message,'status':503}
                await send(message)
            await _ControllerResponse({},handoff=self.handoff)(scope,receive,replaced)
        async def receive():await asyncio.Event().wait()
        async def send(message):pass
        await ControllerResponseGateMiddleware(inner)(self.scope,receive,send)
        self.assertEqual(os.read(self.read,1),b'')

    async def test_missing_outer_middleware_closes_gate_and_refuses_confirmation(self):
        async def receive():await asyncio.Event().wait()
        async def send(message):self.fail('Unprotected response must not be sent')
        with self.assertRaisesRegex(RuntimeError,'outer controller response middleware'):
            await _ControllerResponse({},handoff=self.handoff)(self.scope,receive,send)
        self.assertEqual(os.read(self.read,1),b'')


class NativeDisconnectedResponseTests(unittest.TestCase):
    def run_disconnected(self,*,old,buffered=False):
        import uvicorn
        read,write=os.pipe();handoff=RestoreHandoff(None,str(uuid.uuid4()),1,write)
        request_seen=threading.Event();allow_response=threading.Event()
        async def app(scope,receive,send):
            self.assertEqual(scope['type'],'http')
            await receive();request_seen.set()
            assert await asyncio.to_thread(allow_response.wait,5)
            response=(JSONResponse({},status_code=202,background=BackgroundTask(handoff.release_after_response))
                      if old else _ControllerResponse({},handoff=handoff))
            await response(scope,receive,send)
        if buffered:
            from starlette.middleware.base import BaseHTTPMiddleware
            async def dispatch(request,call_next):return await call_next(request)
            for _ in range(5):app=BaseHTTPMiddleware(app,dispatch=dispatch)
        if not old:app=ControllerResponseGateMiddleware(app)
        listener=socket.socket();listener.bind(('127.0.0.1',0));listener.listen(4)
        server=uvicorn.Server(uvicorn.Config(app,loop='asyncio',http='h11',lifespan='off',log_level='critical'))
        thread=threading.Thread(target=lambda:server.run(sockets=[listener]),daemon=True);thread.start()
        try:
            with socket.create_connection(listener.getsockname(),timeout=5) as client:
                client.sendall(b'POST /api/backup/restore/test.db HTTP/1.1\r\nHost: localhost\r\nContent-Length: 0\r\n\r\n')
                self.assertTrue(request_seen.wait(5))
                cycles=[connection.cycle for connection in server.server_state.connections]
                self.assertTrue(cycles and all(cycle is not None for cycle in cycles))
                client.setsockopt(socket.SOL_SOCKET,socket.SO_LINGER,struct.pack('ii',1,0))
            # Observe actual EOF from the server, rather than guessing that a
            # socket close has propagated before allowing its response task.
            deadline=time.monotonic()+5
            while not all(cycle.disconnected for cycle in cycles):
                if time.monotonic()>deadline:self.fail('Native HTTP disconnect was not observed')
                time.sleep(.005)
            allow_response.set()
            self.assertTrue(select.select([read],[],[],5)[0])
            return os.read(read,1)
        finally:
            allow_response.set();server.should_exit=True;thread.join(5)
            if not handoff._released:handoff.abort_response()
            os.close(read);listener.close()
            self.assertFalse(thread.is_alive())

    def test_actual_tcp_disconnect_reproduces_old_callback_bug(self):
        self.assertEqual(self.run_disconnected(old=True),b'1')

    def test_actual_tcp_disconnect_closes_the_new_response_gate(self):
        self.assertEqual(self.run_disconnected(old=False),b'')

    def test_actual_tcp_disconnect_through_five_buffering_middlewares(self):
        self.assertEqual(self.run_disconnected(old=False,buffered=True),b'')

    def test_buffered_old_callback_still_reproduces_unconfirmed_authorization(self):
        self.assertEqual(self.run_disconnected(old=True,buffered=True),b'1')


if __name__=='__main__':unittest.main()
