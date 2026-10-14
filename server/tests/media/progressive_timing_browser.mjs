// Consume only synthetic sources and actual verified playback-preparation output.
import fs from 'node:fs';
import http from 'node:http';
import path from 'node:path';
import { createRequire } from 'node:module';
import { fileURLToPath } from 'node:url';

const repository = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '../../..');
const require = createRequire(path.join(repository, 'web/package.json'));
const { chromium } = require('playwright');
const cases = JSON.parse(fs.readFileSync(process.argv[2], 'utf8'));
const hlsScript = path.join(repository, 'web/node_modules/hls.js/dist/hls.min.js');
const server = http.createServer((request, response) => {
  const pathname = decodeURIComponent(new URL(request.url, 'http://localhost').pathname);
  if (pathname === '/') {
    response.writeHead(200, { 'Content-Type': 'text/html', 'Cache-Control': 'no-store' });
    response.end('<!DOCTYPE html><html lang="en"><script src="/hls.js"></script><body><video muted preload="auto" controls></video></body></html>');
    return;
  }
  let target;
  if (pathname === '/hls.js') target = hlsScript;
  else {
    const [, kind, name, ...components] = pathname.split('/');
    const fixture = cases.find(item => item.name === name);
    if (fixture && kind === 'source') target = fixture.source;
    if (fixture && kind === 'cache') {
      const root = path.resolve(fixture.cache);
      const candidate = path.resolve(root, ...components);
      if (candidate.startsWith(root + path.sep)) target = candidate;
    }
  }
  if (!target || !fs.existsSync(target) || !fs.statSync(target).isFile()) {
    response.writeHead(404); response.end(); return;
  }
  const size = fs.statSync(target).size;
  const range = /^bytes=(\d+)-(\d*)$/.exec(request.headers.range || '');
  const start = range ? Number(range[1]) : 0;
  const end = range && range[2] ? Math.min(Number(range[2]), size - 1) : size - 1;
  if (start > end || start >= size) {
    response.writeHead(416, { 'Content-Range': `bytes */${size}` }); response.end(); return;
  }
  const contentType = target.endsWith('.m3u8') ? 'application/vnd.apple.mpegurl' : target.endsWith('.js') ? 'text/javascript' : 'video/mp4';
  const headers = { 'Content-Type': contentType, 'Content-Length': end - start + 1, 'Accept-Ranges': 'bytes', 'Cache-Control': 'no-store' };
  if (range) headers['Content-Range'] = `bytes ${start}-${end}/${size}`;
  response.writeHead(range ? 206 : 200, headers);
  if (request.method === 'HEAD') response.end();
  else fs.createReadStream(target, { start, end }).pipe(response);
});

let browser;
try {
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
  const origin = `http://127.0.0.1:${server.address().port}`;
  browser = await chromium.launch({ executablePath: process.argv[3], headless: true, args: ['--no-sandbox', '--autoplay-policy=no-user-gesture-required'] });
  const context = await browser.newContext();
  const summary = { version: browser.version(), cases: [] };
  for (const fixture of cases) {
    const result = { name: fixture.name, advertisedDirect: fixture.direct };
    for (const transport of ['direct', 'adaptive']) {
      const page = await context.newPage();
      await page.goto(origin);
      result[transport] = await page.evaluate(async ({ origin, name, transport, height }) => {
        const video = document.querySelector('video');
        let error = null;
        let playError = null;
        let hls = null;
        const events = [];
        const read = () => ({ duration: video.duration, currentTime: video.currentTime, width: video.videoWidth, height: video.videoHeight, ended: video.ended });
        for (const event of ['loadedmetadata', 'durationchange', 'playing', 'timeupdate', 'ended']) video.addEventListener(event, () => events.push({ event, ...read() }));
        const ready = new Promise(resolve => {
          video.addEventListener('loadeddata', resolve, { once: true });
          video.addEventListener('error', () => { error = video.error?.message || 'media error'; resolve(); }, { once: true });
          setTimeout(resolve, 7000);
        });
        if (transport === 'direct') video.src = `${origin}/source/${name}`;
        else {
          if (!window.Hls.isSupported()) return { error: 'MediaSource/HLS.js unavailable' };
          hls = new window.Hls({ autoStartLoad: false });
          hls.on(window.Hls.Events.ERROR, (_event, data) => { if (data.fatal) error = `${data.type}:${data.details}`; });
          hls.on(window.Hls.Events.MANIFEST_PARSED, () => {
            const selected = hls.levels.findIndex(level => level.height === height);
            if (selected < 0) { error = 'verified original rendition missing'; return; }
            hls.startLevel = selected;
            hls.currentLevel = selected;
            hls.startLoad(0);
          });
          hls.attachMedia(video);
          hls.loadSource(`${origin}/cache/${name}/master.m3u8`);
        }
        await ready;
        const initial = read();
        video.playbackRate = 4;
        try { await video.play(); } catch (cause) { playError = String(cause); }
        await new Promise(resolve => {
          if (video.ended || video.error || error) return resolve();
          video.addEventListener('ended', resolve, { once: true });
          video.addEventListener('error', resolve, { once: true });
          setTimeout(resolve, 4500);
        });
        const canvas = document.createElement('canvas'); canvas.width = canvas.height = 1;
        let pixel = null;
        try { const ctx = canvas.getContext('2d'); ctx.drawImage(video, 0, 0, 1, 1); pixel = [...ctx.getImageData(0, 0, 1, 1).data]; } catch (_) {}
        const final = { ...read(), initial, error, playError, pixel, events };
        if (hls) hls.destroy();
        return final;
      }, { origin, name: fixture.name, transport, height: fixture.height });
      await page.close();
    }
    summary.cases.push(result);
  }
  await context.close();
  fs.writeFileSync(path.join(path.dirname(process.argv[2]), 'browser-observations.json'), JSON.stringify(summary, null, 2));
  process.stdout.write(JSON.stringify(summary));
} finally {
  if (browser) await browser.close();
  await new Promise(resolve => server.close(resolve));
}
