import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { describe, expect, it } from "vitest";

const interactions = readFileSync(resolve("src/themes/application/interactions.css"), "utf8");
const application = readFileSync(resolve("src/themes/application/application.css"), "utf8");
const ember = readFileSync(resolve("src/themes/ember/ember-application.css"), "utf8");
const index = readFileSync(resolve("src/index.css"), "utf8");

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
    for (const profile of ["terminal", "editorial", "cinematic", "workspace"]) {
      expect(interactions).toContain(`data-interaction="${profile}"`);
    }
  });

  it("uses fluid search sizing and compact centered rail controls", () => {
    expect(application).toContain("width: clamp(150px,18vw,260px)");
    expect(application).toContain("font-size: clamp(10px,.76vw,13px)");
    expect(application).toContain(".theme-nav--gemini .theme-search { width: calc(100% - 28px); min-width: 0; max-width: none; height: 42px; min-height: 42px; flex: 0 0 42px");
    expect(application).toContain(".theme-nav--gemini .theme-search button { min-width: 64px");
    expect(application).toContain("top: 50%; bottom: auto; width: 42px; height: 42px");
    expect(ember).toContain("top: 50%; bottom: auto; width: 40px; height: 40px");
  });

  it("gives Cinema navigation an opaque scrolled state and centered brand clearance", () => {
    expect(application).toContain('.theme-nav--cinema[data-scrolled="true"]');
    expect(application).toContain("background-color: rgba(12,12,14,.96)");
    expect(application).toContain("padding: 6px 4vw 2px");
    expect(application).toContain(".theme-nav--cinema .theme-brand { height: 100%; display: flex; align-items: center");
    expect(application).not.toContain(".theme-nav--cinema[data-scrolled=\"true\"] { border-bottom");
  });

  it("renders billboard state as a synchronized filling progress track", () => {
    expect(application).toContain("width: clamp(76px,7vw,104px)");
    expect(ember).toContain("width: clamp(80px,7vw,108px)");
    expect(application).toContain("animation: billboard-progress var(--billboard-rotation-duration,10s) linear forwards");
    expect(ember).toContain("animation: billboard-progress var(--billboard-rotation-duration,10s) linear forwards");
    expect(application).not.toContain('button[data-active="true"]::after { animation: none');
    expect(interactions).not.toContain('button[data-active="true"]::after { animation: none');
  });

  it("gives every theme a distinct billboard selector palette", () => {
    for (const gradient of [
      "linear-gradient(90deg,#ffb59c,#ff5f1f)",
      "linear-gradient(90deg,#fff,#d8c7ff)",
      "linear-gradient(90deg,#e50914,#ff4d57)",
      "linear-gradient(90deg,#4285f4,#9b72cb,#d96570,#f4b400)",
    ]) expect(application).toContain(`--billboard-progress-fill: ${gradient}`);
    expect(application).toContain("background: var(--billboard-marker");
    expect(application).toContain("background: var(--billboard-progress-fill");
    expect(ember).toContain("background: var(--billboard-progress-fill");
    expect(interactions).toContain("background: var(--billboard-progress-fill");
  });

  it("keeps resume controls at the artwork bottom-right without vertical badge strips", () => {
    expect(index).toContain("bottom: calc(74px + 0.75rem)");
    expect(index).toContain(".ember-media-card__resume { bottom: calc(92px + 0.75rem); }");
    expect(index).toContain('.catalog-card-shell[data-card-theme="poster"] .catalog-card__resume { bottom: .75rem;');
    expect(index).not.toContain("box-shadow: inset 2px 0 #ff5f1f");
    expect(ember).not.toContain("box-shadow: inset 2px 0 #ff5f1f");
    expect(application).not.toContain("box-shadow: inset 2px 0 #e50914");
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
