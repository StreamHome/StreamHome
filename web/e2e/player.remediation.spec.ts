import { expect, test } from '@playwright/test';

async function ready(page: import('@playwright/test').Page, query = '') {
  await page.goto(`/__player-visual-fixture${query}`);
  const video = page.locator('video');
  await expect(video).toBeAttached();
  await video.evaluate((element: HTMLVideoElement) => { element.muted = true; });
  await expect.poll(() => video.evaluate((element: HTMLVideoElement) => element.readyState)).toBeGreaterThanOrEqual(2);
  const play = page.getByRole('button',{name:'Play',exact:true});
  if (await play.isVisible()) await play.click();
  await expect.poll(() => video.evaluate((element: HTMLVideoElement) => element.currentTime)).toBeGreaterThan(0.05);
  await page.mouse.move(30,30);
  await page.mouse.move(50,30);
  await page.getByRole('button',{name:'Pause',exact:true}).click();
  await expect(page.locator('[data-player-root]')).toHaveAttribute('data-player-phase','paused');
  return video;
}

for (const theme of ['ember','aurora','cinema']) {
  test(`keeps ${theme} popovers inside a short desktop viewport`, async ({page}) => {
    await page.setViewportSize({width:960,height:320});
    await ready(page,`?theme=${theme}`);
    await page.getByRole('button',{name:'Player settings',exact:true}).click();
    const popup=page.getByRole('dialog',{name:'Player settings'});
    await expect(popup).toBeVisible();
    const bounds=await popup.boundingBox();
    expect(bounds!.x).toBeGreaterThanOrEqual(0);
    expect(bounds!.y).toBeGreaterThanOrEqual(0);
    expect(bounds!.x+bounds!.width).toBeLessThanOrEqual(961);
    expect(bounds!.y+bounds!.height).toBeLessThanOrEqual(321);
    await page.getByRole('button',{name:'Audio language: English · 440 Hz',exact:true}).click();
    await expect(popup).toHaveCount(0);
    await expect(page.getByRole('listbox',{name:'Audio language'})).toBeVisible();
    await page.keyboard.press('Escape');
    await expect(page.getByRole('listbox')).toHaveCount(0);
    await expect(page.getByRole('button',{name:'Audio language: English · 440 Hz',exact:true})).toBeFocused();
  });
}

test('uses the screen Y axis for the rotated portrait timeline', async ({browser}) => {
  const context=await browser.newContext({viewport:{width:390,height:844},isMobile:true,hasTouch:true});
  const page=await context.newPage();
  const video=await ready(page);
  await expect(page.locator('[data-player-root]')).toHaveAttribute('data-mobile-orientation','forced-landscape');
  const timeline=page.getByRole('slider',{name:'Playback position'});
  const box=await timeline.boundingBox();
  await page.mouse.move(box!.x+box!.width/2,box!.y+box!.height*0.75);
  await page.mouse.down(); await page.mouse.up();
  await expect.poll(() => video.evaluate((element:HTMLVideoElement)=>element.currentTime)).toBeGreaterThan(8.7);
  await expect.poll(() => video.evaluate((element:HTMLVideoElement)=>element.currentTime)).toBeLessThan(9.3);
  await context.close();
});

test('accepts actual native backward seeks without rebuilding the source', async ({page}) => {
  const video=await ready(page);
  const source=await video.getAttribute('src');
  await video.evaluate((element:HTMLVideoElement)=>{element.currentTime=9;});
  await expect.poll(()=>page.getByRole('slider',{name:'Playback position'}).inputValue()).toBe('9');
  await video.evaluate((element:HTMLVideoElement)=>{element.currentTime=2;});
  await expect.poll(()=>page.getByRole('slider',{name:'Playback position'}).inputValue()).toBe('2');
  expect(await video.getAttribute('src')).toBe(source);
});

