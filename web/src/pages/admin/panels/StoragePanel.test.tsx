import React, { useState } from "react";
import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import * as api from "../../../api/storage";
import { ApiError } from "../../../api/client";
import { getSettings } from "../../../api/system";
import { StoragePanel, mergeStoragePage, formatStorageBytes, type StorageDraft } from "./StoragePanel";
import type { StorageStatus, StorageReview, StorageOperation } from "../../../api/storage";

vi.mock("../../../api/storage", async original => ({ ...await original<typeof import("../../../api/storage")>(), getStorageStatus: vi.fn(), getStorageRetention: vi.fn(), getStorageActivity: vi.fn(), previewStoragePolicy: vi.fn(), saveStoragePolicy: vi.fn(), getStorageOperation: vi.fn(), setStorageKeep: vi.fn(), previewStorageCleanup: vi.fn(), runStorageCleanup: vi.fn(), refreshStorage: vi.fn() }));
vi.mock("../../../api/system", () => ({ getSettings: vi.fn(), testGoogleDrive: vi.fn() }));
vi.mock("./DriveSetupCard", () => ({ DriveSetupCard: ({ onSettingsRefresh }: { onSettingsRefresh: () => void }) => <section aria-label="Existing Drive setup"><button onClick={onSettingsRefresh}>Test connection</button></section> }));
vi.mock("../SudoModal", () => ({ SudoModal: ({ isOpen, onSuccess }: { isOpen: boolean; onSuccess: () => void }) => isOpen ? <button onClick={onSuccess}>Authorize action</button> : null }));
const status: StorageStatus = { policy: { revision: 3, localBudgetBytes: 80e9, cloudBudgetBytes: 1e12, offloadEnabled: false, prefetchNextEpisode: false, hevcCompressionMode: "auto", diskReserveBytes: 8e9 }, local: { writtenBytes: 40e9, reservedBytes: 2e9, protectedBytes: 10e9, freeBytes: 140e9, diskReserveBytes: 8e9, categories: [{ key: "media", label: "Video and audio", bytes: 40e9 }], measuredAt: "2026-10-17T00:00:00Z", stale: false, errorCode: null }, cloud: { configured: true, reachable: true, healthState: "healthy", folderName: "Selected folder", usedBytes: 100e9, accountTotalBytes: 2e12, accountFreeBytes: 1.4e12, measuredAt: "2026-10-17T00:00:00Z", stale: false, errorCode: null }, offload: { state: "paused", reason: null }, capabilities: { offload: true, keepLocal: true, cleanup: true }, operation: null };
const operation: StorageOperation = { id: "receipt", kind: "policy", status: "completed", phase: "saved", createdAt: "2026-10-17T00:00:00Z", updatedAt: "2026-10-17T00:00:00Z", mediaId: null, title: null, bytesTotal: null, bytesDone: null, releasedBytes: 0, errorCode: null, message: null };
const reviewFor = (draft: api.StoragePolicyDraft): StorageReview => Object.freeze({ reviewId: "exact-review", policyRevision: 3, expiresAt: "2099-01-01T00:00:00Z", draft: Object.freeze({ ...draft }), changes: [{ field: "local_budget_bytes", before: 80e9, after: draft.localBudgetBytes }], effects: ["No files are released by saving this policy."] });
function Visit() { const [shown, setShown] = useState(true); const [draft, setDraft] = useState<StorageDraft>(); return <><button onClick={() => setShown(!shown)}>Change section</button><div hidden={!shown}><StoragePanel active={shown} retainedDraft={draft} onDraftChange={setDraft} /></div></>; }
async function editBudget(value = "90") {
  if (!screen.queryByRole("spinbutton", { name: "Total local budget" })) fireEvent.click(await screen.findByRole("link", { name: "Policy" }));
  fireEvent.change(await screen.findByRole("spinbutton", { name: "Total local budget" }), { target: { value } });
}
async function openReview() { await editBudget(); fireEvent.click(screen.getByRole("button", { name: "Review changes" })); await screen.findByRole("region", { name: "Review storage changes" }); }
const flush = async () => { await act(async () => { for (let i = 0; i < 15; i++) await Promise.resolve(); }); };
const advance = async (ms: number) => { await act(async () => { vi.advanceTimersByTime(ms); for (let i = 0; i < 15; i++) await Promise.resolve(); }); };
const visibility = (state: "visible" | "hidden") => { Object.defineProperty(document, "visibilityState", { configurable: true, value: state }); fireEvent(document, new Event("visibilitychange")); };
afterEach(() => { vi.useRealTimers(); Object.defineProperty(document, "visibilityState", { configurable: true, value: "visible" }); });
beforeEach(() => { vi.clearAllMocks(); vi.mocked(api.getStorageStatus).mockResolvedValue(structuredClone(status)); vi.mocked(getSettings).mockResolvedValue({ storageEngine: "LOCAL", hevcCompressionMode: "auto", driveConfigured: true, driveReachable: true, rcloneRemotePath: "fixture:" }); vi.mocked(api.getStorageRetention).mockResolvedValue({ items: [], nextCursor: null }); vi.mocked(api.getStorageActivity).mockResolvedValue({ items: [], nextCursor: null }); vi.mocked(api.previewStoragePolicy).mockImplementation(async d => reviewFor(d)); vi.mocked(api.saveStoragePolicy).mockResolvedValue({ policy: { ...status.policy, localBudgetBytes: 90e9, revision: 4 }, operation }); });

