import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { describe, expect, it } from "vitest";

const application = readFileSync(resolve("src/themes/application/application.css"), "utf8");
const ember = readFileSync(resolve("src/themes/ember/ember-application.css"), "utf8");

describe("phone billboard and navigation presentation", () => {
  it("makes every compatibility-theme phone billboard compact and artwork-first", () => {
    expect(application).toContain("/* Compact, artwork-first phone billboards */");
    expect(application).toContain(".terminal-feature { position: relative; min-height: 430px");
    expect(application).toContain(".feature-stage--editorial { position: relative; min-height: 430px");
    expect(application).toContain(".feature-stage--cinematic { min-height: 440px");
    for (const selector of [".terminal-feature__copy > p", ".editorial-copy > p", ".cinema-copy > p"]) {
      expect(application).toContain(selector);
    }
  });

  it("gives Ember the same compact artwork-first behavior without losing its signal styling", () => {
    expect(ember).toContain("/* Compact, artwork-first phone billboard */");
    expect(ember).toContain(".ember-hero { min-height: 430px");
    expect(ember).toContain(".ember-hero__copy > p { display: none; }");
    expect(ember).toContain(".ember-action { min-height: 34px");
    expect(ember).toContain(".ember-billboard__pagination { top: 14px");
  });
});
