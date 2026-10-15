import { afterEach, describe, expect, it, vi } from 'vitest';
import { captionsAt } from './captions';
import { nativeCaptionPresentation, openCaptionPip } from './pictureInPicture';

afterEach(() => { Reflect.deleteProperty(window,'documentPictureInPicture'); document.body.replaceChildren(); });
describe('caption presentation surfaces', () => {
  it('restores the existing video to the same sibling position after document PiP', async () => {
    const video=document.createElement('video'); const sibling=document.createElement('span');
    document.body.append(video,sibling);
    const pipDocument=document.implementation.createHTMLDocument('PiP');
    const pip=new EventTarget(); const close=vi.fn();
    Object.assign(pip,{document:pipDocument,close});
    Object.defineProperty(window,'documentPictureInPicture',{configurable:true,value:{requestWindow:vi.fn().mockResolvedValue(pip)}});
    const closed=vi.fn(); const result=await openCaptionPip(video,closed);
    expect(result?.host.contains(video)).toBe(true);
    pip.dispatchEvent(new Event('pagehide'));
    expect(video.nextSibling).toBe(sibling);
    result?.restore(); expect(closed).toHaveBeenCalledTimes(1);
  });
  it('uses native captions with one reversible timing offset', () => {
    const video=document.createElement('video'); const selected=document.createElement('track');
    selected.dataset.subtitleId='en'; video.append(selected);
    const cue={startTime:2,endTime:4};
    Object.defineProperty(selected,'track',{value:{mode:'hidden',cues:[cue]}});
    expect(captionsAt(selected.track.cues!,2.5)).toEqual([cue]);
    const restore=nativeCaptionPresentation(video,'en',1);
    expect(captionsAt(selected.track.cues!,2.5)).toEqual([]);
    expect(captionsAt(selected.track.cues!,4.5)).toEqual([cue]);
    expect(selected.track.mode).toBe('showing'); expect(cue.startTime).toBe(3); expect(cue.endTime).toBe(5);
    restore(); expect(cue.startTime).toBe(2); expect(cue.endTime).toBe(4);
    expect(captionsAt(selected.track.cues!,2.5)).toEqual([cue]);
    expect(captionsAt(selected.track.cues!,4.5)).toEqual([]);
  });
});
