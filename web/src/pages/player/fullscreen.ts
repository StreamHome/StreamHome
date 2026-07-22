interface WebKitFullscreenDocument extends Document {
  webkitExitFullscreen?: () => Promise<void> | void;
  webkitFullscreenElement?: Element | null;
  webkitFullscreenEnabled?: boolean;
}

interface WebKitFullscreenElement extends HTMLElement {
  webkitRequestFullscreen?: () => Promise<void> | void;
}

interface WebKitFullscreenVideo extends HTMLVideoElement {
  webkitDisplayingFullscreen?: boolean;
  webkitEnterFullscreen?: () => void;
  webkitExitFullscreen?: () => void;
}

export type PlayerFullscreenResult = "entered" | "exited";

export function fullscreenElement(documentObject: Document = document): Element | null {
  const webkitDocument = documentObject as WebKitFullscreenDocument;
  return documentObject.fullscreenElement ?? webkitDocument.webkitFullscreenElement ?? null;
}

export function isVideoFullscreen(video: HTMLVideoElement | null): boolean {
  return Boolean((video as WebKitFullscreenVideo | null)?.webkitDisplayingFullscreen);
}

export function isPlayerFullscreen(
  video: HTMLVideoElement | null,
  documentObject: Document = document,
): boolean {
  return Boolean(fullscreenElement(documentObject)) || isVideoFullscreen(video);
}

export function canUsePlayerFullscreen(
  container: HTMLElement | null,
  video: HTMLVideoElement | null,
  documentObject: Document = document,
): boolean {
  if (!container || !video) return false;
  const webkitDocument = documentObject as WebKitFullscreenDocument;
  const webkitContainer = container as WebKitFullscreenElement;
  const webkitVideo = video as WebKitFullscreenVideo;
  return Boolean(
    container.requestFullscreen
      || webkitContainer.webkitRequestFullscreen
      || webkitVideo.webkitEnterFullscreen
      || documentObject.fullscreenEnabled
      || webkitDocument.webkitFullscreenEnabled,
  );
}

async function exitPlayerFullscreen(
  video: HTMLVideoElement,
  documentObject: Document,
): Promise<void> {
  const webkitDocument = documentObject as WebKitFullscreenDocument;
  const webkitVideo = video as WebKitFullscreenVideo;

  if (fullscreenElement(documentObject)) {
    if (documentObject.exitFullscreen) {
      await documentObject.exitFullscreen();
      return;
    }
    if (webkitDocument.webkitExitFullscreen) {
      await webkitDocument.webkitExitFullscreen();
      return;
    }
  }

  if (webkitVideo.webkitDisplayingFullscreen && webkitVideo.webkitExitFullscreen) {
    webkitVideo.webkitExitFullscreen();
  }
}

async function enterPlayerFullscreen(
  container: HTMLElement,
  video: HTMLVideoElement,
  allowVideoFallback: boolean,
): Promise<void> {
  const webkitContainer = container as WebKitFullscreenElement;
  const webkitVideo = video as WebKitFullscreenVideo;
  let standardError: unknown = null;

  if (container.requestFullscreen) {
    try {
      await container.requestFullscreen({ navigationUI: "hide" });
      return;
    } catch (error) {
      standardError = error;
    }
  }

  if (webkitContainer.webkitRequestFullscreen) {
    try {
      await webkitContainer.webkitRequestFullscreen();
      return;
    } catch (error) {
      standardError = error;
    }
  }

  if (allowVideoFallback && webkitVideo.webkitEnterFullscreen) {
    webkitVideo.webkitEnterFullscreen();
    return;
  }

  if (standardError instanceof Error) throw standardError;
  throw new Error("Fullscreen is unavailable in this browser or is blocked by its embedding policy.");
}

export async function togglePlayerFullscreen(
  container: HTMLElement,
  video: HTMLVideoElement,
  documentObject: Document = document,
  options: { allowVideoFallback?: boolean } = {},
): Promise<PlayerFullscreenResult> {
  if (isPlayerFullscreen(video, documentObject)) {
    await exitPlayerFullscreen(video, documentObject);
    return "exited";
  }

  await enterPlayerFullscreen(container, video, options.allowVideoFallback ?? true);
  return "entered";
}
