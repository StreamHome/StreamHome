import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { describe, expect, it } from "vitest";

const playerPage = readFileSync(resolve("src/pages/player/PlayerPage.tsx"), "utf8");
const playerStyles = readFileSync(resolve("src/index.css"), "utf8");
const applicationStyles = readFileSync(resolve("src/themes/application/application.css"), "utf8");

describe("dedicated mobile player presentation", () => {
  it("keeps phone controls separate from the desktop control surface", () => {
    expect(playerPage).toContain('className="mobile-player-chrome"');
    expect(playerPage).toContain('className="mobile-player-topbar"');
    expect(playerPage).toContain('className="mobile-player-transport"');
    expect(playerPage).toContain('className="mobile-player-bottom"');
    expect(playerPage).toContain('!mobilePlayer && showControls && phase !== "ended"');
  });

  it("omits phone volume controls and gates subtitles in both presentations", () => {
    const mobileStart = playerPage.indexOf('className="mobile-player-chrome"');
    const desktopStart = playerPage.indexOf('!mobilePlayer && showControls && phase !== "ended"');
    const mobilePresentation = playerPage.slice(mobileStart, desktopStart);

    expect(mobilePresentation).not.toContain('className="player-volume"');
    expect(mobilePresentation).not.toContain('icon={muted ? "mute" : "volume"}');
    expect((playerPage.match(/\{hasSubtitles && \(/g) ?? [])).toHaveLength(2);
    expect((playerPage.match(/\{hasSubtitles && preferences\.subtitleTrackId !== "off" && \(/g) ?? [])).toHaveLength(4);
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
    const interactiveShortcutGuard = playerPage.indexOf("if (isInteractiveTarget(event.target)) return;");
    const fullscreenShortcut = playerPage.indexOf('if (shortcut === "fullscreen")');

    expect(playerPage).toContain("onClick={mobilePlayer ? undefined : handleDesktopSurfaceClick}");
    expect(playerPage).toContain("onDoubleClick={mobilePlayer ? undefined : handleDesktopSurfaceDoubleClick}");
    expect(playerPage).toContain("desktopClickTimerRef.current = window.setTimeout");
    expect(interactiveShortcutGuard).toBeGreaterThan(-1);
    expect(fullscreenShortcut).toBeGreaterThan(interactiveShortcutGuard);
    expect(playerPage).not.toContain('target.closest("button, input');
    expect(playerPage).toContain("toggleFullscreen();");
    expect(playerPage).toContain('document.addEventListener("keydown", handleKeyDown, true)');
    expect(playerPage).toContain('document.addEventListener("keyup", handleKeyUp, true)');
    expect(playerPage).toContain("event.stopImmediatePropagation()");
    expect(playerPage).toContain("tabIndex={-1}");
    expect(playerPage).toContain("data-theme={theme}");
    expect(playerPage).toContain("data-player-theme={definition.playerVariant}");
    expect(playerStyles).toContain("var(--player-accent)");
    expect(playerPage).toContain("sourceMetadata.sourceFormat");
    expect(playerPage).not.toContain("disabled={!fullscreenAvailable}");
    expect((playerPage.match(/availableAudioTracks\.length > 0/g) ?? [])).toHaveLength(2);
  });

  it("holds the last decoded frame and keeps status polling away from transport credentials", () => {
    expect(playerPage).toContain('className="player-last-frame"');
    expect(playerPage).toContain('data-frame-hold={holdLastFrame ? "true" : "false"}');
    expect(playerPage).toContain("captureLastFrame(true)");
    expect(playerPage).toContain("mergePlaybackRunMetadata(active, refreshed)");
    expect(playerPage).toContain("shouldAcceptObservedPlaybackTime(");
    expect(playerStyles).toContain('.player-view[data-frame-hold="true"] .player-last-frame');
  });

  it("serializes progress keepalives and prefers transport-synchronized adaptive audio", () => {
    const adaptiveAudioSelection = playerPage.indexOf("const transportTrack = pendingAudioSelectionRef.current");
    const directAudioSelection = playerPage.indexOf("const directTrack = pendingAudioSelectionRef.current");

    expect(playerPage).not.toContain("if (keepalive) {");
    expect(playerPage).toContain("progressQueueRef.current = progressQueueRef.current");
    expect(playerPage).toContain('playbackProgressFailureAction(error)');
    expect(adaptiveAudioSelection).toBeGreaterThan(-1);
    expect(directAudioSelection).toBeGreaterThan(adaptiveAudioSelection);
  });

  it("centers player range thumbs and removes the heavy Ember control effect", () => {
    const timelineStart = playerStyles.indexOf(".player-timeline {");
    const timelineEnd = playerStyles.indexOf(".player-view video::cue", timelineStart);
    const timelineStyles = playerStyles.slice(timelineStart, timelineEnd);
    const emberControls = applicationStyles.match(/\.player-view\[data-player-theme="terminal"\] \.player-controls \{[^}]+\}/)?.[0] ?? "";

    expect(timelineStyles).toContain("--player-timeline-track-size: 0.2rem");
    expect(timelineStyles).toContain("color-mix(in srgb, var(--player-accent) 88%, white 12%)");
    expect(timelineStyles).toContain("--player-timeline-thumb-size: 0.58rem");
    expect(timelineStyles).toContain("box-sizing: border-box");
    expect(timelineStyles).toContain("--player-volume-track-size: 0.28rem");
    expect(timelineStyles).toContain("--player-volume-thumb-size: 0.85rem");
    expect(timelineStyles).toContain("margin-top: calc((var(--player-volume-track-size) - var(--player-volume-thumb-size)) / 2)");
    expect(timelineStyles).not.toContain("background: white");
    expect(playerStyles).toContain('.player-view[data-player-theme="terminal"] .player-control-button');
    expect(emberControls).toContain("backdrop-filter: none");
    expect(emberControls).toContain("box-shadow: none");
    expect(emberControls).toContain("rgba(0,0,0,.42)");
    expect(emberControls).not.toContain("rgba(15,6,3,.46)");
  });

  it("renders captions above visible controls and keeps exit actions opaque", () => {
    expect(playerPage).toContain('className="player-caption-layer"');
    expect(playerPage).toContain('label="Subtitle timing"');
    expect(playerStyles).toContain('bottom: max(clamp(9.5rem, 20vh, 13rem), calc(env(safe-area-inset-bottom) + 1.5rem))');
    expect(playerStyles).toContain('.player-view[data-mobile-player="true"] .player-caption-layer');
    expect(playerStyles).not.toContain('[data-controls-visible="true"]:not([data-mobile-player="true"]) .player-caption-layer');
    expect(playerStyles).not.toContain('[data-controls-visible="true"][data-mobile-player="true"] .player-caption-layer');
    expect(playerStyles).toContain('background: color-mix(in srgb, var(--player-control) 92%, black 8%)');
  });
});