test('measures buffered Play, Pause and seek in the mounted HLS player', async ({page}) => {
  test.skip(!process.env.STREAMHOME_PLAYER_PERF,'Opt-in latency gate for a controlled local ready-media environment.');
  const video=await ready(page);
  const samples:{play:number[];pause:number[];seek:number[]}={play:[],pause:[],seek:[]};
  for (let trial=0;trial<20;trial++) {
    const play=await video.evaluate((element:HTMLVideoElement)=>new Promise<number>((resolve)=>{
      const start=performance.now(); element.addEventListener('playing',()=>resolve(performance.now()-start),{once:true});
      document.querySelector<HTMLButtonElement>('button[aria-label="Play"]')!.click();
    }));
    samples.play.push(play);
    const pause=await video.evaluate((element:HTMLVideoElement)=>{
      const start=performance.now(); document.querySelector<HTMLButtonElement>('button[aria-label="Pause"]')!.click();
      return element.paused ? performance.now()-start : Number.POSITIVE_INFINITY;
    });
    samples.pause.push(pause);
    const seek=await video.evaluate((element:HTMLVideoElement)=>new Promise<number>((resolve)=>{
      const start=performance.now(); element.addEventListener('seeked',()=>resolve(performance.now()-start),{once:true});
      element.currentTime=element.currentTime>5 ? 2 : 8;
    }));
    samples.seek.push(seek);
  }
  const p95=(items:number[])=>[...items].sort((a,b)=>a-b)[Math.ceil(items.length*.95)-1];
  const measured={playP95Ms:p95(samples.play),pauseP95Ms:p95(samples.pause),bufferedSeekP95Ms:p95(samples.seek),sampleCount:20};
  console.log('Mounted HLS latency',JSON.stringify(measured));
  await test.info().attach('latency.json',{body:JSON.stringify({measured,samples},null,2),contentType:'application/json'});
  expect(measured.playP95Ms).toBeLessThanOrEqual(150);
  expect(measured.pauseP95Ms).toBeLessThanOrEqual(100);
  expect(measured.bufferedSeekP95Ms).toBeLessThanOrEqual(200);
});

test('keeps loaded captions inside the player settings and separate from idle chrome', async ({page}) => {
  test.skip(!process.env.STREAMHOME_SEMANTIC_FIXTURE,'Requires the isolated backend semantic HLS fixture export.');
  await page.route('**/api/playback/subtitles/m_player_visual_fixture/**',route=>route.fulfill({contentType:'text/vtt',body:'WEBVTT\n\n00:01.000 --> 00:10.000\nSemantic caption\n'}));
  const video=await ready(page,'?fixture=semantic');
  await page.getByRole('button',{name:/Subtitles: Subtitles off/}).click();
  await page.getByRole('option',{name:'English',exact:true}).click();
  await video.evaluate((element:HTMLVideoElement)=>{element.currentTime=2;});
  await expect(page.getByRole('region',{name:'Subtitles'})).toHaveText('Semantic caption');
  await page.getByRole('button',{name:'Player settings',exact:true}).click();
  await expect(page.getByRole('slider',{name:'Subtitle timing'})).toBeVisible();
  await page.screenshot({path:test.info().outputPath('captions-settings.png')});
  await page.keyboard.press('Escape');
  await page.getByRole('button',{name:'Play',exact:true}).click();
  await page.locator('[data-player-root]').focus();
  await expect(page.locator('[data-player-root]')).toHaveAttribute('data-controls-visible','false',{timeout:5000});
  await expect(page.getByRole('region',{name:'Subtitles'})).toHaveText('Semantic caption');
});

