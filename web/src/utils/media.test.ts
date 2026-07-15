import { describe, expect, it } from "vitest";
import { completionFraction, downloadFraction, isServerArtworkUrl, normalizeTheme } from "./media";

describe("server media rules", () => {
  it("accepts only server media paths or absolute HTTP URLs", () => {
    expect(isServerArtworkUrl("/media/Movies/title/poster.jpg")).toBe(true);
    expect(isServerArtworkUrl("https://media.example/poster.jpg")).toBe(true);
    expect(isServerArtworkUrl("/poster.jpg")).toBe(false);
    expect(isServerArtworkUrl("data:image/png;base64,test")).toBe(false);
    expect(isServerArtworkUrl("")).toBe(false);
  });

  it("normalizes legacy and unknown themes", () => {
    expect(normalizeTheme("netflix")).toBe("cinema");
    expect(normalizeTheme("aurora")).toBe("aurora");
    expect(normalizeTheme("unknown")).toBe("ember");
  });

  it("normalizes playback and server download progress", () => {
    expect(completionFraction(1.5)).toBe(1);
    expect(completionFraction(-1)).toBe(0);
    expect(downloadFraction(45)).toBe(0.45);
    expect(downloadFraction(250)).toBe(1);
  });
});
