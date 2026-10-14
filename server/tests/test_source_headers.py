"""Run only through server/tests/run_isolated.py; no live state or providers."""
import asyncio
from contextlib import ExitStack
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import AsyncMock, patch

from fastapi import FastAPI, HTTPException
import httpx
from sqlalchemy.ext.asyncio import create_async_engine
from sqlmodel import SQLModel, select
from sqlmodel.ext.asyncio.session import AsyncSession
from starlette.requests import Request

from db import get_session
from models import DownloadAddRequest, DownloadTask, IntegrationCredential, Movie
from routes import queue as api
from services import queue as worker
from services.ffmpeg import download_and_merge
from services.media_probe import probe_media_stream
from services.ingestion_headers import encode_source_headers, decode_source_headers
from services.integration_auth import integration_token_hash
from services.ingestion_errors import IngestionFailure
from services.logger import redact_diagnostic
from scratch.test_ffmpeg_headers import _create_separate_disguised_hls_fixtures, _probe_stream_types

VIDEO={'Authorization':'Bearer source-video-only'}
AUDIO={'Cookie':'session=source-audio-only'}
SUBTITLE={'Authorization':'Bearer source-subtitle-only'}

class SourceServer(ThreadingHTTPServer):
    daemon_threads=True
    def __init__(self, root, expected):
        self.root=root;self.expected=expected;self.seen=[];self.leaks=[]
        super().__init__(('127.0.0.1',0), SourceHandler)
    @property
    def base(self):return f'http://127.0.0.1:{self.server_address[1]}'

class SourceHandler(BaseHTTPRequestHandler):
    def log_message(self,*args):pass
    def do_HEAD(self):self.serve(False)
    def do_GET(self):
        try:self.serve(True)
        except (BrokenPipeError, ConnectionResetError):pass
    def serve(self, body):
        selected={key:self.headers.get(key) for key in ['Authorization','Cookie'] if self.headers.get(key)}
        if selected!=self.server.expected:
            self.server.leaks.append(selected);self.send_response(403);self.send_header('Content-Length','0');self.end_headers();return
        file=(self.server.root/self.path.lstrip('/')).resolve()
        if not file.is_relative_to(self.server.root) or not file.is_file():self.send_error(404);return
        self.server.seen.append(self.path);data=file.read_bytes()
        self.send_response(200);self.send_header('Content-Length',str(len(data)));self.send_header('Connection','close')
        self.send_header('Content-Type','text/plain' if file.suffix in ['.txt','.vtt'] else 'video/mp2t');self.end_headers()
        if body:self.wfile.write(data)

class HeaderStorageTests(unittest.TestCase):
    def test_legacy_flat_headers_and_new_pairs_survive_task_restart(self):
        task=DownloadTask(headers_str=json.dumps(VIDEO));self.assertEqual(task.headers,VIDEO);self.assertEqual(task.audio_headers,VIDEO)
        task=DownloadTask(headers_str=encode_source_headers(VIDEO,{}));self.assertEqual(task.headers,VIDEO);self.assertEqual(task.audio_headers,{})
        self.assertEqual(decode_source_headers(encode_source_headers(VIDEO,AUDIO),'audio'),AUDIO)
    def test_unknown_storage_version_is_not_forwarded_as_a_header(self):
        with self.assertRaises(ValueError):decode_source_headers('{"__streamhome_source_headers__":2}')
    def test_independent_authorization_values_remain_redacted_in_nested_storage(self):
        raw=json.dumps({'headers_str':encode_source_headers(VIDEO,AUDIO),'subtitles_str':json.dumps([{'headers':SUBTITLE}])})
        redacted=redact_diagnostic(raw)
        for value in ['source-video-only','source-audio-only','source-subtitle-only']:self.assertNotIn(value,redacted)

class SourceApiTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.engine=create_async_engine('sqlite+aiosqlite:///:memory:')
        async with self.engine.begin() as c:await c.run_sync(SQLModel.metadata.create_all)
        self.app=FastAPI();self.app.include_router(api.router)
        async def session():
            async with AsyncSession(self.engine) as db:yield db
        self.app.dependency_overrides[get_session]=session
        async with AsyncSession(self.engine) as db:
            for name,scopes in [('allowed',['ingest']),('reader',['downloads:read'])]:
                credential=IntegrationCredential(id=name,name=name,token_hash=integration_token_hash(name),token_hint='synthetic',scopes_str=json.dumps(scopes),created_at=time.time(),last_used_at=time.time())
                db.add(credential)
            await db.commit()
        self.client=httpx.AsyncClient(transport=httpx.ASGITransport(app=self.app),base_url='http://127.0.0.1')
    async def asyncTearDown(self):await self.client.aclose();await self.engine.dispose()
    async def test_capability_requires_ingest_scope_and_does_not_create_a_task(self):
        self.assertEqual((await self.client.get('/api/mediasender/capabilities')).status_code,401)
        self.assertEqual((await self.client.get('/api/mediasender/capabilities',headers={'Authorization':'Bearer reader'})).status_code,403)
        response=await self.client.get('/api/mediasender/capabilities',headers={'Authorization':'Bearer allowed'})
        self.assertEqual(response.status_code,200);self.assertEqual(response.json(),{'ingestion':{'source_headers':1}})
        async with AsyncSession(self.engine) as db:self.assertEqual((await db.exec(select(DownloadTask))).all(),[])
    async def test_api_persists_each_set_and_explicit_empty_overrides_shared(self):
        payload={'tmdb_id':123,'media_type':'movie','video_url':'http://127.0.0.1:8123/video','audio_url':'http://127.0.0.1:8124/audio',
            'headers':VIDEO,'video_headers':VIDEO,'audio_headers':{},'subtitles':[{'language':'tr','url':'http://127.0.0.1:8125/sub.vtt','headers':SUBTITLE}]}
        with patch.object(api,'engine',self.engine),patch.object(api.tmdb_client,'fetch_movie_metadata',AsyncMock(return_value={'title':'Synthetic'})):
            response=await self.client.post('/api/add-movie',json=payload,headers={'Authorization':'Bearer allowed'})
        self.assertEqual(response.status_code,201)
        async with AsyncSession(self.engine) as db:
            task=await db.get(DownloadTask,response.json()['taskId']);self.assertEqual(task.headers,VIDEO);self.assertEqual(task.audio_headers,{})
            self.assertEqual(task.subtitles[0]['headers'],SUBTITLE)
            movie=await db.get(Movie,'m_123');self.assertEqual(movie.subtitles,[])
        for value in ['source-video-only','source-subtitle-only']:self.assertNotIn(value,response.text)
    async def test_legacy_api_keeps_shared_headers_and_bad_sidecar_headers_create_no_task(self):
        body={'tmdb_id':124,'media_type':'movie','video_url':'http://127.0.0.1:8123/video','headers':VIDEO,'subtitles':[{'language':'en','url':'http://127.0.0.1:8125/sub.vtt'}]}
        with patch.object(api,'engine',self.engine),patch.object(api.tmdb_client,'fetch_movie_metadata',AsyncMock(return_value={'title':'Synthetic'})):
            response=await self.client.post('/api/add-movie',json=body,headers={'Authorization':'Bearer allowed'})
            self.assertEqual(response.status_code,201)
            async with AsyncSession(self.engine) as db:
                task=await db.get(DownloadTask,response.json()['taskId']);self.assertEqual(task.headers,VIDEO);self.assertEqual(task.audio_headers,VIDEO);self.assertEqual(task.subtitles[0]['headers'],VIDEO)
            for bad in [{'Authorization':'bad\r\nCookie: injected'},{'X-Internal':'private'}]:
                body['subtitles'][0]['headers']=bad
                self.assertEqual((await self.client.post('/api/add-movie',json=body,headers={'Authorization':'Bearer allowed'})).status_code,400)
        async with AsyncSession(self.engine) as db:self.assertEqual(len((await db.exec(select(DownloadTask))).all()),1)