test('switches the decoded HLS dubbing within the latency budget', async ({page}) => {
  test.skip(!process.env.STREAMHOME_PLAYER_PERF,'Opt-in latency gate for a controlled local ready-media environment.');
  await ready(page);
  await page.locator('video').evaluate(async (element:HTMLVideoElement)=>{
    const context=new AudioContext();
    const source=context.createMediaElementSource(element);
    const analyser=context.createAnalyser(); analyser.fftSize=4096;
    const silent=context.createGain(); silent.gain.value=0.000001;
    source.connect(analyser); analyser.connect(silent); silent.connect(context.destination);
    await context.resume(); element.muted=false; element.loop=true;
    Object.assign(window,{playerTestAnalyser:analyser,playerTestAudioContext:context});
  });
  await page.getByRole('button',{name:'Play',exact:true}).click();
  const results:number[]=[];
  for (let trial=0;trial<20;trial++) {
    await page.mouse.move(40,40); await page.mouse.move(60,40);
    await page.getByRole('button',{name:/Audio language/}).click();
    const language=trial%2===0 ? 'Turkish' : 'English';
    const option=page.getByRole('option',{name:new RegExp(language)});
    const timing=await option.evaluate((element:HTMLElement,frequency:number)=>new Promise<number>((resolve,reject)=>{
      const scope=window as Window & {playerTestAnalyser:AnalyserNode;playerTestAudioContext:AudioContext};
      const data=new Float32Array(scope.playerTestAnalyser.frequencyBinCount);
      const start=performance.now(); element.click();
      const detect=()=>{
        scope.playerTestAnalyser.getFloatFrequencyData(data);
        const bin=Math.round(frequency/scope.playerTestAudioContext.sampleRate*scope.playerTestAnalyser.fftSize);
        const other=Math.round((frequency===880 ? 440 : 880)/scope.playerTestAudioContext.sampleRate*scope.playerTestAnalyser.fftSize);
        if (data[bin]>-50 && data[bin]>data[other]+12) { resolve(performance.now()-start); return; }
        if (performance.now()-start>3000) { reject(new Error('Decoded dubbing never switched')); return; }
        requestAnimationFrame(detect);
      }; requestAnimationFrame(detect);
    }),trial%2===0 ? 880 : 440);
    results.push(timing);
  }
  const p95=[...results].sort((a,b)=>a-b)[18];
  console.log('Decoded HLS audio switch',JSON.stringify({p95Ms:p95,sampleCount:20}));
  await test.info().attach('audio-switch.json',{body:JSON.stringify({p95Ms:p95,samples:results}),contentType:'application/json'});
  expect(p95).toBeLessThanOrEqual(500);
});

