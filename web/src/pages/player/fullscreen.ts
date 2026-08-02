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

export interface PlayerFullscreenOptions {
  allowVideoFallback?: boolean;
  allowViewportFallback?: boolean;
  transitionTimeoutMs?: number;
}

const DEFAULT_TRANSITION_TIMEOUT_MS = 2_500;
const VIEWPORT_FULLSCREEN_ATTRIBUTE = "data-player-viewport-fullscreen";

export type PlayerFullscreenMode = "native" | "viewport" | null;

export function fullscreenElement(documentObject: Document = document): Element | null {
  const webkitDocument = documentObject as WebKitFullscreenDocument;
  return documentObject.fullscreenElement ?? webkitDocument.webkitFullscreenElement ?? null;
}

export function isVideoFullscreen(video: HTMLVideoElement | null): boolean {
  return Boolean((video as WebKitFullscreenVideo | null)?.webkitDisplayingFullscreen);
}

export function isViewportPlayerFullscreen(container: HTMLElement | null): boolean {
  return container?.getAttribute?.(VIEWPORT_FULLSCREEN_ATTRIBUTE) === "true";
}

function ownsFullscreenElement(
  container: HTMLElement,
  video: HTMLVideoElement,
  activeElement: Element | null,
): boolean {
  return Boolean(
    activeElement
    && (
      activeElement === container
      || activeElement === video
      || container.contains(activeElement)
      || (typeof activeElement.contains === "function" && activeElement.contains(container))
    ),
  );
}

export function isPlayerFullscreen(
  container: HTMLElement | null,
  video: HTMLVideoElement | null,
  documentObject: Document = document,
): boolean {
  if (!container || !video) return false;
  return ownsFullscreenElement(container, video, fullscreenElement(documentObject))
    || isVideoFullscreen(video)
    || isViewportPlayerFullscreen(container);
}

export function playerFullscreenMode(
  container: HTMLElement | null,
  video: HTMLVideoElement | null,
  documentObject: Document = document,
): PlayerFullscreenMode {
  if (!container || !video) return null;
  if (
    ownsFullscreenElement(container, video, fullscreenElement(documentObject))
    || isVideoFullscreen(video)
  ) return "native";
  return isViewportPlayerFullscreen(container) ? "viewport" : null;
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
    (documentObject.fullscreenEnabled !== false && (container.requestFullscreen || video.requestFullscreen))
    || (webkitDocument.webkitFullscreenEnabled !== false && webkitContainer.webkitRequestFullscreen)
    || webkitVideo.webkitEnterFullscreen,
  );
}

function setViewportPlayerFullscreen(
  container: HTMLElement,
  documentObject: Document,
  active: boolean,
): void {
  if (active) container.setAttribute?.(VIEWPORT_FULLSCREEN_ATTRIBUTE, "true");
  else container.removeAttribute?.(VIEWPORT_FULLSCREEN_ATTRIBUTE);
  const documentElement = documentObject.documentElement;
  const body = documentObject.body;
  if (active) {
    documentElement?.setAttribute?.(VIEWPORT_FULLSCREEN_ATTRIBUTE, "true");
    body?.setAttribute?.(VIEWPORT_FULLSCREEN_ATTRIBUTE, "true");
  } else {
    documentElement?.removeAttribute?.(VIEWPORT_FULLSCREEN_ATTRIBUTE);
    body?.removeAttribute?.(VIEWPORT_FULLSCREEN_ATTRIBUTE);
  }
}

export function releaseViewportPlayerFullscreen(
  container: HTMLElement | null,
  documentObject: Document = document,
): void {
  container?.removeAttribute?.(VIEWPORT_FULLSCREEN_ATTRIBUTE);
  documentObject.documentElement?.removeAttribute?.(VIEWPORT_FULLSCREEN_ATTRIBUTE);
  documentObject.body?.removeAttribute?.(VIEWPORT_FULLSCREEN_ATTRIBUTE);
}

