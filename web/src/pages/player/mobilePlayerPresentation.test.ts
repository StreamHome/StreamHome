import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { describe, expect, it } from "vitest";

const playerPage = readFileSync(resolve("src/pages/player/PlayerPage.tsx"), "utf8");
const playerStyles = readFileSync(resolve("src/index.css"), "utf8");

describe("dedicated mobile player presentation", () => {
  it("keeps phone controls separate from the desktop control surface", () => {
    expect(playerPage).toContain('className="mobile-player-chrome"');
    expect(playerPage).toContain('className="mobile-player-topbar"');
    expect(playerPage).toContain('className="mobile-player-transport"');
    expect(playerPage).toContain('className="mobile-player-bottom"');
    expect(playerPage).toContain('!mobilePlayer && (showControls || phase !== "playing")');
  });

  it("omits phone volume controls and gates subtitles in both presentations", () => {
    const mobileStart = playerPage.indexOf('className="mobile-player-chrome"');
    const desktopStart = playerPage.indexOf('!mobilePlayer && (showControls || phase !== "playing")');
    const mobilePresentation = playerPage.slice(mobileStart, desktopStart);

    expect(mobilePresentation).not.toContain('className="player-volume"');
    expect(mobilePresentation).not.toContain('icon={muted ? "mute" : "volume"}');
    expect((playerPage.match(/\{hasSubtitles && \(/g) ?? [])).toHaveLength(2);
    expect((playerPage.match(/\{hasSubtitles && preferences\.subtitleTrackId !== "off" && \(/g) ?? [])).toHaveLength(2);
  });

  it("defines automatic locked-portrait landscape fallback, safe areas, gesture feedback, and touch tooltip suppression", () => {
    expect(playerStyles).toContain('[data-mobile-orientation="forced-landscape"]');
    expect(playerStyles).toContain('transform: translate(-50%, -50%) rotate(90deg)');
    expect(playerPage).toContain("startMobilePlayback()");
    expect(playerPage).toContain("{ allowVideoFallback: true }");
    expect(playerPage).not.toContain('icon="rotate"');
    expect(playerPage).not.toContain('label="Start over"');
    expect(playerPage).toContain("const startOver = useCallback");
    expect(playerPage).toContain("startOverPlaybackRun(runResponse.runId)");
    expect(playerPage).toContain('className="mobile-player-topbar__actions"');
    expect(playerPage).toContain('className="mobile-player-exit"');
    expect(playerStyles).toContain('.mobile-player-seek-feedback');
    expect(playerStyles).toContain('env(safe-area-inset-right)');
    expect(playerStyles).toContain('@media (hover: none), (pointer: coarse)');
    expect(playerStyles).toContain('.player-control-button::after');
  });

  it("uses frame-synchronized progress, guarded pointer gestures, and a stable seek indicator", () => {
    expect(playerPage).toContain("window.requestAnimationFrame(updateTimeline)");
    expect(playerPage).toContain("isMobileTapCandidate(");
    expect(playerPage).toContain('onPointerDown={(event) => handleMobilePointerDown("left", event)}');
    expect(playerPage).not.toContain("key={mobileSeekFeedback.nonce}");
    expect(playerPage).toContain("seek(currentTimeRef.current + result.seekDelta, false)");
    expect(playerStyles).toContain("touch-action: pan-y");
  });

  it("keeps player actions theme-aware and gives desktop blank-space clicks standard media behavior", () => {
    expect(playerPage).toContain("onClick={mobilePlayer ? undefined : handleDesktopSurfaceClick}");
    expect(playerPage).toContain("onDoubleClick={mobilePlayer ? undefined : handleDesktopSurfaceDoubleClick}");
    expect(playerPage).toContain("desktopClickTimerRef.current = window.setTimeout");
    expect(playerPage).toContain('event.key.toLowerCase() === "f"');
    expect(playerPage).toContain("toggleFullscreen();");
    expect(playerPage).toContain("data-theme={theme}");
    expect(playerPage).toContain("data-player-theme={definition.playerVariant}");
    expect(playerStyles).toContain("var(--player-accent)");
    expect(playerPage).toContain("sourceMetadata.sourceFormat");
  });

  it("holds the last decoded frame and keeps status polling away from transport credentials", () => {
    expect(playerPage).toContain('className="player-last-frame"');
    expect(playerPage).toContain('data-frame-hold={holdLastFrame ? "true" : "false"}');
    expect(playerPage).toContain("captureLastFrame(true)");
    expect(playerPage).toContain("mergePlaybackRunMetadata(active, refreshed)");
    expect(playerPage).toContain("shouldAcceptObservedPlaybackTime(");
    expect(playerStyles).toContain('.player-view[data-frame-hold="true"] .player-last-frame');
  });
});