test('aligns browser-decoded flash and impulse markers in both HLS dubbings', async ({page}) => {
  const pageErrors:string[]=[]; page.on('pageerror',error=>pageErrors.push(error.message));
  test.skip(!process.env.STREAMHOME_SEMANTIC_FIXTURE,'Requires the isolated backend semantic HLS fixture export.');
  await ready(page,'?fixture=semantic');
  const video=page.locator('video');
  const mountedSource=await video.getAttribute('src');
  await video.evaluate(async (element:HTMLVideoElement)=>{
    const context=new AudioContext(); const source=context.createMediaElementSource(element);
    const code=`class Marker extends AudioWorkletProcessor { last=-1; process(inputs) { const data=inputs[0]?.[0]; if(data) { for(let i=0;i<data.length;i++) { if(Math.abs(data[i])>.2 && currentTime-this.last>.5) {this.last=currentTime;this.port.postMessage({time:currentTime+i/sampleRate});break;} } } return true; } } registerProcessor('player-marker',Marker);`;
    const url=URL.createObjectURL(new Blob([code],{type:'text/javascript'}));
    await context.audioWorklet.addModule(url); URL.revokeObjectURL(url);
    const marker=new AudioWorkletNode(context,'player-marker'); const silent=context.createGain(); silent.gain.value=0.000001;
    source.connect(marker); marker.connect(silent); silent.connect(context.destination); await context.resume();
    const state={audio:[] as number[],video:[] as number[],context,frameCount:0,colors:[] as {time:number;rgb:number[]}[]};
    Object.assign(window,{playerSemanticTest:state});
    marker.port.onmessage=(event:MessageEvent<{time:number}>)=>{
      const clock=context.getOutputTimestamp();
      state.audio.push(clock.performanceTime!+(event.data.time-clock.contextTime!)*1000);
    };
    const canvas=document.createElement('canvas');canvas.width=2;canvas.height=2;const pixels=canvas.getContext('2d')!;
    let white=false;
    const frame=(_now:number,metadata:VideoFrameCallbackMetadata)=>{
      pixels.drawImage(element,0,0,2,2); const data=pixels.getImageData(0,0,2,2).data;
      state.frameCount++; if(state.colors.length<15 || (metadata.mediaTime>1.9&&metadata.mediaTime<2.3))state.colors.push({time:metadata.mediaTime,rgb:Array.from(data.slice(0,4))});
      const next=data[0]>200&&data[1]>200&&data[2]>200;
      if(next&&!white)state.video.push(metadata.expectedDisplayTime); white=next;
      element.requestVideoFrameCallback(frame);
    };element.requestVideoFrameCallback(frame);element.muted=false;
  });
  const errors:{language:string;errorsMs:number[]}[]=[];
  for (const language of ['English','Turkish']) {
    if(language==='Turkish') {
      await page.mouse.move(40,40);await page.mouse.move(60,40);
      await page.getByRole('button',{name:/Audio language/}).click();await page.getByRole('option',{name:language,exact:true}).click();
    }
    await expect(page.getByRole('button',{name:/Audio language/})).toContainText(language);
    expect(await video.getAttribute('src')).toBe(mountedSource);
    await expect.poll(()=>video.evaluate((element:HTMLVideoElement)=>element.readyState)).toBeGreaterThanOrEqual(2);
    await page.getByRole('button',{name:'Rewind 10 seconds',exact:true}).click();
    await expect.poll(()=>video.evaluate((element:HTMLVideoElement)=>element.seeking)).toBe(false);
    await expect.poll(()=>video.evaluate((element:HTMLVideoElement)=>element.currentTime)).toBeLessThan(.5);
    await video.evaluate(()=>{
      const scope=window as Window&{playerSemanticTest:{audio:number[];video:number[]}};
      scope.playerSemanticTest.audio.length=0;scope.playerSemanticTest.video.length=0;
    });
    await page.getByRole('button',{name:'Play',exact:true}).click();
    try {
      await expect.poll(()=>page.evaluate(()=>{
        const state=(window as Window&{playerSemanticTest:{audio:number[];video:number[]}}).playerSemanticTest;
        return Math.min(state.audio.length,state.video.length);
      }),{timeout:15000}).toBeGreaterThanOrEqual(3);
    } catch(error) {
      const diagnostic=await video.evaluate((element:HTMLVideoElement)=>{
        const state=(window as Window&{playerSemanticTest:{audio:number[];video:number[];context:AudioContext;frameCount:number;colors:unknown[]}}).playerSemanticTest;
        return {audio:state.audio,video:state.video,frameCount:state.frameCount,colors:state.colors,contextState:state.context.state,contextTime:state.context.currentTime,outputTimestamp:typeof state.context.getOutputTimestamp,
          mediaTime:element.currentTime,paused:element.paused,muted:element.muted,readyState:element.readyState,videoWidth:element.videoWidth,transport:document.querySelector('[data-player-root]')?.getAttribute('data-player-transport')};
      });
      console.log('Semantic capture diagnostic',JSON.stringify({language,diagnostic,pageErrors}));
      throw error;
    }
    console.log('Semantic transport',await page.locator('[data-player-root]').getAttribute('data-player-transport'));
    const measured=await page.evaluate(()=>{
      const state=(window as Window&{playerSemanticTest:{audio:number[];video:number[]}}).playerSemanticTest;
      return state.video.slice(0,3).map((time,index)=>state.audio[index]-time);
    });
    errors.push({language,errorsMs:measured});
    await page.mouse.move(40,40);await page.mouse.move(60,40);await page.getByRole('button',{name:'Pause',exact:true}).click();
  }
  console.log('Browser semantic alignment',JSON.stringify(errors));
  await test.info().attach('semantic-alignment.json',{body:JSON.stringify(errors),contentType:'application/json'});
  for(const result of errors)for(const error of result.errorsMs)expect(Math.abs(error)).toBeLessThanOrEqual(80);
});

