import React, { useState } from "react";
import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import * as api from "../../../api/system";
import type { SystemSettings } from "../../../types/api";
import { StoragePanel, type StorageDraft } from "./StoragePanel";

vi.mock("../../../api/adminDrive", () => ({ getAdminDriveSetup: vi.fn(async () => ({ configured: false, publicUrl: "https://fixture.invalid", callbackUrl: "https://fixture.invalid/api/setup/rclone/drive/callback", guideUrl: "https://console.cloud.google.com/apis/credentials", job: null })) }));
vi.mock("../../../api/system", () => ({ getSettings: vi.fn(), testGoogleDrive: vi.fn(), updateSettings: vi.fn() }));
vi.mock("../SudoModal", () => ({ SudoModal: ({ isOpen, onSuccess }: { isOpen: boolean; onSuccess: () => void }) => isOpen ? <button onClick={onSuccess}>Authorize save</button> : null }));
const settings: SystemSettings = { storageEngine: "LOCAL", hevcCompressionMode: "auto", driveConfigured: true, driveReachable: false, rcloneRemotePath: "fixture:" };

function Visit() {
  const [shown, setShown] = useState(true);
  const [draft, setDraft] = useState<StorageDraft>();
  return <><button onClick={() => setShown(!shown)}>Change section</button>{shown && <StoragePanel retainedDraft={draft} onDraftChange={setDraft} />}</>;
}
async function chooseCompression(option: string) {
  fireEvent.click(await screen.findByRole("combobox", { name: "HEVC compression" }));
  fireEvent.click(screen.getByRole("option", { name: option }));
}
beforeEach(() => { vi.clearAllMocks(); vi.mocked(api.getSettings).mockResolvedValue(settings); vi.mocked(api.updateSettings).mockImplementation(async value => value); });
describe("storage preference drafts", () => {
  it("keeps a newer draft when an earlier visit's save finishes after navigation", async () => {
    let finish!: (value: SystemSettings) => void;
    vi.mocked(api.updateSettings).mockReturnValue(new Promise(resolve => { finish = resolve; }));
    render(<Visit />);
    await chooseCompression("Always enabled");
    fireEvent.click(screen.getByRole("button", { name: "Save changes" }));
    fireEvent.click(screen.getByRole("button", { name: "Authorize save" }));
    await waitFor(() => expect(api.updateSettings).toHaveBeenCalledTimes(1));
    fireEvent.click(screen.getByRole("button", { name: "Change section" }));
    fireEvent.click(screen.getByRole("button", { name: "Change section" }));
    await chooseCompression("Disabled");
    await act(async () => finish({ ...settings, hevcCompressionMode: "on" }));
    fireEvent.click(screen.getByRole("button", { name: "Change section" }));
    fireEvent.click(screen.getByRole("button", { name: "Change section" }));
    expect((await screen.findByRole("combobox", { name: "HEVC compression" })).textContent).toBe("Disabled");
    expect(screen.getByText(/Unsaved changes/)).toBeTruthy();
  });
  it("retains edits across section visits while refreshing server connection metadata", async () => {
    render(<Visit />);
    await chooseCompression("Always enabled");
    fireEvent.click(screen.getByRole("button", { name: "Change section" }));
    vi.mocked(api.getSettings).mockResolvedValue({ ...settings, driveReachable: true });
    fireEvent.click(screen.getByRole("button", { name: "Change section" }));
    await screen.findByText("Connected");
    expect(screen.getByRole("combobox", { name: "HEVC compression" }).textContent).toBe("Always enabled");
    expect(screen.getByText(/Unsaved changes/)).toBeTruthy();
    expect(api.updateSettings).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole("button", { name: "Discard changes" }));
    expect(screen.getByRole("combobox", { name: "HEVC compression" }).textContent).toBe("Automatic");
    expect((screen.getByRole("button", { name: "Save changes" }) as HTMLButtonElement).disabled).toBe(true);
  });
  it("does not discard preferences when the Drive connection is tested", async () => {
    render(<StoragePanel />);
    await chooseCompression("Disabled");
    expect(screen.queryByRole("dialog")).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "Test connection" }));
    await waitFor(() => expect(api.getSettings).toHaveBeenCalledTimes(2));
    expect(screen.getByRole("combobox", { name: "HEVC compression" }).textContent).toBe("Disabled");
    expect(api.updateSettings).not.toHaveBeenCalled();
  });
  it("keeps unconfigured Drive status and setup visible while explanatory help opens separately", async () => {
    vi.mocked(api.getSettings).mockResolvedValue({ ...settings, driveConfigured: false, driveReachable: false });
    render(<StoragePanel />);
    expect(await screen.findByText("Not configured")).toBeTruthy();
    expect(screen.getByRole("link", { name: "Google Cloud setup guide ↗" })).toBeTruthy();
    const engine = screen.getByRole("combobox", { name: "Storage engine" });
    fireEvent.click(engine);
    const unavailableDrive = screen.getByRole("option", { name: "Google Drive" });
    expect(unavailableDrive.getAttribute("aria-disabled")).toBe("true");
    fireEvent.click(unavailableDrive);
    expect(engine.textContent).toBe("Local server storage");
    fireEvent.keyDown(engine, { key: "Escape" });
    expect(screen.queryByRole("listbox")).toBeNull();
    expect(screen.queryByRole("button", { name: "Test connection" })).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "HEVC compatibility and processing" }));
    expect(screen.getByRole("dialog", { name: "HEVC compatibility and processing" })).toBeTruthy();
    expect(screen.getByText(/may be less compatible with older devices/)).toBeTruthy();
    expect(api.updateSettings).not.toHaveBeenCalled();
    expect(api.testGoogleDrive).not.toHaveBeenCalled();
  });
  it("retains field help and submits configured Drive selection only after authorization", async () => {
    render(<StoragePanel />);
    const engine = await screen.findByRole("combobox", { name: "Storage engine" });
    expect(engine.getAttribute("aria-describedby")).toBe("storage-engine-help");
    expect(screen.getByRole("combobox", { name: "HEVC compression" }).getAttribute("aria-describedby")).toBe("hevc-mode-help");
    fireEvent.keyDown(engine, { key: "ArrowDown" });
    fireEvent.keyDown(engine, { key: "End" });
    fireEvent.keyDown(engine, { key: "Enter" });
    expect(engine.textContent).toBe("Google Drive");
    expect(api.updateSettings).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole("button", { name: "Save changes" }));
    expect(api.updateSettings).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole("button", { name: "Authorize save" }));
    await waitFor(() => expect(api.updateSettings).toHaveBeenCalledWith({ ...settings, storageEngine: "CLOUD" }));
  });
});
