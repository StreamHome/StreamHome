import { describe, expect, it, vi } from "vitest";
import {
  canUsePlayerFullscreen,
  fullscreenElement,
  isPlayerFullscreen,
  togglePlayerFullscreen,
} from "./fullscreen";

interface FullscreenDocumentHarness {
  documentObject: Document;
  dispatch: (eventName: string) => void;
  setActiveElement: (element: Element | null) => void;
}

function fullscreenDocument(initialElement: Element | null = null): FullscreenDocumentHarness {
  let activeElement = initialElement;
  const events = new EventTarget();
  const documentObject = {
    get fullscreenElement() {
      return activeElement;
    },
    fullscreenEnabled: true,
    addEventListener: events.addEventListener.bind(events),
    removeEventListener: events.removeEventListener.bind(events),
    dispatchEvent: events.dispatchEvent.bind(events),
  } as unknown as Document;

  return {
    documentObject,
    dispatch: (eventName) => events.dispatchEvent(new Event(eventName)),
    setActiveElement: (element) => {
      activeElement = element;
    },
  };
}

function fullscreenContainer(
  properties: Partial<HTMLElement> = {},
  descendants: Element[] = [],
): HTMLElement {
  return Object.assign(new EventTarget(), {
    contains: (element: Element | null) => Boolean(element && descendants.includes(element)),
    ...properties,
  }) as unknown as HTMLElement;
}

function fullscreenVideo(properties: Partial<HTMLVideoElement> = {}): HTMLVideoElement {
  return Object.assign(new EventTarget(), properties) as unknown as HTMLVideoElement;
}

