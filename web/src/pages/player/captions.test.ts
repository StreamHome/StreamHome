import { describe, expect, it } from 'vitest';
import { captionsAt } from './captions';

const list = (items: {startTime:number;endTime:number}[]) => items as unknown as TextTrackCueList;
describe('indexed subtitle activation', () => {
  it('keeps overlapping cues and excludes their exact end boundary', () => {
    const cues = list([{startTime:1,endTime:12},{startTime:2,endTime:3},{startTime:4,endTime:8},{startTime:15,endTime:16}]);
    expect(captionsAt(cues,5)).toEqual([cues[0],cues[2]]);
    expect(captionsAt(cues,12)).toEqual([]);
    expect(captionsAt(cues,0)).toEqual([]);
  });
  it('rebuilds an index when the browser appends newly loaded cues', () => {
    const cues = list([{startTime:1,endTime:2}]);
    expect(captionsAt(cues,4)).toEqual([]);
    (cues as unknown as {startTime:number;endTime:number}[]).push({startTime:3,endTime:5});
    expect(captionsAt(cues,4)).toEqual([cues[1]]);
  });
});
