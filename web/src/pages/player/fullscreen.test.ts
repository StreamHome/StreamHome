import { describe, expect, it, vi } from "vitest";
import {
  canUsePlayerFullscreen,
  fullscreenElement,
  isPlayerFullscreen,
  togglePlayerFullscreen,
} from "./fullscreen";

function fullscreenDocument(): Document {
  return {
    fullscreenElement: null,
    fullscreenEnabled: true,
  } as unknown as Document;
}

describe("player fullscreen controller", () => {
  it("enters container fullscreen through the standard API", async () => {
    const requestFullscreen = vi.fn().mockResolvedValue(undefined);
    const container = { requestFullscreen } as unknown as HTMLElement;
    const video = {} as HTMLVideoElement;

    await expect(togglePlayerFullscreen(container, video, fullscreenDocument())).resolves.toBe("entered");
    expect(requestFullscreen).toHaveBeenCalledWith({ navigationUI: "hide" });
  });

  it("exits an active standard fullscreen session", async () => {
    const exitFullscreen = vi.fn().mockResolvedValue(undefined);
    const activeElement = {} as Element;
    const documentObject = {
      fullscreenElement: activeElement,
      fullscreenEnabled: true,
      exitFullscreen,
    } as unknown as Document;

    await expect(togglePlayerFullscreen({} as HTMLElement, {} as HTMLVideoElement, documentObject)).resolves.toBe("exited");
    expect(exitFullscreen).toHaveBeenCalledOnce();
  });

  it("uses native WebKit video fullscreen when element fullscreen is unavailable", async () => {
    const webkitEnterFullscreen = vi.fn();
    const video = { webkitEnterFullscreen } as unknown as HTMLVideoElement;

    await expect(togglePlayerFullscreen({} as HTMLElement, video, fullscreenDocument())).resolves.toBe("entered");
    expect(webkitEnterFullscreen).toHaveBeenCalledOnce();
  });

  it("falls back to native WebKit video fullscreen after a rejected element request", async () => {
    const requestFullscreen = vi.fn().mockRejectedValue(new Error("Blocked"));
    const webkitEnterFullscreen = vi.fn();
    const video = { webkitEnterFullscreen } as unknown as HTMLVideoElement;

    await expect(togglePlayerFullscreen({ requestFullscreen } as unknown as HTMLElement, video, fullscreenDocument())).resolves.toBe("entered");
    expect(webkitEnterFullscreen).toHaveBeenCalledOnce();
  });

  it("does not surrender custom controls to native video fullscreen when fallback is disabled", async () => {
    const requestFullscreen = vi.fn().mockRejectedValue(new Error("Blocked"));
    const webkitEnterFullscreen = vi.fn();
    const video = { webkitEnterFullscreen } as unknown as HTMLVideoElement;

    await expect(togglePlayerFullscreen(
      { requestFullscreen } as unknown as HTMLElement,
      video,
      fullscreenDocument(),
      { allowVideoFallback: false },
    )).rejects.toThrow("Blocked");
    expect(webkitEnterFullscreen).not.toHaveBeenCalled();
  });

  it("reports an unsupported fullscreen environment", async () => {
    const documentObject = { fullscreenElement: null, fullscreenEnabled: false } as unknown as Document;
    await expect(togglePlayerFullscreen({} as HTMLElement, {} as HTMLVideoElement, documentObject)).rejects.toThrow("Fullscreen is unavailable");
  });

  it("reads standard and WebKit state and capabilities", () => {
    const activeElement = {} as Element;
    const webkitDocument = { fullscreenElement: null, webkitFullscreenElement: activeElement } as unknown as Document;
    const video = { webkitDisplayingFullscreen: true, webkitEnterFullscreen: vi.fn() } as unknown as HTMLVideoElement;

    expect(fullscreenElement(webkitDocument)).toBe(activeElement);
    expect(isPlayerFullscreen(video, fullscreenDocument())).toBe(true);
    expect(canUsePlayerFullscreen({} as HTMLElement, video, fullscreenDocument())).toBe(true);
  });
});
