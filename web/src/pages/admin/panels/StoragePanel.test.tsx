import React, { useState } from "react";
import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import * as api from "../../../api/storage";
import { ApiError } from "../../../api/client";
import { getSettings } from "../../../api/system";
import { StoragePanel, type StorageDraft } from "./StoragePanel";
import type { StorageStatus, StorageReview, StorageOperation } from "../../../api/storage";

vi.mock("../../../api/storage", async original => ({ ...await original<typeof import("../../../api/storage")>(), getStorageStatus: vi.fn(), getStorageRetention: vi.fn(), getStorageActivity: vi.fn(), previewStoragePolicy: vi.fn(), saveStoragePolicy: vi.fn(), getStorageOperation: vi.fn(), setStorageKeep: vi.fn(), previewStorageCleanup: vi.fn(), runStorageCleanup: vi.fn(), refreshStorage: vi.fn() }));
vi.mock("../../../api/system", () => ({ getSettings: vi.fn(), testGoogleDrive: vi.fn() }));
vi.mock("./DriveSetupCard", () => ({ DriveSetupCard: ({ onSettingsRefresh }: { onSettingsRefresh: () => void }) => <section aria-label="Existing Drive setup"><button onClick={onSettingsRefresh}>Test connection</button></section> }));
vi.mock("../SudoModal", () => ({ SudoModal: ({ isOpen, onSuccess }: { isOpen: boolean; onSuccess: () => void }) => isOpen ? <button onClick={onSuccess}>Authorize action</button> : null }));
const status: StorageStatus = { policy: { revision: 3, localBudgetBytes: 80e9, cloudBudgetBytes: 1e12, offloadEnabled: false, prefetchNextEpisode: false, hevcCompressionMode: "auto", diskReserveBytes: 8e9 }, local: { writtenBytes: 40e9, reservedBytes: 2e9, protectedBytes: 10e9, freeBytes: 140e9, diskReserveBytes: 8e9, categories: [{ key: "media", label: "Video and audio", bytes: 40e9 }], measuredAt: "2026-10-17T00:00:00Z", stale: false, errorCode: null }, cloud: { configured: true, reachable: true, healthState: "healthy", folderName: "Selected folder", usedBytes: 100e9, accountTotalBytes: 2e12, accountFreeBytes: 1.4e12, measuredAt: "2026-10-17T00:00:00Z", stale: false, errorCode: null }, offload: { state: "paused", reason: null }, capabilities: { offload: true, keepLocal: true, cleanup: true }, operation: null };
const operation: StorageOperation = { id: "receipt", kind: "policy", status: "completed", phase: "saved", createdAt: "2026-10-17T00:00:00Z", updatedAt: "2026-10-17T00:00:00Z", mediaId: null, title: null, bytesTotal: null, bytesDone: null, releasedBytes: 0, errorCode: null, message: null };
const reviewFor = (draft: api.StoragePolicyDraft): StorageReview => Object.freeze({ reviewId: "exact-review", policyRevision: 3, expiresAt: "2099-01-01T00:00:00Z", draft: Object.freeze({ ...draft }), changes: [{ field: "local_budget_bytes", before: 80e9, after: draft.localBudgetBytes }], effects: ["No files are released by saving this policy."] });
function Visit() { const [shown, setShown] = useState(true); const [draft, setDraft] = useState<StorageDraft>(); return <><button onClick={() => setShown(!shown)}>Change section</button>{shown && <StoragePanel retainedDraft={draft} onDraftChange={setDraft} />}</>; }
async function editBudget(value = "90") { fireEvent.change(await screen.findByRole("spinbutton", { name: "Total local budget" }), { target: { value } }); }
async function openReview() { await editBudget(); fireEvent.click(screen.getByRole("button", { name: "Review changes" })); await screen.findByRole("region", { name: "Review storage changes" }); }
beforeEach(() => { vi.clearAllMocks(); vi.mocked(api.getStorageStatus).mockResolvedValue(structuredClone(status)); vi.mocked(getSettings).mockResolvedValue({ storageEngine: "LOCAL", hevcCompressionMode: "auto", driveConfigured: true, driveReachable: true, rcloneRemotePath: "fixture:" }); vi.mocked(api.getStorageRetention).mockResolvedValue({ items: [], nextCursor: null }); vi.mocked(api.getStorageActivity).mockResolvedValue({ items: [], nextCursor: null }); vi.mocked(api.previewStoragePolicy).mockImplementation(async d => reviewFor(d)); vi.mocked(api.saveStoragePolicy).mockResolvedValue({ policy: { ...status.policy, localBudgetBytes: 90e9, revision: 4 }, operation }); });