test('measures cold and warm first-frame latency for prepared local media', async ({browser}) => {
  test.skip(!process.env.STREAMHOME_PLAYER_PERF,'Opt-in latency gate for a controlled local ready-media environment.');
  test.setTimeout(120000);
  const cold:number[]=[]; const warm:number[]=[];
  for(let trial=0;trial<20;trial++) {
    const context=await browser.newContext();
    await context.addInitScript(()=>{
      new MutationObserver(()=>{
        const video=document.querySelector('video');
        if(!video || video.dataset.firstFrameObserved)return;
        video.dataset.firstFrameObserved='true';video.muted=true;
        video.requestVideoFrameCallback(()=>Object.assign(window,{playerFirstFrameMs:performance.now()}));
      }).observe(document,{childList:true,subtree:true});
    });
    const page=await context.newPage();
    for(const samples of [cold,warm]) {
      await page.goto('/__player-visual-fixture');
      await expect.poll(()=>page.evaluate(()=>(window as Window&{playerFirstFrameMs?:number}).playerFirstFrameMs),{timeout:5000}).toBeGreaterThan(0);
      samples.push(await page.evaluate(()=>(window as Window&{playerFirstFrameMs:number}).playerFirstFrameMs));
    }
    await context.close();
  }
  const p95=(values:number[])=>[...values].sort((a,b)=>a-b)[18];
  const measured={coldP95Ms:p95(cold),warmP95Ms:p95(warm),sampleCount:20};
  console.log('Prepared local first frame',JSON.stringify(measured));
  await test.info().attach('startup-latency.json',{body:JSON.stringify({measured,cold,warm}),contentType:'application/json'});
  expect(measured.coldP95Ms).toBeLessThanOrEqual(2000);
  expect(measured.warmP95Ms).toBeLessThanOrEqual(1000);
});

test('retains video and captions when opening and closing document picture in picture',async({page})=>{
  test.skip(!process.env.STREAMHOME_SEMANTIC_FIXTURE,'Requires the isolated backend semantic HLS fixture export.');
  await page.route('**/api/playback/subtitles/m_player_visual_fixture/**',route=>route.fulfill({contentType:'text/vtt',body:'WEBVTT\n\n00:01.000 --> 00:10.000\nPiP caption\n'}));
  await ready(page,'?fixture=semantic');
  test.skip(!await page.evaluate(()=>Boolean((window as Window&{documentPictureInPicture?:unknown}).documentPictureInPicture)),'Document picture in picture is unavailable in this browser.');
  await page.getByRole('button',{name:/Subtitles: Subtitles off/}).click();await page.getByRole('option',{name:'English',exact:true}).click();
  await page.locator('video').evaluate((video:HTMLVideoElement)=>{video.currentTime=2;Object.assign(window,{pipOriginalVideo:video,pipLoads:0});video.addEventListener('loadstart',()=>{(window as Window&{pipLoads:number}).pipLoads++;});});
  await expect(page.getByRole('region',{name:'Subtitles'})).toHaveText('PiP caption');
  await page.getByRole('button',{name:'Picture in picture',exact:true}).click();
  await expect.poll(()=>page.evaluate(()=>{
    const scope=window as Window&{documentPictureInPicture:{window?:Window};pipOriginalVideo:HTMLVideoElement};
    const pip=scope.documentPictureInPicture.window;
    return Boolean(pip&&pip.document.querySelector('video')===scope.pipOriginalVideo&&pip.document.querySelector('[aria-label="Subtitles"]')?.textContent==='PiP caption');
  })).toBe(true);
  await page.evaluate(()=>(window as Window&{documentPictureInPicture:{window:Window}}).documentPictureInPicture.window.close());
  await expect.poll(()=>page.evaluate(()=>document.querySelector('video')===(window as Window&{pipOriginalVideo:HTMLVideoElement}).pipOriginalVideo)).toBe(true);
  expect(await page.evaluate(()=>(window as Window&{pipLoads:number}).pipLoads)).toBe(0);
  await expect(page.getByRole('region',{name:'Subtitles'})).toHaveText('PiP caption');
});
