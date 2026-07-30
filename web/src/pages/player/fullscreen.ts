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
  transitionTimeoutMs?: number;
}

const DEFAULT_TRANSITION_TIMEOUT_MS = 2_500;

export function fullscreenElement(documentObject: Document = document): Element | null {
  const webkitDocument = documentObject as WebKitFullscreenDocument;
  return documentObject.fullscreenElement ?? webkitDocument.webkitFullscreenElement ?? null;
}

export function isVideoFullscreen(video: HTMLVideoElement | null): boolean {
  return Boolean((video as WebKitFullscreenVideo | null)?.webkitDisplayingFullscreen);
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
    || isVideoFullscreen(video);
}

export function canUsePlayerFullscreen(
  container: HTMLElement | null,
  video: HTMLVideoElement | null,
): boolean {
  if (!container || !video) return false;
  const webkitContainer = container as WebKitFullscreenElement;
  const webkitVideo = video as WebKitFullscreenVideo;
  return Boolean(
    container.requestFullscreen
    || video.requestFullscreen
    || webkitContainer.webkitRequestFullscreen
    || webkitVideo.webkitEnterFullscreen,
  );
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
  transitionTimeoutMs: number,
): Promise<void> {
  const webkitContainer = container as WebKitFullscreenElement;
  const documentRoot = documentObject.documentElement as HTMLElement | undefined;
  const webkitDocumentRoot = documentRoot as WebKitFullscreenElement | undefined;
  const webkitVideo = video as WebKitFullscreenVideo;
  const failures: string[] = [];
  const attempts: Array<{ label: string; request: () => Promise<void> | void }> = [];

  if (documentRoot?.requestFullscreen) {
    attempts.push({
      label: "application root",
      request: () => documentRoot.requestFullscreen(),
    });
  }
  if (container.requestFullscreen) {
    attempts.push({
      label: "player container",
      request: () => container.requestFullscreen(),
    });
  }
  if (allowVideoFallback && video.requestFullscreen) {
    attempts.push({
      label: "video element",
      request: () => video.requestFullscreen(),
    });
  }
  if (webkitContainer.webkitRequestFullscreen) {
    attempts.push({
      label: "WebKit player container",
      request: () => webkitContainer.webkitRequestFullscreen?.(),
    });
  }
  if (
    webkitDocumentRoot
    && webkitDocumentRoot !== webkitContainer
    && webkitDocumentRoot.webkitRequestFullscreen
  ) {
    attempts.push({
      label: "WebKit application root",
      request: () => webkitDocumentRoot.webkitRequestFullscreen?.(),
    });
  }
  if (allowVideoFallback && webkitVideo.webkitEnterFullscreen) {
    attempts.push({
      label: "native video",
      request: () => webkitVideo.webkitEnterFullscreen?.(),
    });
  }

  for (const attempt of attempts) {
    try {
      if (await requestVerifiedFullscreenTransition(
        attempt.request,
        container,
        video,
        documentObject,
        true,
        transitionTimeoutMs,
      )) return;
      failures.push(`${attempt.label}: the browser accepted the request but did not enter fullscreen.`);
    } catch (error) {
      failures.push(`${attempt.label}: ${errorMessage(error)}`);
    }
  }

  throw fullscreenEntryError(failures);
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
    transitionTimeoutMs,
  );
  return "entered";
}
