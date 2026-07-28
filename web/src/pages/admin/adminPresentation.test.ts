import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { describe, expect, it } from "vitest";

const read = (path: string) => readFileSync(resolve(path), "utf8");

describe("admin presentation contracts", () => {
  it("keeps the global reset in the base cascade layer", () => {
    const index = read("src/index.css");
    expect(index).toMatch(/@layer base\s*{\s*\*, \*::before, \*::after\s*{/);
    expect(index).not.toMatch(/@import "tailwindcss";\s*\*, \*::before, \*::after/);
  });

  it("uses dedicated responsive structures for every admin control surface", () => {
    const application = read("src/themes/application/application.css");
    const gate = read("src/pages/admin/AdminGate.tsx");
    const account = read("src/pages/admin/panels/AccountPanel.tsx");
    const security = read("src/pages/AccountSecurityPage.tsx");
    const storage = read("src/pages/admin/panels/StoragePanel.tsx");
    const updates = read("src/pages/admin/panels/UpdatesPanel.tsx");
    const profileData = read("src/pages/admin/panels/ProfileDataPanel.tsx");
    const apiKeys = read("src/pages/admin/panels/ApiKeysPanel.tsx");
    const adminCenter = read("src/pages/admin/AdminCenter.tsx");

    for (const selector of [
      ".admin-auth-stage",
      ".admin-panel__header",
      ".admin-security",
      ".security-credential-grid",
      ".admin-settings-grid",
      ".admin-settings-actions",
      ".update-overview-grid",
      ".update-log-card",
      ".admin-subject-bar",
      ".profile-data-table-card",
      ".admin-panel--api-keys",
      ".api-key-readiness",
    ]) expect(application).toContain(selector);

    expect(gate).toContain('className="admin-auth-form"');
    expect(account).toContain("<AccountSecurityPage />");
    expect(security).toContain('className="admin-panel admin-panel--account admin-security"');
    expect(storage).toContain('className="admin-panel admin-panel--storage"');
    expect(updates).toContain('className="admin-panel admin-panel--updates"');
    expect(updates).toContain("Install when idle");
    expect(updates).toContain("Update now");
    expect(updates).toContain("Active ingestion, downloads, media processing, backups, and restores must finish or be cancelled first.");
    expect(updates).toContain("Retry failed target");
    expect(profileData).toContain('className="admin-panel admin-panel--profile-data"');
    expect(profileData).toContain("TMDB cache is not profile-owned");
    expect(apiKeys).toContain('className="admin-panel admin-panel--api-keys admin-security"');
    expect(apiKeys).toContain("API keys belong to the server account, not to a viewing profile");
    expect(adminCenter).toContain('aria-label="Selected profile"');
    expect(adminCenter).toContain('{ id: "api-keys", label: "API Keys" }');
    expect(adminCenter).toContain('const profileAware = section === "profiles" || section === "recommendations"');
    expect(adminCenter).toContain("<ProfileDataPanel profileId={subjectProfile.id} />");
    expect(application).toContain("@media (max-width: 760px)");
  });

  it("preserves the dedicated downloads layout inside the repaired shell", () => {
    const application = read("src/themes/application/application.css");
    const downloads = read("src/pages/admin/panels/DownloadsPanel.tsx");
    expect(downloads).toContain("ServerDownloads as DownloadsPanel");
    expect(application).toContain(".admin-content .server-downloads");
  });
});