function transitionTargets(
  video: HTMLVideoElement,
  documentObject: Document,
): Array<{ target: EventTarget; events: string[] }> {
  return [
    {
      target: documentObject,
      events: [
        "fullscreenchange",
        "fullscreenerror",
        "webkitfullscreenchange",
        "webkitfullscreenerror",
      ],
    },
    {
      target: video,
      events: [
        "fullscreenchange",
        "fullscreenerror",
        "webkitbeginfullscreen",
        "webkitendfullscreen",
      ],
    },
  ];
}

interface FullscreenStateObservation {
  cancel: () => void;
  promise: Promise<boolean>;
}

function observePlayerFullscreenState(
  container: HTMLElement,
  video: HTMLVideoElement,
  documentObject: Document,
  expectedActive: boolean,
  timeoutMs: number,
): FullscreenStateObservation {
  const hasExpectedState = () => isPlayerFullscreen(container, video, documentObject) === expectedActive;
  if (hasExpectedState()) {
    return {
      cancel: () => undefined,
      promise: Promise.resolve(true),
    };
  }

  let cancel: () => void = () => undefined;
  const promise = new Promise<boolean>((resolve) => {
    let settled = false;
    let timer: ReturnType<typeof setTimeout> | null = null;
    const targets = transitionTargets(video, documentObject);

    const finish = (matched: boolean) => {
      if (settled) return;
      settled = true;
      if (timer !== null) clearTimeout(timer);
      for (const { target, events } of targets) {
        for (const eventName of events) target.removeEventListener(eventName, onTransition);
      }
      resolve(matched);
    };

    const onTransition = (event: Event) => {
      if (event.type.endsWith("error")) {
        finish(false);
        return;
      }
      if (hasExpectedState()) finish(true);
    };

    cancel = () => finish(false);
    for (const { target, events } of targets) {
      for (const eventName of events) target.addEventListener(eventName, onTransition);
    }
    timer = setTimeout(() => finish(hasExpectedState()), Math.max(0, timeoutMs));
  });

  return { cancel, promise };
}

async function requestVerifiedFullscreenTransition(
  request: () => Promise<void> | void,
  container: HTMLElement,
  video: HTMLVideoElement,
  documentObject: Document,
  expectedActive: boolean,
  timeoutMs: number,
): Promise<boolean> {
  const observation = observePlayerFullscreenState(
    container,
    video,
    documentObject,
    expectedActive,
    timeoutMs,
  );
  let requestResult: Promise<void> | void;
  try {
    requestResult = request();
  } catch (error) {
    observation.cancel();
    throw error;
  }

  const requestOutcome = Promise.resolve(requestResult).then(
    () => ({ source: "request" as const, error: null }),
    (error: unknown) => ({ source: "request" as const, error }),
  );
  const stateOutcome = observation.promise.then((matched) => ({
    source: "state" as const,
    matched,
  }));
  const firstOutcome = await Promise.race([requestOutcome, stateOutcome]);

  if (firstOutcome.source === "state") return firstOutcome.matched;
  if (firstOutcome.error !== null) {
    observation.cancel();
    throw firstOutcome.error;
  }
  return observation.promise;
}

function errorMessage(error: unknown): string {
  if (error instanceof Error && error.message.trim()) return error.message.trim();
  return "The browser rejected the fullscreen request.";
}

function fullscreenEntryError(details: string[]): Error {
  if (details.length === 0) {
    return new Error("Fullscreen is unavailable in this browser or is blocked by its site policy.");
  }
  const latestDetail = details[details.length - 1];
  return new Error(
    `Fullscreen could not be opened. ${latestDetail} Check the browser's fullscreen permission for this site.`,
  );
}

