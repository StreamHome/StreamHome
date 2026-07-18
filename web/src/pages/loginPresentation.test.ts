import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { describe, expect, it } from "vitest";

describe("Linear-structured Ember login presentation", () => {
  it("keeps Ember identity while removing the old terminal effects", () => {
    const component = readFileSync(resolve("src/pages/LoginPage.tsx"), "utf8");
    const css = readFileSync(resolve("src/pages/login.css"), "utf8");
    expect(component).not.toContain("EmberBackground");
    expect(component).not.toContain("ScanLines");
    expect(component).not.toContain("Initialize Connection");
    expect(component).toContain("Sign in to StreamHome");
    expect(css).toContain("--login-ember: #ff6b35");
    expect(css).toContain("--login-peach: #ffb59c");
    expect(css).toContain(".login-auth-card");
  });

  it("defines responsive and reduced-motion contracts", () => {
    const css = readFileSync(resolve("src/pages/login.css"), "utf8");
    expect(css).toContain("100dvh");
    expect(css).toContain("@media (max-width: 560px)");
    expect(css).toContain("@media (max-height: 700px)");
    expect(css).toContain("@media (prefers-reduced-motion: reduce)");
  });
});
