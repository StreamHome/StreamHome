import { invalidateCaptionIndex } from "./captions";

interface DocumentPip {
  requestWindow: (options: { width: number; height: number }) => Promise<Window>;
}
export interface CaptionPip {
  window: Window;
  host: HTMLElement;
  restore: () => void;
}

/** Move the existing video, keeping its decoder and media event ownership intact. */
export async function openCaptionPip(video: HTMLVideoElement, closed: () => void): Promise<CaptionPip | null> {
  const api = (window as Window & { documentPictureInPicture?: DocumentPip }).documentPictureInPicture;
  if (!api) return null;
  const pip = await api.requestWindow({ width: 640, height: 400 });
  const originalParent = video.parentNode;
  if (!originalParent || !video.isConnected) { pip.close(); return null; }
  const anchor = document.createComment("player-video-position");
  originalParent.insertBefore(anchor, video);
  for (const node of document.querySelectorAll("style, link[rel='stylesheet']")) {
    const clone = node.cloneNode(true) as HTMLElement;
    if (node.tagName === "LINK") (clone as HTMLLinkElement).href = (node as HTMLLinkElement).href;
    pip.document.head.appendChild(clone);
  }
  pip.document.documentElement.dataset.theme = document.documentElement.dataset.theme;
  const host = pip.document.createElement("div");
  const player = video.closest<HTMLElement>("[data-player-root='true']");
  host.className = "player-view";
  host.dataset.theme = player?.dataset.theme;
  host.dataset.playerTheme = player?.dataset.playerTheme;
  host.dataset.playerPip = "true";
  host.style.cssText = "position:fixed;inset:0;background:#000;color:#fff;overflow:hidden";
  pip.document.body.appendChild(host);
  host.appendChild(video);
  let restored = false;
  const restore = () => {
    if (restored) return;
    restored = true;
    if (anchor.parentNode) anchor.parentNode.replaceChild(video, anchor);
    closed();
  };
  pip.addEventListener("pagehide", restore, { once: true });
  return { window: pip, host, restore };
}

/** Native video-only surfaces need browser captions, including the selected offset. */
export function nativeCaptionPresentation(video: HTMLVideoElement, selected: string, offset: number): () => void {
  const changedTracks: TextTrackCueList[] = [];
  const originals: { cue: TextTrackCue; start: number; end: number }[] = [];
  for (const element of video.querySelectorAll<HTMLTrackElement>("track[data-subtitle-id]")) {
    const enabled = selected !== "off" && element.dataset.subtitleId === selected;
    element.track.mode = enabled ? "showing" : "disabled";
    if (!enabled || !element.track.cues || offset === 0) continue;
    changedTracks.push(element.track.cues);
    invalidateCaptionIndex(element.track.cues);
    for (const cue of Array.from(element.track.cues)) {
      originals.push({ cue, start: cue.startTime, end: cue.endTime });
      cue.startTime = Math.max(0, cue.startTime + offset);
      cue.endTime = Math.max(cue.startTime, cue.endTime + offset);
    }
  }
  return () => {
    for (const { cue, start, end } of originals) { cue.startTime = start; cue.endTime = end; }
    for (const cues of changedTracks) invalidateCaptionIndex(cues);
  };
}
