import { afterEach, describe, expect, it, vi } from "vitest";
import { getEpisodes, normalizeMovie } from "./movies";
import { normalizeSkipMarkers } from "./skipMarkers";

afterEach(() => vi.unstubAllGlobals());

describe("skip-marker API normalization", () => {
  it("preserves separate intervals and arbitrary valid custom group names", () => {
    expect(normalizeSkipMarkers({
      intro: [{ start: 0, end: 32 }],
      credits: [{ start: 5964, end: 6000 }, { start: 6100, end: 6166 }],
      cold_open: [{ start: 100, end: 110 }],
      Özeti: [{ start: 40, end: 50 }],
    })).toEqual({
      intro: [{ start: 0, end: 32 }],
      credits: [{ start: 5964, end: 6000 }, { start: 6100, end: 6166 }],
      cold_open: [{ start: 100, end: 110 }],
      Özeti: [{ start: 40, end: 50 }],
    });
  });

  it("keeps legacy decimal numeric timestamps without accepting general coercion", () => {
    expect(normalizeSkipMarkers({ intro: [
      { start: " 0 ", end: "32.5" },
      { start: ".5", end: "1e2" },
      { start: "-0", end: "+1.0" },
    ] })).toEqual({ intro: [{ start: 0, end: 32.5 }, { start: 0.5, end: 100 }, { start: 0, end: 1 }] });
    for (const value of [null, undefined, true, false, "", " ", "0x10", "1:30", "12 seconds", "Infinity", NaN, Infinity, -Infinity, [], {}]) {
      expect(normalizeSkipMarkers({ intro: [{ start: value, end: 32 }] })).toEqual({});
      expect(normalizeSkipMarkers({ intro: [{ start: 0, end: value }] })).toEqual({});
    }
  });

  it("rejects negative, reversed, zero-length and malformed ranges independently", () => {
    expect(normalizeSkipMarkers({ intro: [
      { start: -1, end: 10 }, { start: 0, end: -10 }, { start: 5, end: 4 },
      { start: 5, end: 5 }, { end: 10 }, { start: 0 }, null, true, [], [0, 10],
      { start: 1, end: 2, extra: "ignored wire field" },
    ] })).toEqual({ intro: [{ start: 1, end: 2 }] });
  });

  it("rejects invalid containers and group names without discarding valid siblings", () => {
    for (const raw of [null, undefined, true, 1, "{}", [], [{ start: 0, end: 10 }]]) {
      expect(normalizeSkipMarkers(raw)).toEqual({});
    }
    expect(normalizeSkipMarkers({
      "": [{ start: 0, end: 1 }], "has space": [{ start: 0, end: 1 }],
      ["a".repeat(33)]: [{ start: 0, end: 1 }], "<intro>": [{ start: 0, end: 1 }],
      empty: [], broken: { start: 0, end: 1 }, intro: [{ start: 0, end: 1 }],
    })).toEqual({ intro: [{ start: 0, end: 1 }] });
  });

  it("treats prototype-like custom names as ordinary data", () => {
    const result = normalizeSkipMarkers(JSON.parse('{"__proto__":[{"start":0,"end":1}],"constructor":[{"start":2,"end":3}]}'));
    expect(Object.keys(result)).toEqual(["__proto__", "constructor"]);
    expect(Object.getPrototypeOf(result)).toBe(Object.prototype);
    expect(result.__proto__).toEqual([{ start: 0, end: 1 }]);
  });

  it("normalizes movie and embedded episode metadata at the same boundary", () => {
    const movie = normalizeMovie({
      skipMarkers: { intro: [{ start: "0", end: "32" }, { start: null, end: 100 }] },
      episodes: [{ skipMarkers: { recap: [{ start: "1", end: "12" }, { start: false, end: 100 }] } }],
    });
    expect(movie.skipMarkers).toEqual({ intro: [{ start: 0, end: 32 }] });
    expect(movie.episodes?.[0].skipMarkers).toEqual({ recap: [{ start: 1, end: 12 }] });
  });

  it("normalizes standalone episode responses before exposing them to consumers", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(new Response(JSON.stringify([
      { id: "episode", skipMarkers: { credits: [{ start: "90", end: "100" }, { start: true, end: 100 }] } },
    ]), { status: 200 })));
    expect((await getEpisodes("series"))[0].skipMarkers).toEqual({ credits: [{ start: 90, end: 100 }] });
  });
});
