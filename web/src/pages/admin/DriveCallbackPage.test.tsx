import React, { StrictMode } from "react";
import { act, cleanup, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { DriveCallbackPage } from "./DriveCallbackPage";
let fetcher: ReturnType<typeof vi.fn>;
const response = (job: unknown) => new Response(JSON.stringify({ configured: false, publicUrl: "https://fixture.invalid", callbackUrl: "https://fixture.invalid/api/setup/rclone/drive/callback", guideUrl: "https://console.cloud.google.com/apis/credentials", job }), { status: 200 });
beforeEach(() => { fetcher = vi.fn(); vi.stubGlobal("fetch", fetcher); });
afterEach(() => { cleanup(); vi.unstubAllGlobals(); vi.restoreAllMocks(); window.history.replaceState(null, "", "/"); });
describe("Admin Drive callback", () => {
  it("does not trust callback query strings or navigate to supplied addresses", async () => {
    window.history.replaceState(null, "", "/admin/drive/callback?drive=connected&job=untrusted&return=https://attacker.invalid");
    fetcher.mockResolvedValue(response(null)); render(<DriveCallbackPage />);
    await screen.findByText("Return to Storage to check the current connection status.");
    expect(screen.getByRole("link", { name: "Return to Storage" }).getAttribute("href")).toBe("/admin/storage");
    expect(fetcher.mock.calls[0][0]).toBe("/api/system/drive/setup");
    expect(screen.queryByText(/Authorization received/)).toBeNull();
  });
  it("only acknowledges server-confirmed session-owned authorization", async () => {
    fetcher.mockResolvedValue(response({ id: "synthetic-job", remoteName: "streamhome-drive", pendingOperation: null, generation: 1, selectedPath: "", status: "selecting_folder", progress: "Choose folder", audience: "external", publishingStatus: "production", expiresAt: 2000000000 }));
    render(<DriveCallbackPage />); await screen.findByText(/Authorization received/);
  });
  it("rejects stale callback completion across StrictMode cleanup", async () => {
    let finish!: (value: Response) => void;
    fetcher.mockReturnValueOnce(new Promise(resolve => { finish = resolve; })).mockResolvedValue(response(null));
    render(<StrictMode><DriveCallbackPage /></StrictMode>);
    await screen.findByText("Return to Storage to check the current connection status.");
    await act(async () => finish(response({ id: "stale", remoteName: "streamhome-drive", pendingOperation: null, generation: 1, selectedPath: "", status: "selecting_folder", progress: "Choose folder", audience: "external", publishingStatus: "production", expiresAt: 2000000000 })));
    expect(screen.queryByText(/Authorization received/)).toBeNull();
  });
});
