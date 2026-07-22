import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { describe, expect, it } from "vitest";

const read = (path: string) => readFileSync(resolve(path), "utf8");
const motionSystem = read("src/motion/motionSystem.tsx");
const interactions = read("src/themes/application/interactions.css");
const application = read("src/themes/application/application.css");
const ember = read("src/themes/ember/ember-application.css");
const login = read("src/pages/login.css");

describe("motion performance guardrails", () => {
  it("keeps route choreography concurrent and free of animated blur", () => {
    expect(motionSystem).not.toContain('mode="wait"');
    expect(motionSystem).not.toMatch(/filter:\s*["']blur/);
    expect(application).not.toContain("@keyframes player-panel-enter { from { opacity: 0; filter:");
  });

  it("does not animate large ambient layers continuously", () => {
    expect(login).not.toContain("animation: login-ember-drift");
    expect(application).not.toContain("animation: cinema-ambient-sweep");
    expect(ember).not.toContain("animation: ember-hero-drift");
  });

  it("keeps shared interaction transitions on compositor-friendly properties", () => {
    expect(interactions).not.toMatch(/transition[^;]*(filter|box-shadow|letter-spacing|padding)/);
    expect(interactions).not.toContain("will-change: transform");
    expect(interactions).not.toMatch(/transition:\s*all/);
  });
});
