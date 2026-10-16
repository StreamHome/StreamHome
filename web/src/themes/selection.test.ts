import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { describe, expect, it } from "vitest";

const readCss = (path: string) => readFileSync(resolve(path), "utf8");

describe("theme text selection", () => {
  it("uses inherited theme tokens for selected text", () => {
    const index = readCss("src/index.css");
    expect(index).toContain("background-color: var(--selection-background");
    expect(index).toContain("color: var(--selection-foreground");
  });

  it("defines a distinct readable selection palette for every theme", () => {
    const palettes = [
      ["src/themes/ember/ember.css", "#ff7a3d", "#1e100b"],
      ["src/themes/aurora/aurora.css", "#d8c7ff", "#050505"],
      ["src/themes/cinema/cinema.css", "#e50914", "#ffffff"],
    ] as const;

    for (const [path, background, foreground] of palettes) {
      const css = readCss(path);
      expect(css).toContain(`--selection-background: ${background}`);
      expect(css).toContain(`--selection-foreground: ${foreground}`);
    }
    expect(new Set(palettes.map(([, background]) => background)).size).toBe(3);
  });
});
