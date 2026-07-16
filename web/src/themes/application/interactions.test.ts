import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { describe, expect, it } from "vitest";

const interactions = readFileSync(resolve("src/themes/application/interactions.css"), "utf8");
const application = readFileSync(resolve("src/themes/application/application.css"), "utf8");
const ember = readFileSync(resolve("src/themes/ember/ember-application.css"), "utf8");

describe("semantic hover interaction system", () => {
  it("removes the old blanket scale and competing card hover rules", () => {
    expect(application).not.toContain("scale(1.04)");
    expect(application).not.toContain("button:not(:disabled):not(.catalog-card)");
    expect(ember).not.toContain("--tilt-x");
    expect(ember).not.toContain(".ember-media-card:hover");
  });

  it("limits spatial hover motion to fine pointers and honors reduced motion", () => {
    expect(interactions).toContain("@media (hover: hover) and (pointer: fine)");
    expect(interactions).toContain("@media (prefers-reduced-motion: reduce)");
    expect(interactions).toContain("transform: none !important");
  });

  it("defines distinct navigation and card behavior for all four themes", () => {
    for (const selector of [
      ".theme-nav--ember nav button:hover",
      ".theme-nav--aurora nav button:hover",
      ".theme-nav--cinema nav button:hover",
      ".theme-nav--gemini nav button:hover",
      '.catalog-card[data-card-theme="sharp"]:hover',
      '.catalog-card[data-card-theme="glass"]:hover',
      '.catalog-card[data-card-theme="poster"]:hover',
      '.catalog-card[data-card-theme="module"]:hover',
    ]) expect(interactions).toContain(selector);
  });

  it("covers every major interactive surface", () => {
    for (const selector of [
      ".theme-profile-control", ".feature-action", ".catalog-rail-blade",
      ".search-result", ".episode-card", ".profile-tile",
      ".profile-editor__themes button", ".admin-nav button",
      ".player-controls button", ".login-primary-action",
    ]) expect(interactions).toContain(selector);
  });
});
