// Local synthetic ASGI authority and real Chrome media; no provider/account data.
import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
import { createRequire } from 'node:module';
import { fileURLToPath } from 'node:url';

const repository = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '../../..');
const require = createRequire(path.join(repository, 'web/package.json'));
const { chromium } = require('playwright');
const [origin, chrome, output] = process.argv.slice(2);
const browser = await chromium.launch({ executablePath: chrome, headless: true,
  args: ['--no-sandbox', '--autoplay-policy=no-user-gesture-required'] });
try {
  const context = await browser.newContext();
  await context.addCookies([{ name: 'fixture-session', value: 'hybrid-browser-session', url: origin, httpOnly: true }]);
  const summary = { version: browser.version(), cases: [] };
  for (const transport of ['direct', 'adaptive']) {
    const page = await context.newPage();
    await page.goto(origin);
    const observation = await page.evaluate(async ({ transport }) => {
      const json = async (url, body) => {
        const response = await fetch(url, body ? { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) } : {});
        if (!response.ok) throw new Error(`${url}: ${response.status}: ${await response.text()}`);
        return response.json();
      };
      const run = await json('/api/playback/runs', { movieId: `m_hybrid_${transport}`, profileId: 'hybrid-browser-profile' });
      let status = run;
      for (let attempt = 0; status.preparationState === 'preparing' && attempt < 100; attempt++) {
        await new Promise(resolve => setTimeout(resolve, 100));
        status = await json(`/api/playback/runs/${run.runId}`);
      }
      if (status.preparationState !== 'ready') throw new Error(JSON.stringify(status.preparationError));
      const video = document.querySelector('video');
      let hls = null;
      const errors = [];
      const loaded = new Promise((resolve, reject) => {
        video.addEventListener('loadeddata', resolve, { once: true });
        video.addEventListener('error', () => reject(new Error(video.error?.message || 'video error')), { once: true });
        setTimeout(() => reject(new Error('media load timeout')), 10000);
      });
      if (transport === 'direct') video.src = `${run.streamBaseUrl}/progressive`;
      else {
        if (!window.Hls.isSupported()) throw new Error('HLS.js unavailable');
        hls = new window.Hls();
        hls.on(window.Hls.Events.ERROR, (_event, data) => { if (data.fatal) errors.push(`${data.type}:${data.details}`); });
        hls.attachMedia(video);
        hls.loadSource(`${run.streamBaseUrl}/manifest`);
      }
      await loaded;
      await video.play();
      await new Promise(resolve => setTimeout(resolve, 250));
      video.pause();
      const mountedSource = video.currentSrc;
      const pausedAt = video.currentTime;
      await json('/fixture/expire-short-cache-leases', {});
      for (let index = 0; index < 3; index++) {
        const refreshed = await json(`/api/playback/runs/${run.runId}`);
        if (refreshed.sourceFingerprint !== run.sourceFingerprint || refreshed.streamBaseUrl !== run.streamBaseUrl) throw new Error('status replaced accepted transport identity');
      }
      await new Promise(resolve => setTimeout(resolve, 250));
      const afterStatus = video.currentTime;
      const pausedAfterStatus = video.paused;
      const sourceAfterStatus = video.currentSrc;
      const seeked = new Promise(resolve => video.addEventListener('seeked', resolve, { once: true }));
      video.currentTime = 4;
      await Promise.race([seeked, new Promise((_, reject) => setTimeout(() => reject(new Error('seek timeout')), 5000))]);
      const seekPosition = video.currentTime;
      await video.play();
      await new Promise(resolve => setTimeout(resolve, 250));
      video.pause();
      const resumedPosition = video.currentTime;
      const result = { transport, preparationState: status.preparationState, directCompatible: run.sourceMetadata.progressiveCompatible,
        duration: video.duration, pausedAt, afterStatus, pausedAfterStatus, sourcePreserved: mountedSource === sourceAfterStatus,
        seekPosition, resumedPosition, errors };
      await json(`/api/playback/runs/${run.runId}/close`, { discardProgress: true, timestamp: resumedPosition, sequenceNumber: run.nextSequenceNumber });
      if (hls) hls.destroy();
      return result;
    }, { transport });
    assert.equal(observation.preparationState, 'ready');
    assert.equal(observation.directCompatible, transport === 'direct');
    assert.equal(observation.pausedAfterStatus, true);
    assert.equal(observation.sourcePreserved, true);
    assert.ok(Math.abs(observation.pausedAt - observation.afterStatus) < 0.05, JSON.stringify(observation));
    assert.ok(Math.abs(observation.seekPosition - 4) < 0.3, JSON.stringify(observation));
    assert.ok(observation.resumedPosition > observation.seekPosition + 0.05, JSON.stringify(observation));
    assert.deepEqual(observation.errors, []);
    summary.cases.push(observation);
    await page.close();
  }
  await context.close();
  fs.writeFileSync(output, JSON.stringify(summary, null, 2));
  process.stdout.write(JSON.stringify(summary));
} finally {
  await browser.close();
}