describe("player fullscreen controller", () => {
  it("uses the application root so the complete player can own fullscreen", async () => {
    const harness = fullscreenDocument();
    const container = fullscreenContainer();
    let documentRoot: HTMLElement;
    const rootRequest = vi.fn().mockImplementation(() => {
      harness.setActiveElement(documentRoot);
      harness.dispatch("fullscreenchange");
      return Promise.resolve();
    });
    documentRoot = fullscreenContainer({ requestFullscreen: rootRequest }, [container]);
    Object.assign(harness.documentObject, { documentElement: documentRoot });
    const containerRequest = vi.fn();
    Object.assign(container, { requestFullscreen: containerRequest });

    await expect(togglePlayerFullscreen(
      container,
      fullscreenVideo(),
      harness.documentObject,
    )).resolves.toBe("entered");
    expect(rootRequest).toHaveBeenCalledOnce();
    expect(containerRequest).not.toHaveBeenCalled();
  });

  it("enters container fullscreen only after the browser exposes the transition", async () => {
    const harness = fullscreenDocument();
    let container: HTMLElement;
    const requestFullscreen = vi.fn().mockImplementation(() => {
      harness.setActiveElement(container);
      harness.dispatch("fullscreenchange");
      return Promise.resolve();
    });
    container = fullscreenContainer({ requestFullscreen });
    const video = fullscreenVideo();

    await expect(togglePlayerFullscreen(container, video, harness.documentObject)).resolves.toBe("entered");
    expect(requestFullscreen).toHaveBeenCalledWith();
    expect(isPlayerFullscreen(container, video, harness.documentObject)).toBe(true);
  });

  it("waits for an asynchronous fullscreenchange transition", async () => {
    const harness = fullscreenDocument();
    let container: HTMLElement;
    const requestFullscreen = vi.fn().mockImplementation(() => {
      setTimeout(() => {
        harness.setActiveElement(container);
        harness.dispatch("fullscreenchange");
      }, 0);
      return Promise.resolve();
    });
    container = fullscreenContainer({ requestFullscreen });
    const video = fullscreenVideo();

    await expect(togglePlayerFullscreen(
      container,
      video,
      harness.documentObject,
      { transitionTimeoutMs: 50 },
    )).resolves.toBe("entered");
  });

  it("exits an active fullscreen session owned by the player", async () => {
    let container: HTMLElement;
    const harness = fullscreenDocument();
    const exitFullscreen = vi.fn().mockImplementation(() => {
      harness.setActiveElement(null);
      harness.dispatch("fullscreenchange");
      return Promise.resolve();
    });
    Object.assign(harness.documentObject, { exitFullscreen });
    container = fullscreenContainer();
    const video = fullscreenVideo();
    harness.setActiveElement(container);

    await expect(togglePlayerFullscreen(container, video, harness.documentObject)).resolves.toBe("exited");
    expect(exitFullscreen).toHaveBeenCalledOnce();
  });

  it("reports an exit request that never leaves fullscreen", async () => {
    const harness = fullscreenDocument();
    const container = fullscreenContainer();
    const video = fullscreenVideo();
    harness.setActiveElement(container);
    Object.assign(harness.documentObject, {
      exitFullscreen: vi.fn().mockResolvedValue(undefined),
    });

    await expect(togglePlayerFullscreen(
      container,
      video,
      harness.documentObject,
      { transitionTimeoutMs: 0 },
    )).rejects.toThrow("did not exit fullscreen");
  });

  it("falls back to standard video fullscreen after a rejected container request", async () => {
    const harness = fullscreenDocument();
    const containerRequest = vi.fn().mockRejectedValue(new Error("Container blocked"));
    const container = fullscreenContainer({ requestFullscreen: containerRequest });
    let video: HTMLVideoElement;
    const videoRequest = vi.fn().mockImplementation(() => {
      harness.setActiveElement(video);
      harness.dispatch("fullscreenchange");
      return Promise.resolve();
    });
    video = fullscreenVideo({ requestFullscreen: videoRequest });

    await expect(togglePlayerFullscreen(container, video, harness.documentObject)).resolves.toBe("entered");
    expect(containerRequest).toHaveBeenCalledOnce();
    expect(videoRequest).toHaveBeenCalledOnce();
  });

  it("falls back when a browser resolves without entering fullscreen", async () => {
    const harness = fullscreenDocument();
    const containerRequest = vi.fn().mockResolvedValue(undefined);
    const container = fullscreenContainer({ requestFullscreen: containerRequest });
    let video: HTMLVideoElement;
    const videoRequest = vi.fn().mockImplementation(() => {
      harness.setActiveElement(video);
      return Promise.resolve();
    });
    video = fullscreenVideo({ requestFullscreen: videoRequest });

    await expect(togglePlayerFullscreen(
      container,
      video,
      harness.documentObject,
      { transitionTimeoutMs: 0 },
    )).resolves.toBe("entered");
    expect(containerRequest).toHaveBeenCalledOnce();
    expect(videoRequest).toHaveBeenCalledOnce();
  });

  it("falls back when a browser leaves the fullscreen request pending", async () => {
    const harness = fullscreenDocument();
    const containerRequest = vi.fn().mockImplementation(() => new Promise<void>(() => undefined));
    const container = fullscreenContainer({ requestFullscreen: containerRequest });
    let video: HTMLVideoElement;
    const videoRequest = vi.fn().mockImplementation(() => {
      harness.setActiveElement(video);
      return Promise.resolve();
    });
    video = fullscreenVideo({ requestFullscreen: videoRequest });

    await expect(togglePlayerFullscreen(
      container,
      video,
      harness.documentObject,
      { transitionTimeoutMs: 0 },
    )).resolves.toBe("entered");
    expect(containerRequest).toHaveBeenCalledOnce();
    expect(videoRequest).toHaveBeenCalledOnce();
  });

  it("uses native WebKit video fullscreen when element fullscreen is unavailable", async () => {
    const harness = fullscreenDocument();
    let video: HTMLVideoElement;
    const webkitEnterFullscreen = vi.fn().mockImplementation(() => {
      Object.assign(video, { webkitDisplayingFullscreen: true });
      video.dispatchEvent(new Event("webkitbeginfullscreen"));
    });
    video = fullscreenVideo({ webkitEnterFullscreen } as Partial<HTMLVideoElement>);

    await expect(togglePlayerFullscreen(
      fullscreenContainer(),
      video,
      harness.documentObject,
    )).resolves.toBe("entered");
    expect(webkitEnterFullscreen).toHaveBeenCalledOnce();
  });

  it("does not surrender custom controls to video fullscreen when fallback is disabled", async () => {
    const harness = fullscreenDocument();
    const requestFullscreen = vi.fn().mockRejectedValue(new Error("Blocked"));
    const webkitEnterFullscreen = vi.fn();
    const video = fullscreenVideo({ webkitEnterFullscreen } as Partial<HTMLVideoElement>);

    await expect(togglePlayerFullscreen(
      fullscreenContainer({ requestFullscreen }),
      video,
      harness.documentObject,
      { allowVideoFallback: false, transitionTimeoutMs: 0 },
    )).rejects.toThrow("Blocked");
    expect(webkitEnterFullscreen).not.toHaveBeenCalled();
  });

  it("reports a resolved request that never changes fullscreen state", async () => {
    const harness = fullscreenDocument();
    const container = fullscreenContainer({
      requestFullscreen: vi.fn().mockResolvedValue(undefined),
    });

    await expect(togglePlayerFullscreen(
      container,
      fullscreenVideo(),
      harness.documentObject,
      { transitionTimeoutMs: 0 },
    )).rejects.toThrow("did not enter fullscreen");
  });

  it("reports an unsupported fullscreen environment", async () => {
    const harness = fullscreenDocument();
    await expect(togglePlayerFullscreen(
      fullscreenContainer(),
      fullscreenVideo(),
      harness.documentObject,
    )).rejects.toThrow("Fullscreen is unavailable");
  });

  it("does not treat or exit another element's fullscreen session as player fullscreen", async () => {
    const unrelatedElement = {} as Element;
    const harness = fullscreenDocument(unrelatedElement);
    const exitFullscreen = vi.fn();
    Object.assign(harness.documentObject, { exitFullscreen });
    const container = fullscreenContainer();
    const video = fullscreenVideo();

    expect(isPlayerFullscreen(container, video, harness.documentObject)).toBe(false);
    await expect(togglePlayerFullscreen(container, video, harness.documentObject)).rejects.toThrow("Fullscreen is unavailable");
    expect(exitFullscreen).not.toHaveBeenCalled();
  });

  it("reads standard and WebKit state and callable capabilities", () => {
    const activeElement = {} as Element;
    const webkitDocument = {
      fullscreenElement: null,
      webkitFullscreenElement: activeElement,
    } as unknown as Document;
    const video = fullscreenVideo({
      webkitDisplayingFullscreen: true,
      webkitEnterFullscreen: vi.fn(),
    } as Partial<HTMLVideoElement>);
    const container = fullscreenContainer({}, [activeElement]);

    expect(fullscreenElement(webkitDocument)).toBe(activeElement);
    expect(isPlayerFullscreen(container, video, fullscreenDocument().documentObject)).toBe(true);
    expect(canUsePlayerFullscreen(container, video)).toBe(true);
    expect(canUsePlayerFullscreen(fullscreenContainer(), fullscreenVideo())).toBe(false);
  });
});