async function exitPlayerFullscreen(
  container: HTMLElement,
  video: HTMLVideoElement,
  documentObject: Document,
  transitionTimeoutMs: number,
): Promise<void> {
  const webkitDocument = documentObject as WebKitFullscreenDocument;
  const webkitVideo = video as WebKitFullscreenVideo;
  const activeElement = fullscreenElement(documentObject);

  if (isViewportPlayerFullscreen(container)) {
    setViewportPlayerFullscreen(container, documentObject, false);
    return;
  }

  if (ownsFullscreenElement(container, video, activeElement)) {
    const exit = documentObject.exitFullscreen
      ? () => documentObject.exitFullscreen()
      : webkitDocument.webkitExitFullscreen
        ? () => webkitDocument.webkitExitFullscreen?.()
        : null;
    if (!exit) throw new Error("Fullscreen is active, but this browser does not expose an exit method.");

    if (await requestVerifiedFullscreenTransition(
      exit,
      container,
      video,
      documentObject,
      false,
      transitionTimeoutMs,
    )) return;
    throw new Error("The browser accepted the request but did not exit fullscreen.");
  }

  if (webkitVideo.webkitDisplayingFullscreen && webkitVideo.webkitExitFullscreen) {
    if (await requestVerifiedFullscreenTransition(
      () => webkitVideo.webkitExitFullscreen?.(),
      container,
      video,
      documentObject,
      false,
      transitionTimeoutMs,
    )) return;
    throw new Error("The browser accepted the request but did not exit native video fullscreen.");
  }
}

async function enterPlayerFullscreen(
  container: HTMLElement,
  video: HTMLVideoElement,
  documentObject: Document,
  allowVideoFallback: boolean,
  allowViewportFallback: boolean,
  transitionTimeoutMs: number,
): Promise<void> {
  const webkitDocument = documentObject as WebKitFullscreenDocument;
  const webkitContainer = container as WebKitFullscreenElement;
  const webkitVideo = video as WebKitFullscreenVideo;
  let attempt: { label: string; request: () => Promise<void> | void } | null = null;
  let failure: string | null = null;
  const standardElementFullscreenEnabled = documentObject.fullscreenEnabled !== false;
  const webkitElementFullscreenEnabled = webkitDocument.webkitFullscreenEnabled !== false;

  // Fullscreen requests must remain inside the original user-activation turn.
  // Choose the best enabled native target up front. The viewport fallback does
  // not require transient activation and remains safe after native rejection.
  if (standardElementFullscreenEnabled && container.requestFullscreen) {
    attempt = {
      label: "player container",
      request: () => container.requestFullscreen(),
    };
  } else if (webkitElementFullscreenEnabled && webkitContainer.webkitRequestFullscreen) {
    attempt = {
      label: "WebKit player container",
      request: () => webkitContainer.webkitRequestFullscreen?.(),
    };
  } else if (allowVideoFallback && standardElementFullscreenEnabled && video.requestFullscreen) {
    attempt = {
      label: "video element",
      request: () => video.requestFullscreen(),
    };
  } else if (allowVideoFallback && webkitVideo.webkitEnterFullscreen) {
    attempt = {
      label: "native video",
      request: () => webkitVideo.webkitEnterFullscreen?.(),
    };
  }

  if (attempt) {
    try {
      if (await requestVerifiedFullscreenTransition(
        attempt.request,
        container,
        video,
        documentObject,
        true,
        transitionTimeoutMs,
      )) return;
      failure = `${attempt.label}: the browser accepted the request but did not enter fullscreen.`;
    } catch (error) {
      failure = `${attempt.label}: ${errorMessage(error)}`;
    }
  }

  if (allowViewportFallback) {
    setViewportPlayerFullscreen(container, documentObject, true);
    return;
  }

  throw fullscreenEntryError(failure ? [failure] : []);
}

export async function togglePlayerFullscreen(
  container: HTMLElement,
  video: HTMLVideoElement,
  documentObject: Document = document,
  options: PlayerFullscreenOptions = {},
): Promise<PlayerFullscreenResult> {
  const transitionTimeoutMs = options.transitionTimeoutMs ?? DEFAULT_TRANSITION_TIMEOUT_MS;
  if (isPlayerFullscreen(container, video, documentObject)) {
    await exitPlayerFullscreen(container, video, documentObject, transitionTimeoutMs);
    return "exited";
  }

  await enterPlayerFullscreen(
    container,
    video,
    documentObject,
    options.allowVideoFallback ?? true,
    options.allowViewportFallback ?? true,
    transitionTimeoutMs,
  );
  return "entered";
}
