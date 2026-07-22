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
    expect((playerPage.match(/\{hasSubtitles && preferences\.subtitleLanguage !== "off" && \(/g) ?? [])).toHaveLength(2);
  });

  it("defines locked-portrait landscape fallback, safe areas, gesture feedback, and touch tooltip suppression", () => {
    expect(playerStyles).toContain('[data-mobile-orientation="forced-landscape"]');
    expect(playerStyles).toContain('transform: translate(-50%, -50%) rotate(90deg)');
    expect(playerStyles).toContain('.mobile-player-seek-feedback');
    expect(playerStyles).toContain('env(safe-area-inset-right)');
    expect(playerStyles).toContain('@media (hover: none), (pointer: coarse)');
    expect(playerStyles).toContain('.player-control-button::after');
  });
});
