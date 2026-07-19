import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { describe, expect, it } from "vitest";

const read = (path: string) => readFileSync(resolve(path), "utf8");

describe("Google Drive setup presentation", () => {
  it("keeps Drive inside the setup wizard with an accordion and documentation link", () => {
    const page = read("src/pages/SetupPage.tsx");
    expect(page).toContain('className="setup-step-accordion"');
    expect(page).toContain("Learn how to create the required Client ID");
    expect(page).toContain("Continue with Google");
    expect(page).toContain("Run read/write test");
    expect(page).toContain("Activate Google Drive");
  });

  it("does not restore the generic interactive Rclone provider questionnaire", () => {
    const api = read("src/api/setup.ts");
    const page = read("src/pages/SetupPage.tsx");
    expect(api).not.toContain("RcloneQuestion");
    expect(api).not.toContain("config/continue");
    expect(page).not.toContain("or add a provider");
  });

  it("documents the app-owned callback and preserves the standard media directories", () => {
    const docs = read("../docs/google-drive.md");
    expect(docs).toContain("/api/setup/rclone/drive/callback");
    expect(docs).toContain("Rclone runs only on the StreamHome server");
    const setup = read("src/pages/SetupPage.tsx");
    expect(setup).toContain("standard server/media catalog");
  });
});