describe("storage workspace", () => {
  it("shows measured totals, read-only summaries and direct Policy links", async () => {
    render(<StoragePanel />);
    const local = await screen.findByRole("region", { name: "Local storage" });
    expect(within(local).getByText("42 GB")).toBeTruthy();
    expect(within(local).getByText("140 GB")).toBeTruthy();
    expect(within(local).getByRole("list", { name: "Usage distribution legend" }).textContent).toContain("Reserved · 2 GB");
    expect(screen.queryByRole("spinbutton")).toBeNull();
    expect(screen.queryByRole("switch")).toBeNull();
    for (const name of ["Refresh usage", "Check connection", "Check status", "Refresh activity", "Discard changes", "Review changes"]) expect(screen.queryByRole("button", { name })).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "Edit local budget →" }));
    expect(screen.getByRole("spinbutton", { name: "Total local budget" })).toBeTruthy();
    expect(screen.getByRole("spinbutton", { name: "Total cloud budget" })).toBeTruthy();
    expect(screen.getByRole("switch", { name: "Automatic offloading" })).toBeTruthy();
    expect(screen.getByRole("checkbox", { name: /Prepare the next episode/ })).toBeTruthy();
    expect(screen.getByRole("combobox", { name: "HEVC compression" })).toBeTruthy();
    await flush();
  });
  it("retains an immutable review through section visits without saving it", async () => {
    render(<Visit />); await openReview();
    fireEvent.click(screen.getByRole("button", { name: "Change section" }));
    fireEvent.click(screen.getByRole("button", { name: "Change section" }));
    expect((screen.getByRole("spinbutton", { name: "Total local budget" }) as HTMLInputElement).value).toBe("90");
    expect(api.saveStoragePolicy).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole("button", { name: "Save changes" }));
    await waitFor(() => expect(api.saveStoragePolicy).toHaveBeenCalledWith(expect.objectContaining({ reviewId: "exact-review", policyRevision: 3, draft: expect.objectContaining({ localBudgetBytes: 90e9 }) }), expect.any(String)));
  });
  it("validates budgets and invalidates reviews on editing", async () => {
    render(<StoragePanel />); await openReview(); await editBudget("1");
    expect(screen.queryByRole("button", { name: "Save changes" })).toBeNull();
    const input = screen.getByRole("spinbutton", { name: "Total local budget" });
    expect(input.getAttribute("aria-invalid")).toBe("true");
    expect(input.getAttribute("aria-describedby")).toContain("storage-local-error");
    expect(screen.getByText("Protected data needs at least 10 GB.")).toBeTruthy();
    expect((screen.getByRole("button", { name: "Review changes" }) as HTMLButtonElement).disabled).toBe(true);
    await editBudget("90");
    fireEvent.click(screen.getByRole("switch", { name: "Automatic offloading" }));
    await editBudget("");
    expect(screen.getByText("Set a total budget before enabling automatic offloading.")).toBeTruthy();
  });
  it("automatically reads at idle cadence without any measurement POST", async () => {
    vi.useFakeTimers(); render(<StoragePanel />); await flush();
    expect(api.getStorageStatus).toHaveBeenCalledTimes(1);
    await advance(14999); expect(api.getStorageStatus).toHaveBeenCalledTimes(1);
    await advance(1); expect(api.getStorageStatus).toHaveBeenCalledTimes(2);
    expect(api.getStorageRetention).toHaveBeenCalledTimes(2);
    expect(api.refreshStorage).not.toHaveBeenCalled();
    expect(api.previewStoragePolicy).not.toHaveBeenCalled();
    expect(api.setStorageKeep).not.toHaveBeenCalled();
  });
  it("uses the active cadence for server refresh work and retains last successful values", async () => {
    vi.useFakeTimers();
    vi.mocked(api.getStorageStatus).mockResolvedValue({ ...status, local: { ...status.local, refresh: { state: "running", phase: "scanning_local", lastAttemptAt: "2026-10-17T00:00:00Z", lastSuccessAt: status.local.measuredAt, nextAttemptAt: null, errorCode: null } } });
    render(<StoragePanel />); await flush();
    expect(screen.getByText("Measuring local usage…")).toBeTruthy();
    expect(screen.getByText("42 GB")).toBeTruthy();
    await advance(3000); expect(api.getStorageStatus).toHaveBeenCalledTimes(2);
    expect(api.refreshStorage).not.toHaveBeenCalled();
  });
  it("aborts hidden reads, fences late results and coalesces focus with an in-flight read", async () => {
    vi.useFakeTimers(); let finish!: (s: StorageStatus) => void;
    vi.mocked(api.getStorageStatus).mockReturnValueOnce(new Promise(resolve => { finish = resolve; }));
    const mounted = render(<StoragePanel />); await flush();
    const signal = vi.mocked(api.getStorageStatus).mock.calls[0][0]!.signal!;
    fireEvent(window, new Event("focus")); expect(api.getStorageStatus).toHaveBeenCalledTimes(1);
    visibility("hidden"); expect(signal.aborted).toBe(true);
    await advance(30000); expect(api.getStorageStatus).toHaveBeenCalledTimes(1);
    visibility("visible"); await flush(); expect(api.getStorageStatus).toHaveBeenCalledTimes(2);
    await act(async () => finish({ ...status, local: { ...status.local, writtenBytes: 999e9 } }));
    expect(within(screen.getByRole("region", { name: "Local storage" })).getByText("42 GB")).toBeTruthy();
    fireEvent(window, new Event("focus")); await flush(); expect(api.getStorageStatus).toHaveBeenCalledTimes(3);
    const lastSignal = vi.mocked(api.getStorageStatus).mock.calls[2][0]!.signal!;
    mounted.unmount(); expect(lastSignal.aborted).toBe(false);
    await advance(60000); expect(api.getStorageStatus).toHaveBeenCalledTimes(3);
  });
  it("aborts an in-flight request on unmount or inactive section and keeps cached draft", async () => {
    vi.useFakeTimers(); const mounted = render(<StoragePanel />); await flush();
    fireEvent.click(screen.getByRole("link", { name: "Policy" })); await flush();
    fireEvent.change(screen.getByRole("spinbutton", { name: "Total local budget" }), { target: { value: "92" } });
    let finish!: (s: StorageStatus) => void;
    vi.mocked(api.getStorageStatus).mockReturnValueOnce(new Promise(resolve => { finish = resolve; }));
    fireEvent(window, new Event("focus")); await flush();
    const signal = vi.mocked(api.getStorageStatus).mock.lastCall![0]!.signal!;
    mounted.rerender(<StoragePanel active={false} />); expect(signal.aborted).toBe(true);
    await act(async () => finish(status));
    mounted.rerender(<StoragePanel active />); await flush();
    expect((screen.getByRole("spinbutton", { name: "Total local budget" }) as HTMLInputElement).value).toBe("92");
    vi.mocked(api.getStorageStatus).mockReturnValueOnce(new Promise(() => undefined));
    fireEvent(window, new Event("focus")); await flush();
    const unmountSignal = vi.mocked(api.getStorageStatus).mock.lastCall![0]!.signal!;
    mounted.unmount(); expect(unmountSignal.aborted).toBe(true);
  });
  it("aborts an explicit older-page read when hidden and restores pagination on return", async () => {
    vi.mocked(api.getStorageActivity).mockResolvedValueOnce({ items: [{ ...operation, title: "First" }], nextCursor: "older" }).mockReturnValueOnce(new Promise(() => undefined)).mockResolvedValue({ items: [{ ...operation, title: "First" }], nextCursor: "older" });
    render(<StoragePanel view="activity" />); await screen.findByText("First · Completed");
    fireEvent.click(screen.getByRole("button", { name: "Load more activity" }));
    const signal = vi.mocked(api.getStorageActivity).mock.lastCall![1]!.signal!;
    visibility("hidden"); expect(signal.aborted).toBe(true);
    visibility("visible"); await flush();
    expect((screen.getByRole("button", { name: "Load more activity" }) as HTMLButtonElement).disabled).toBe(false);
    fireEvent.click(screen.getByRole("button", { name: "Load more activity" }));
    await waitFor(() => expect(api.getStorageActivity).toHaveBeenCalledTimes(4));
  });
  it("refreshes server status without overwriting dirty drafts or exact reviews", async () => {
    render(<StoragePanel />); await openReview();
    vi.mocked(api.getStorageStatus).mockResolvedValue({ ...status, local: { ...status.local, writtenBytes: 50e9 } });
    fireEvent(window, new Event("focus")); await flush();
    expect((screen.getByRole("spinbutton", { name: "Total local budget" }) as HTMLInputElement).value).toBe("90");
    fireEvent.click(screen.getByRole("button", { name: "Save changes" }));
    await waitFor(() => expect(api.saveStoragePolicy).toHaveBeenCalledWith(expect.objectContaining({ reviewId: "exact-review" }), expect.any(String)));
  });
  it("reconciles unknown outcomes by the original ID and never replays a write", async () => {
    vi.mocked(api.saveStoragePolicy).mockRejectedValue(new ApiError("Unreadable acknowledgement", 0, "invalid_response"));
    vi.mocked(api.getStorageOperation).mockResolvedValue(operation);
    render(<StoragePanel />); await openReview(); fireEvent.click(screen.getByRole("button", { name: "Save changes" }));
    await waitFor(() => expect(api.getStorageOperation).toHaveBeenCalled());
    expect(api.getStorageOperation).toHaveBeenCalledWith(vi.mocked(api.saveStoragePolicy).mock.calls[0][1], expect.objectContaining({ signal: expect.any(AbortSignal) }));
    expect(api.saveStoragePolicy).toHaveBeenCalledTimes(1);
    await waitFor(() => expect(screen.queryByText("Operation awaiting confirmation")).toBeNull());
    expect(screen.getByRole("spinbutton", { name: "Total local budget" })).toBeTruthy();
    expect(screen.queryByText("Loading storage…")).toBeNull();
  });
  it("retains the visible policy draft if the read after a successful receipt fails", async () => {
    vi.mocked(api.saveStoragePolicy).mockRejectedValueOnce(new ApiError("Unknown", 409, "storage_save_outcome_uncertain"));
    vi.mocked(api.getStorageOperation).mockImplementationOnce(async () => {
      vi.mocked(api.getStorageStatus).mockRejectedValueOnce(new Error("Status temporarily unavailable"));
      return operation;
    });
    render(<StoragePanel />); await openReview();
    vi.useFakeTimers(); fireEvent.click(screen.getByRole("button", { name: "Save changes" })); await flush();
    const input = screen.getByRole("spinbutton", { name: "Total local budget" }) as HTMLInputElement;
    expect(input.value).toBe("90"); expect(input.disabled).toBe(true);
    expect(screen.queryByText("Loading storage…")).toBeNull();
    expect(screen.queryByText("Operation awaiting confirmation")).toBeNull();
    expect(screen.getByText(/Policy accepted. Waiting for the saved settings/)).toBeTruthy();
    vi.mocked(api.getStorageStatus).mockResolvedValue({ ...status, policy: { ...status.policy, revision: 4, localBudgetBytes: 90e9 } });
    await advance(3000);
    expect((screen.getByRole("spinbutton", { name: "Total local budget" }) as HTMLInputElement).disabled).toBe(false);
    expect(api.saveStoragePolicy).toHaveBeenCalledTimes(1);
  });
  it("keeps 409 uncertainty and 404 receipts pending, backs off and stops on reauthentication", async () => {
    vi.mocked(api.saveStoragePolicy).mockRejectedValueOnce(new ApiError("Uncertain", 409, "storage_save_outcome_uncertain"));
    vi.mocked(api.getStorageOperation).mockRejectedValueOnce(new ApiError("Not found", 404, "not_found")).mockRejectedValue(new ApiError("Confirm identity", 403, "reauthentication_required"));
    render(<StoragePanel />); await openReview();
    vi.useFakeTimers(); fireEvent.click(screen.getByRole("button", { name: "Save changes" })); await flush();
    expect(screen.getByText("Operation awaiting confirmation")).toBeTruthy();
    expect(screen.getByText(/not confirmed this request yet/)).toBeTruthy();
    expect(api.getStorageOperation).toHaveBeenCalledTimes(1);
    await advance(2999); expect(api.getStorageOperation).toHaveBeenCalledTimes(1);
    await advance(1); expect(api.getStorageOperation).toHaveBeenCalledTimes(2);
    expect(screen.getByRole("button", { name: "Confirm identity" })).toBeTruthy();
    const count = vi.mocked(api.getStorageStatus).mock.calls.length;
    fireEvent(window, new Event("focus")); await advance(120000);
    expect(api.getStorageStatus).toHaveBeenCalledTimes(count);
    expect(api.saveStoragePolicy).toHaveBeenCalledTimes(1);
    expect(screen.getByText("Operation awaiting confirmation")).toBeTruthy();
  });
  it("distinguishes accepted background work from uncertainty and allows policy editing", async () => {
    const item: api.RetentionItem = { id: "one", mediaId: "tv_8", title: "Actual title", kind: "episode", localState: "cloud", bytes: 4e9, kept: false, offlineReady: false, protectedReason: null, operationId: null };
    vi.mocked(api.getStorageRetention).mockResolvedValue({ items: [item], nextCursor: null });
    vi.mocked(api.getStorageOperation).mockResolvedValue({ ...operation, status: "queued" });
    vi.mocked(api.setStorageKeep).mockResolvedValue({ item: { ...item, kept: true, localState: "preparing" }, operation: { ...operation, status: "queued" } });
    render(<StoragePanel />); fireEvent.click(await screen.findByRole("button", { name: "Keep local" }));
    await waitFor(() => expect(api.getStorageOperation).toHaveBeenCalled());
    expect(screen.queryByText("Operation awaiting confirmation")).toBeNull();
    await editBudget("92");
    expect((screen.getByRole("spinbutton", { name: "Total local budget" }) as HTMLInputElement).disabled).toBe(false);
    expect((screen.getByRole("button", { name: "Review changes" }) as HTMLButtonElement).disabled).toBe(false);
    expect(api.setStorageKeep).toHaveBeenCalledTimes(1);
  });
  it("keeps loaded history and updates duplicate head records without resetting scroll", async () => {
    vi.mocked(api.getStorageActivity).mockResolvedValueOnce({ items: [{ ...operation, id: "head", title: "First", status: "running" }], nextCursor: "older" })
      .mockResolvedValueOnce({ items: [{ ...operation, id: "older", title: "Older" }], nextCursor: null })
      .mockResolvedValue({ items: [{ ...operation, id: "new", title: "New" }, { ...operation, id: "head", title: "First" }], nextCursor: "older" });
    render(<StoragePanel view="activity" />); await screen.findByText("First · Running");
    fireEvent.click(screen.getByRole("button", { name: "Load more activity" })); await screen.findByText("Older · Completed");
    const region = screen.getByRole("region", { name: "Storage activity" }); region.scrollTop = 280;
    fireEvent(window, new Event("focus")); await screen.findByText("New · Completed");
    expect(screen.getByText("First · Completed")).toBeTruthy(); expect(screen.getByText("Older · Completed")).toBeTruthy();
    expect(region.scrollTop).toBe(280); expect(screen.queryByRole("button", { name: "Load more activity" })).toBeNull();
    expect(screen.queryByRole("button", { name: "Review changes" })).toBeNull();
  });
  it("reauthenticates reads without replaying an expired save", async () => {
    vi.mocked(api.saveStoragePolicy).mockRejectedValueOnce(new ApiError("Confirm identity", 403, "reauthentication_required"));
    render(<StoragePanel />); await openReview(); fireEvent.click(screen.getByRole("button", { name: "Save changes" }));
    fireEvent.click(await screen.findByRole("button", { name: "Confirm identity" })); fireEvent.click(screen.getByRole("button", { name: "Authorize action" }));
    await waitFor(() => expect(screen.queryByRole("button", { name: "Confirm identity" })).toBeNull());
    expect(api.saveStoragePolicy).toHaveBeenCalledTimes(1); expect(screen.queryByRole("button", { name: "Save changes" })).toBeNull();
    expect((screen.getByRole("spinbutton", { name: "Total local budget" }) as HTMLInputElement).value).toBe("90");
  });
  it("labels an unset budget as a distribution and restores focus after breakdown", async () => {
    vi.mocked(api.getStorageStatus).mockResolvedValue({ ...status, policy: { ...status.policy, localBudgetBytes: null } });
    render(<StoragePanel />); await screen.findByText("Written usage distribution · no budget limit set");
    expect(screen.getByRole("list", { name: "Usage distribution legend" })).toBeTruthy();
    const trigger = screen.getByRole("button", { name: "View breakdown" }); fireEvent.click(trigger);
    const region = screen.getByRole("region", { name: "Local allocation" });
    fireEvent.keyDown(region, { key: "Escape" }); expect(document.activeElement).toBe(trigger);
    expect(screen.queryByRole("region", { name: "Local allocation" })).toBeNull();
  });
  it("keeps unseen pagination gaps reachable while preserving an overlapping deeper continuation", () => {
    const old = { items: [{ id: "old" }], nextCursor: null };
    expect(mergeStoragePage({ items: [], nextCursor: null }, { items: [{ id: "new" }], nextCursor: "more" }, false).nextCursor).toBe("more");
    const gap = mergeStoragePage(old, { items: [{ id: "new" }], nextCursor: "gap" }, false);
    expect(gap).toEqual({ items: [{ id: "new" }, { id: "old" }], nextCursor: "gap" });
    const deeper = mergeStoragePage({ items: [{ id: "old" }, { id: "older" }], nextCursor: "deep" }, { items: [{ id: "new" }, { id: "old" }], nextCursor: "head-next" }, false);
    expect(deeper.nextCursor).toBe("deep");
    expect(deeper.items).toHaveLength(3);
  });
  it("refreshes loaded older retention rows through accepted background completion", async () => {
    const item: api.RetentionItem = { id: "old-item", mediaId: "tv_8", title: "Older episode", kind: "episode", localState: "cloud", bytes: 4e9, kept: false, offlineReady: false, protectedReason: null, operationId: null };
    let ready = false;
    vi.mocked(api.getStorageRetention).mockImplementation(async cursor => cursor === "older" ? { items: [{ ...item, kept: ready, offlineReady: ready, localState: ready ? "both" : "cloud" }], nextCursor: null } : { items: [], nextCursor: "older" });
    vi.mocked(api.setStorageKeep).mockResolvedValue({ item: { ...item, kept: true, localState: "preparing" }, operation: { ...operation, status: "queued" } });
    vi.mocked(api.getStorageOperation).mockImplementation(async () => { ready = true; return operation; });
    render(<StoragePanel />); fireEvent.click(await screen.findByRole("button", { name: "Load more media" }));
    fireEvent.click(await screen.findByRole("button", { name: "Keep local" }));
    await screen.findByText(/Ready offline on this server/);
    expect(api.getStorageRetention).toHaveBeenLastCalledWith("older", expect.objectContaining({ signal: expect.any(AbortSignal) }));
    expect(api.setStorageKeep).toHaveBeenCalledTimes(1);
    expect(screen.queryByText(/Keep requested · not ready offline/)).toBeNull();
  });
  it("explains measurement failures without claiming a missing successful sample", async () => {
    vi.mocked(api.getStorageStatus).mockResolvedValue({ ...status, local: { ...status.local, measuredAt: null, writtenBytes: null, reservedBytes: null, errorCode: "storage_inventory_unreadable" } });
    render(<StoragePanel />); await screen.findByRole("region", { name: "Local storage" });
    const local = screen.getByRole("region", { name: "Local storage" });
    expect(within(local).getByText("Some managed files could not be read. Check the server's storage access permissions.")).toBeTruthy();
    expect(within(local).getByText("No successful measurement is available yet. Automatic checks continue.")).toBeTruthy();
    expect(within(local).queryByText(/Last successful values are retained/)).toBeNull();
    expect(formatStorageBytes(335002)).toBe("335 KB");
  });
  it("uses product labels for measurement activity rather than raw kind and phase values", async () => {
    vi.mocked(api.getStorageActivity).mockResolvedValue({ items: [{ ...operation, kind: "refresh", phase: "measured" }], nextCursor: null });
    render(<StoragePanel view="activity" />);
    await screen.findByText("Storage measurement · Completed");
    expect(screen.getByText("Usage measurement finished")).toBeTruthy();
    expect(screen.queryByText("Refresh · Completed")).toBeNull();
  });
  it("shows separate last-good measurement errors while preserving values", async () => {
    vi.mocked(api.getStorageStatus).mockResolvedValue({ ...status, local: { ...status.local, stale: true, errorCode: "local_measurement_failed" }, cloud: { ...status.cloud, stale: true, errorCode: "provider_unavailable" } });
    render(<StoragePanel />); await screen.findByRole("region", { name: "Local storage" });
    expect(screen.getByText("42 GB")).toBeTruthy();
    expect(screen.getAllByText("Last successful values are retained. Automatic checks continue.")).toHaveLength(2);
    expect(screen.getByText("Local usage could not be measured.")).toBeTruthy();
    expect(screen.getByText("Google Drive could not be reached.")).toBeTruthy();
    expect(screen.queryByText("Settings up to date")).toBeNull();
  });
});

