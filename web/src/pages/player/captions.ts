import React from "react";
interface CueIndex {
  length: number;
  starts: number[];
  latestEnd: number[];
}
const indexes = new WeakMap<TextTrackCueList, CueIndex>();

export function invalidateCaptionIndex(cues: TextTrackCueList): void {
  indexes.delete(cues);
}

/** Find overlapping cues without scanning a full subtitle file on every frame. */
export function captionsAt(cues: TextTrackCueList, time: number): TextTrackCue[] {
  let index = indexes.get(cues);
  if (!index || index.length !== cues.length) {
    const starts: number[] = [];
    const latestEnd: number[] = [];
    let end = Number.NEGATIVE_INFINITY;
    for (let item = 0; item < cues.length; item += 1) {
      starts.push(cues[item].startTime);
      end = Math.max(end, cues[item].endTime);
      latestEnd.push(end);
    }
    index = { length: cues.length, starts, latestEnd };
    indexes.set(cues, index);
  }
  let left = 0;
  let right = index.length;
  while (left < right) {
    const middle = (left + right) >>> 1;
    if (index.latestEnd[middle] <= time) left = middle + 1;
    else right = middle;
  }
  const active: TextTrackCue[] = [];
  for (let item = left; item < index.length && index.starts[item] <= time; item += 1) {
    if (cues[item].endTime > time) active.push(cues[item]);
  }
  return active;
}

const contentCache = new WeakMap<VTTCue, { text: string; content: React.ReactNode }>();

/** Preserve supported WebVTT emphasis without accepting attributes, styles or HTML. */
export function captionContent(cue: VTTCue, fallback: string): React.ReactNode {
  if (typeof cue.getCueAsHTML !== 'function') return fallback;
  const cached = contentCache.get(cue);
  if (cached?.text === cue.text) return cached.content;
  const convert = (node: Node, key: string): React.ReactNode => {
    if (node.nodeType === 3) return node.textContent;
    const children = Array.from(node.childNodes).map((child, index) => convert(child, `${key}-${index}`));
    const tag = node.nodeType === 1 ? (node as Element).tagName.toLowerCase() : '';
    return React.createElement(['b', 'i', 'u', 'ruby', 'rt'].includes(tag) ? tag : React.Fragment, { key }, children);
  };
  try {
    const content = convert(cue.getCueAsHTML(), 'caption');
    contentCache.set(cue, { text: cue.text, content });
    return content;
  } catch {
    return fallback;
  }
}
