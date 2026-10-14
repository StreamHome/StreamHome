"""Disposable localhost-only real API fixture, run through run_isolated.py."""
from contextlib import asynccontextmanager
import json
import socket
import time
from fastapi import FastAPI
from fastapi.responses import JSONResponse, PlainTextResponse
from sqlalchemy.ext.asyncio import create_async_engine
from sqlmodel import SQLModel, select
from sqlmodel.ext.asyncio.session import AsyncSession
import uvicorn
from db import get_session
from models import DownloadTask, IntegrationCredential
from routes import queue as api
from services.integration_auth import integration_token_hash

engine=create_async_engine('sqlite+aiosqlite:///:memory:')
state={'legacy':False}
@asynccontextmanager
async def lifespan(app):
    async with engine.begin() as c:await c.run_sync(SQLModel.metadata.create_all)
    async with AsyncSession(engine) as db:
        db.add(IntegrationCredential(id='fixture',name='Fixture',token_hash=integration_token_hash('fixture-integration'),last_used_at=time.time()))
        await db.commit()
    yield
    await engine.dispose()
app=FastAPI(lifespan=lifespan)
@app.middleware('http')
async def legacy_gate(request,call_next):
    if state['legacy'] and request.url.path=='/api/mediasender/capabilities':return JSONResponse({},status_code=404)
    return await call_next(request)
async def session():
    async with AsyncSession(engine) as db:yield db
async def metadata(*args,**kwargs):return {'title':'Synthetic source fixture'}
app.dependency_overrides[get_session]=session
api.engine=engine;api.tmdb_client.fetch_movie_metadata=metadata
app.include_router(api.router)
@app.get('/__fixture/legacy/{enabled}')
async def legacy(enabled:int):state['legacy']=bool(enabled);return {'legacy':state['legacy']}
@app.get('/__fixture/inspection')
async def inspect():
    async with AsyncSession(engine) as db:
        tasks=(await db.exec(select(DownloadTask))).all()
        return {'count':len(tasks),'independent':all(t.headers=={'cookie':'fixture-video-only'} and t.audio_headers=={}
            and t.subtitles and t.subtitles[0]['headers']=={'authorization':'Bearer fixture-subtitle-only'} for t in tasks)}
@app.get('/__fixture/subtitle.vtt')
async def subtitle():
    return PlainTextResponse('WEBVTT\n\n00:00:00.000 --> 00:00:02.000\nThis is a readable synthetic English subtitle for the independent source access fixture.\n',media_type='text/vtt')
@app.get('/__fixture/video.m3u8')
async def video():return PlainTextResponse('#EXTM3U\n#EXTINF:1,\nsegment.ts\n#EXT-X-ENDLIST\n',media_type='application/vnd.apple.mpegurl')
@app.post('/__fixture/stop')
async def stop():
    server.should_exit=True
    return {'stopping':True}

sock=socket.socket();sock.bind(('127.0.0.1',0));sock.listen(128)
print('SOURCE_HEADER_FIXTURE='+json.dumps({'base':f'http://127.0.0.1:{sock.getsockname()[1]}'}),flush=True)
server=uvicorn.Server(uvicorn.Config(app,log_level='error',access_log=False))
server.run(sockets=[sock])