it("preserves an explicit append that starts during a background head refresh", async () => {
  let finishHead!: (value: api.StoragePage<StorageOperation>) => void;
  let finishAppend!: (value: api.StoragePage<StorageOperation>) => void;
  vi.mocked(api.getStorageActivity)
    .mockResolvedValueOnce({ items: [{ ...operation, id: "head", title: "Head" }], nextCursor: "older" })
    .mockResolvedValueOnce({ items: [{ ...operation, id: "older", title: "Older" }], nextCursor: "deep" })
    .mockReturnValueOnce(new Promise(resolve => { finishHead = resolve; }))
    .mockReturnValueOnce(new Promise(resolve => { finishAppend = resolve; }))
    .mockResolvedValue({ items: [{ ...operation, id: "older", title: "Older" }], nextCursor: "deep" });
  render(<StoragePanel view="activity" />);
  await screen.findByText("Head · Completed");
  fireEvent.click(screen.getByRole("button", { name: "Load more activity" }));
  await screen.findByText("Older · Completed");
  fireEvent(window, new Event("focus"));
  await waitFor(() => expect(api.getStorageActivity).toHaveBeenCalledTimes(3));
  fireEvent.click(screen.getByRole("button", { name: "Load more activity" }));
  await waitFor(() => expect(api.getStorageActivity).toHaveBeenCalledTimes(4));
  await act(async () => finishHead({ items: [{ ...operation, id: "head", title: "Head" }], nextCursor: "older" }));
  await flush();
  expect(api.getStorageActivity).toHaveBeenCalledTimes(4);
  await act(async () => finishAppend({ items: [{ ...operation, id: "deep", title: "Deep" }], nextCursor: null }));
  expect(screen.getByText("Deep · Completed")).toBeTruthy();
});