class SourceMediaTests(unittest.TestCase):
    def test_real_probe_merge_and_subtitle_queue_use_independent_credentials(self):
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary);video,audio=_create_separate_disguised_hls_fixtures(root)
            (root/'tr.vtt').write_text('WEBVTT\n\n00:00:00.000 --> 00:00:00.500\nSynthetic subtitle.\n')
            servers=[SourceServer(root,h) for h in [VIDEO,AUDIO,SUBTITLE]]
            threads=[threading.Thread(target=s.serve_forever,daemon=True) for s in servers]
            for t in threads:t.start()
            try:
                vurl=servers[0].base+'/'+str(video.relative_to(root));aurl=servers[1].base+'/'+str(audio.relative_to(root));surl=servers[2].base+'/tr.vtt'
                async def run():
                    probe=await probe_media_stream(vurl,aurl,VIDEO,audio_headers=AUDIO,client_address='127.0.0.1')
                    self.assertTrue(probe['has_video']);self.assertTrue(probe['has_audio']);self.assertIsNone(probe.get('failure'))
                    output=root/'merged.mp4';success,failure=await download_and_merge('source-headers-real',vurl,aurl,VIDEO,str(output),1,
                        audio_headers=AUDIO,preview_enabled=False,client_address='127.0.0.1',video_source_type='hls',audio_source_type='hls')
                    self.assertTrue(success,failure.display if failure else 'unknown');self.assertEqual(_probe_stream_types(output),{'video','audio'})
                    engine=create_async_engine('sqlite+aiosqlite:///:memory:')
                    try:
                        async with engine.begin() as c:await c.run_sync(SQLModel.metadata.create_all)
                        async with AsyncSession(engine) as db:
                            db.add(DownloadTask(id='source-worker',tmdb_id=125,media_type='movie',video_url=vurl,audio_url=aurl,
                                headers_str=encode_source_headers(VIDEO,AUDIO),subtitles_str=json.dumps([{'language':'tr','url':surl,'headers':SUBTITLE}]),
                                private_source_allowed=True,created_at='synthetic'));await db.commit()
                        manager=type(worker.queue_manager)();captured={}
                        async def stub_probe(*args,**kwargs):
                            self.assertEqual(args[2],VIDEO);self.assertEqual(kwargs['audio_headers'],AUDIO)
                            return {**probe,'video_duration':1}
                        async def stop_after_subtitles(**kwargs):
                            captured.update(kwargs);return False,IngestionFailure('SOURCE_ACCESS_DENIED','Synthetic terminal stop',False)
                        with ExitStack() as stack:
                            stack.enter_context(patch.object(worker,'engine',engine));stack.enter_context(patch.object(worker,'probe_media_stream',stub_probe))
                            stack.enter_context(patch.object(worker,'download_and_merge',stop_after_subtitles));stack.enter_context(patch.object(worker,'notify_video_sender',AsyncMock()))
                            stack.enter_context(patch.object(manager,'_reserve_storage',return_value=None));stack.enter_context(patch.object(worker.tmdb_client,'fetch_movie_metadata',AsyncMock(return_value={'title':'Synthetic source headers'})))
                            await manager._process_admitted_task('source-worker')
                        self.assertEqual(captured['headers'],VIDEO);self.assertEqual(captured['audio_headers'],AUDIO);self.assertIn('/tr.vtt',servers[2].seen)
                    finally:await engine.dispose()
                asyncio.run(run())
                for server in servers:self.assertTrue(server.seen);self.assertEqual(server.leaks,[])
            finally:
                for server in servers:server.shutdown();server.server_close()
                for t in threads:t.join(5)

if __name__=='__main__':unittest.main()