describe("storage workspace", () => {
  it("shows separate measured disk, local allocation, folder budget and provider capacity without sample records", async () => {
    render(<StoragePanel />);
    const local = await screen.findByRole("region", { name: "Local storage" });
    expect(within(local).getByText("42 GB")).toBeTruthy(); expect(within(local).getByText("140 GB")).toBeTruthy();
    const cloud = screen.getByRole("region", { name: "Google Drive storage" }); expect(within(cloud).getByText("100 GB")).toBeTruthy(); expect(within(cloud).getByText("1.4 TB")).toBeTruthy();
    expect(await screen.findByText("No media is available for local retention.")).toBeTruthy(); expect(screen.queryByText(/Sample film/)).toBeNull();
    expect(screen.queryByRole("combobox", { name: "Storage engine" })).toBeNull();
  });
  it("keeps distinct pages and preserves a draft while changing views and refreshing the connection", async () => {
    render(<StoragePanel />); await editBudget("92"); fireEvent.click(screen.getByRole("link", { name: "Policy" }));
    expect(screen.queryByRole("spinbutton")).toBeNull(); expect(screen.getByRole("combobox", { name: "HEVC compression" })).toBeTruthy();
    fireEvent.click(screen.getByRole("link", { name: "Overview" })); expect((screen.getByRole("spinbutton", { name: "Total local budget" }) as HTMLInputElement).value).toBe("92");
    fireEvent.click(screen.getByRole("button", { name: "Connection details" })); fireEvent.click(screen.getByRole("button", { name: "Test connection" }));
    await waitFor(() => expect(api.getStorageStatus).toHaveBeenCalledTimes(2)); expect((screen.getByRole("spinbutton", { name: "Total local budget" }) as HTMLInputElement).value).toBe("92");
  });
  it("retains a draft and immutable review across admin section visits", async () => {
    render(<Visit />); await openReview(); fireEvent.click(screen.getByRole("button", { name: "Change section" })); fireEvent.click(screen.getByRole("button", { name: "Change section" }));
    await screen.findByRole("button", { name: "Save changes" }); expect((screen.getByRole("spinbutton", { name: "Total local budget" }) as HTMLInputElement).value).toBe("90");
    expect(api.saveStoragePolicy).not.toHaveBeenCalled(); fireEvent.click(screen.getByRole("button", { name: "Save changes" }));
    await waitFor(() => expect(api.saveStoragePolicy).toHaveBeenCalledWith(expect.objectContaining({ reviewId: "exact-review", policyRevision: 3, draft: expect.objectContaining({ localBudgetBytes: 90e9 }) }), expect.any(String)));
  });
  it("invalidates a reviewed draft on editing and blocks undersized budgets with associated errors", async () => {
    render(<StoragePanel />); await openReview(); await editBudget("1"); expect(screen.queryByRole("button", { name: "Save changes" })).toBeNull();
    const input = screen.getByRole("spinbutton", { name: "Total local budget" }); expect(input.getAttribute("aria-invalid")).toBe("true"); expect(input.getAttribute("aria-describedby")).toContain("storage-local-error"); expect(screen.getByText("Protected data needs at least 10 GB.")).toBeTruthy(); expect((screen.getByRole("button", { name: "Review changes" }) as HTMLButtonElement).disabled).toBe(true);
  });
  it("never retries a lost save acknowledgement and reconciles the same request after a section visit", async () => {
    vi.mocked(api.saveStoragePolicy).mockRejectedValue(new ApiError("Unreadable acknowledgement", 0, "invalid_response")); vi.mocked(api.getStorageOperation).mockResolvedValue(operation);
    render(<Visit />); await openReview(); fireEvent.click(screen.getByRole("button", { name: "Save changes" })); await screen.findByText(/its result is unknown/);
    const id = vi.mocked(api.saveStoragePolicy).mock.calls[0][1]; fireEvent.click(screen.getByRole("button", { name: "Change section" })); fireEvent.click(screen.getByRole("button", { name: "Change section" }));
    fireEvent.click(await screen.findByRole("button", { name: "Check status" })); await waitFor(() => expect(api.getStorageOperation).toHaveBeenCalledWith(id)); expect(api.saveStoragePolicy).toHaveBeenCalledTimes(1);
  });
  it("keeps a durable status lookup when a 409 explicitly reports an uncertain save outcome", async () => {
    vi.mocked(api.saveStoragePolicy).mockRejectedValueOnce(new ApiError("Check the operation status before retrying.", 409, "storage_save_outcome_uncertain"));
    render(<StoragePanel />); await openReview(); fireEvent.click(screen.getByRole("button", { name: "Save changes" }));
    expect(await screen.findByRole("button", { name: "Check status" })).toBeTruthy();
    expect(screen.getByText("Operation awaiting confirmation")).toBeTruthy();
    expect(api.saveStoragePolicy).toHaveBeenCalledTimes(1);
  });
  it("does not publish ready offline before the server proves readiness and removing a keep preserves usage", async () => {
    const item: api.RetentionItem = { id: "one", mediaId: "tv_8", title: "Actual title", kind: "episode", localState: "cloud", bytes: 4e9, kept: false, offlineReady: false, protectedReason: null, operationId: null };
    vi.mocked(api.getStorageRetention).mockResolvedValue({ items: [item], nextCursor: null }); vi.mocked(api.setStorageKeep).mockResolvedValue({ item: { ...item, kept: true, localState: "preparing" }, operation: { ...operation, kind: "keep_local", status: "queued" } });
    render(<StoragePanel />); fireEvent.click(await screen.findByRole("button", { name: "Keep local" }));
    await screen.findByText(/Keep requested · not ready offline/); expect(api.setStorageKeep).toHaveBeenCalledWith("one", true, expect.any(String)); expect(screen.queryByText(/Ready offline on this server/)).toBeNull(); expect(screen.getByText("42 GB")).toBeTruthy();
  });
  it("loads real activity pages and offers an explicit retry after page failure", async () => {
    vi.mocked(api.getStorageActivity).mockResolvedValueOnce({ items: [{ ...operation, title: "Actual title" }], nextCursor: "next-page" }).mockRejectedValueOnce(new Error("Activity unavailable"));
    render(<StoragePanel />); await screen.findByRole("spinbutton", { name: "Total local budget" }); fireEvent.click(screen.getByRole("link", { name: "Activity" })); await screen.findByText("Actual title · completed"); fireEvent.click(screen.getByRole("button", { name: "Load more activity" })); await screen.findByText("Activity unavailable"); expect(api.getStorageActivity).toHaveBeenLastCalledWith("next-page"); expect(screen.getByRole("button", { name: "Try again" })).toBeTruthy();
  });
  it("requires identity confirmation to recover unavailable reads without making a write", async () => {
    vi.mocked(api.getStorageStatus).mockRejectedValueOnce(new ApiError("Confirm identity", 403, "reauthentication_required")); render(<StoragePanel />); fireEvent.click(await screen.findByRole("button", { name: "Confirm identity" })); fireEvent.click(screen.getByRole("button", { name: "Authorize action" })); await screen.findByRole("region", { name: "Local storage" }); expect(api.saveStoragePolicy).not.toHaveBeenCalled();
  });
  it("reauthenticates an expired save grant without automatically repeating the rejected action", async () => {
    vi.mocked(api.saveStoragePolicy).mockRejectedValueOnce(new ApiError("Confirm identity", 403, "reauthentication_required"));
    render(<StoragePanel />); await openReview(); fireEvent.click(screen.getByRole("button", { name: "Save changes" }));
    fireEvent.click(await screen.findByRole("button", { name: "Confirm identity" }));
    fireEvent.click(screen.getByRole("button", { name: "Authorize action" }));
    await waitFor(() => expect(api.getStorageStatus).toHaveBeenCalledTimes(2));
    expect(api.saveStoragePolicy).toHaveBeenCalledTimes(1);
    expect(screen.queryByRole("button", { name: "Save changes" })).toBeNull();
    expect((screen.getByRole("spinbutton", { name: "Total local budget" }) as HTMLInputElement).value).toBe("90");
  });
  it("keeps unavailable measurements and unsupported capabilities truthful", async () => {
    vi.mocked(api.getStorageStatus).mockResolvedValue({ ...status, policy: { ...status.policy, localBudgetBytes: null, cloudBudgetBytes: null }, local: { ...status.local, writtenBytes: null, reservedBytes: null, freeBytes: null, categories: [] }, cloud: { ...status.cloud, configured: false, usedBytes: null, accountTotalBytes: null, accountFreeBytes: null }, capabilities: { offload: false, keepLocal: false, cleanup: false } });
    render(<StoragePanel />); const input = await screen.findByRole("spinbutton", { name: "Total local budget" }); expect((input as HTMLInputElement).value).toBe(""); expect((screen.getByRole("switch", { name: "Automatic offloading" }) as HTMLButtonElement).disabled).toBe(true); expect((screen.getByRole("button", { name: "Review cleanup" }) as HTMLButtonElement).disabled).toBe(true); expect(screen.getAllByText("Not measured").length).toBeGreaterThan(0);
  });
  it("does not let an old completed save replace a newer visit draft", async () => {
    let finish!: (value: { policy: api.StoragePolicy; operation: StorageOperation }) => void;
    vi.mocked(api.saveStoragePolicy).mockReturnValue(new Promise(resolve => { finish = resolve; })); render(<Visit />); await openReview(); fireEvent.click(screen.getByRole("button", { name: "Save changes" }));
    await waitFor(() => expect(api.saveStoragePolicy).toHaveBeenCalledTimes(1)); fireEvent.click(screen.getByRole("button", { name: "Change section" })); fireEvent.click(screen.getByRole("button", { name: "Change section" })); await act(async () => finish({ policy: { ...status.policy, revision: 4 }, operation })); await screen.findByRole("button", { name: "Check status" }); expect(screen.getByText("Operation awaiting confirmation")).toBeTruthy();
  });
});
