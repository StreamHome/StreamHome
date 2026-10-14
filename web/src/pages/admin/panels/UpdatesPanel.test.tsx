import React from "react";
import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { ApiError } from "../../../api/client";
import * as updates from "../../../api/updates";
import type { UpdateStatus } from "../../../types/api";
import { UpdatesPanel } from "./UpdatesPanel";

vi.mock("../../../api/updates", () => ({
  getUpdateStatus: vi.fn(),
  checkForUpdates: vi.fn(),
  installUpdate: vi.fn(),
  cancelPendingUpdate: vi.fn(),
  updateUpdatePolicy: vi.fn(),
}));

vi.mock("../SudoModal", () => ({
  SudoModal: ({ isOpen, onSuccess, actionLabel }: { isOpen: boolean; onSuccess: () => void; actionLabel: string }) =>
    isOpen ? <button onClick={onSuccess}>Authorize {actionLabel}</button> : null,
}));

const baseStatus: UpdateStatus = {
  phase: "update_available",
  message: "A newer StreamHome commit is available.",
  currentCommit: "a".repeat(40),
  targetCommit: "b".repeat(40),
  updateAvailable: true,
  automatic: false,
  installMode: "when_idle",
  queuedAt: null,
  startedAt: null,
  finishedAt: null,
  lastCheckedAt: 1_720_000_000,
  lastSuccessAt: null,
  updatedAt: 1_720_000_100,
  recoveryRequestedAt: null,
  transactionId: "transaction-1234567890",
  diagnosticId: "",
  failedTarget: "",
  error: "",
  blockers: ["1 active browser session"],
  maintenanceWindowOpen: true,
  updateInProgress: false,
  logTail: ["preflight ready"],
  policy: {
    automaticUpdates: false,
    idleMinutes: 10,
    checkIntervalHours: 6,
    maintenanceStart: null,
    maintenanceEnd: null,
    branch: "main",
    requireSignedCommits: false,
  },
};

describe("UpdatesPanel", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.mocked(updates.getUpdateStatus).mockResolvedValue(baseStatus);
    vi.mocked(updates.checkForUpdates).mockResolvedValue(baseStatus);
    vi.mocked(updates.installUpdate).mockImplementation(async (mode) => ({ ...baseStatus, phase: "queued", installMode: mode, message: "Queued" }));
    vi.mocked(updates.updateUpdatePolicy).mockResolvedValue(baseStatus);
  });

  it("shows the server clock beside maintenance times without browser conversion", async () => {
    vi.mocked(updates.getUpdateStatus).mockResolvedValue({ ...baseStatus, serverClock: { timeZone: "EEST", utcOffset: "UTC+03:00", localTime: "2026-10-13 15:30:00" } });
    render(<UpdatesPanel />);
    expect(await screen.findByText("EEST (UTC+03:00)", { exact: false })).toBeTruthy();
    expect(screen.getByText("2026-10-13 15:30:00")).toBeTruthy();
    expect(screen.queryByText(/Server time zone unavailable/)).toBeNull();
    expect(updates.updateUpdatePolicy).not.toHaveBeenCalled();
  });

  it("provides an actionable unavailable-clock state for older servers", async () => {
    render(<UpdatesPanel />);
    expect(await screen.findByText(/Server time zone unavailable.*Refresh status/)).toBeTruthy();
    expect(screen.getByText(/ask the server operator to confirm the host time zone/)).toBeTruthy();
    expect(updates.updateUpdatePolicy).not.toHaveBeenCalled();
  });

  it("checks for updates and queues installation through protected authorization", async () => {
    render(<UpdatesPanel />);
    await screen.findByText("A newer StreamHome commit is available.");

    fireEvent.click(screen.getByRole("button", { name: "Check now" }));
    await waitFor(() => expect(updates.checkForUpdates).toHaveBeenCalledTimes(1));

    fireEvent.click(screen.getByRole("button", { name: "Install when idle" }));
    fireEvent.click(screen.getByRole("button", { name: "Authorize queue the update for idle time" }));
    await waitFor(() => expect(updates.installUpdate).toHaveBeenCalledWith("when_idle"));
    expect(await screen.findByText(/Update queued/)).toBeTruthy();
  });
  it("presents operator recovery with read-only refresh instead of ordinary install or policy actions", async () => {
    vi.mocked(updates.getUpdateStatus).mockResolvedValue({ ...baseStatus, phase: "recovery_required", message: "Recovery required", failedTarget: baseStatus.targetCommit });
    render(<UpdatesPanel />);
    await screen.findByRole("heading", { name: "Ask the server operator to recover this update" });
    expect(screen.queryByRole("button", { name: "Update now" })).toBeNull();
    expect(screen.queryByRole("button", { name: "Install when idle" })).toBeNull();
    expect(screen.queryByRole("button", { name: "Retry failed target" })).toBeNull();
    expect(screen.queryByText("Waiting for idle")).toBeNull();
    expect((screen.getByRole("button", { name: "Save update policy" }) as HTMLButtonElement).disabled).toBe(true);
    expect(screen.getByRole("checkbox", { name: /Automatic updates/ }).closest("fieldset")?.disabled).toBe(true);
    expect((screen.getByRole("combobox", { name: "Idle grace period" }) as HTMLButtonElement).disabled).toBe(true);
    vi.mocked(updates.getUpdateStatus).mockResolvedValue(baseStatus);
    fireEvent.click(screen.getByRole("button", { name: "Refresh status" }));
    await screen.findByRole("button", { name: "Update now" });
    expect(updates.checkForUpdates).not.toHaveBeenCalled();
    expect(updates.installUpdate).not.toHaveBeenCalled();
    expect(updates.updateUpdatePolicy).not.toHaveBeenCalled();
  });

  it("confirms and authorizes an immediate preflight separately from idle installation", async () => {
    render(<UpdatesPanel />);
    await screen.findByText("A newer StreamHome commit is available.");

    fireEvent.click(screen.getByRole("button", { name: "Update now" }));
    expect(await screen.findByRole("heading", { name: "Update StreamHome now?" })).toBeTruthy();
    expect(screen.getByText(/Browser presence, playback/)).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "Continue to authorization" }));
    fireEvent.click(screen.getByRole("button", { name: "Authorize install the update now" }));

    await waitFor(() => expect(updates.installUpdate).toHaveBeenCalledWith("now"));
    expect(await screen.findByText(/Official update\.sh launch requested/)).toBeTruthy();
    expect(screen.getByText("Immediate mode ignores the window")).toBeTruthy();
  });

  it("starts confirmation reading at its heading and keeps backward focus inside the dialog", async () => {
    render(<UpdatesPanel />);
    const trigger = await screen.findByRole("button", { name: "Update now" });
    trigger.focus();
    fireEvent.click(trigger);
    const heading = screen.getByRole("heading", { name: "Update StreamHome now?" });
    expect(document.activeElement).toBe(heading);
    expect(screen.getByRole("dialog").scrollTop).toBe(0);
    fireEvent.keyDown(heading, { key: "Tab", shiftKey: true });
    expect(document.activeElement).toBe(screen.getByRole("button", { name: "Continue to authorization" }));
    fireEvent.keyDown(document.activeElement!, { key: "Tab" });
    expect(document.activeElement).toBe(screen.getByRole("button", { name: "Cancel" }));
    fireEvent.keyDown(document.activeElement!, { key: "Escape" });
    await waitFor(() => expect(document.activeElement).toBe(trigger));
    expect(updates.installUpdate).not.toHaveBeenCalled();
  });

  it("saves administrator-selected idle and automatic-update policy", async () => {
    render(<UpdatesPanel />);
    await screen.findByText("A newer StreamHome commit is available.");

    fireEvent.click(screen.getByRole("checkbox", { name: /Automatic updates/ }));
    fireEvent.click(screen.getByRole("combobox", { name: "Idle grace period" }));
      fireEvent.click(screen.getByRole("option", { name: "30 minutes" }));
    fireEvent.click(screen.getByRole("button", { name: "Save update policy" }));
    fireEvent.click(screen.getByRole("button", { name: "Authorize save the update policy" }));

    await waitFor(() => expect(updates.updateUpdatePolicy).toHaveBeenCalledWith(expect.objectContaining({
      automaticUpdates: true,
      idleMinutes: 30,
    })));
  });

  it("allows a preflight transaction to request cancellation", async () => {
    vi.mocked(updates.getUpdateStatus).mockResolvedValue({
      ...baseStatus,
      phase: "preflight",
      updateAvailable: false,
      updateInProgress: true,
      message: "Preparing candidate",
    });
    vi.mocked(updates.cancelPendingUpdate).mockResolvedValue({
      ...baseStatus,
      phase: "preflight",
      updateAvailable: false,
      updateInProgress: true,
      message: "Cancellation requested",
    });

    render(<UpdatesPanel />);
    await screen.findByText("Preparing candidate");
    fireEvent.click(screen.getByRole("button", { name: "Cancel update" }));
    fireEvent.click(screen.getByRole("button", { name: "Authorize cancel the pending update" }));

    await waitFor(() => expect(updates.cancelPendingUpdate).toHaveBeenCalledTimes(1));
    expect(await screen.findByText(/Update cancellation requested/)).toBeTruthy();
  });
  it("recovers an initially expired authorization through a status-only confirmation", async () => {
    vi.mocked(updates.getUpdateStatus).mockRejectedValueOnce(new ApiError("Confirm your identity", 403, "reauthentication_required"));
    render(<UpdatesPanel />);
    fireEvent.click(await screen.findByRole("button", { name: "Confirm your identity" }));
    expect(screen.queryByRole("button", { name: "Try again" })).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "Authorize view update controls" }));
    expect(await screen.findByText("A newer StreamHome commit is available.")).toBeTruthy();
    expect(updates.installUpdate).not.toHaveBeenCalled();
    expect(updates.updateUpdatePolicy).not.toHaveBeenCalled();
  });

  it("preserves the policy draft through polling, expiration, reauthorization and Check now", async () => {
    const interval = vi.spyOn(window, "setInterval");
    const poll = () => { const callback = interval.mock.calls.find(([, delay]) => delay === 30000)?.[0]; if (typeof callback !== "function") throw new Error("Missing status poll"); callback(); };
    try {
      render(<UpdatesPanel />);
      await screen.findByText("A newer StreamHome commit is available.");
      fireEvent.click(screen.getByRole("combobox", { name: "Idle grace period" }));
      fireEvent.click(screen.getByRole("option", { name: "30 minutes" }));
      await act(async () => { poll(); });
      expect(screen.getByRole("combobox", { name: "Idle grace period" }).textContent).toContain("30 minutes");
      vi.mocked(updates.getUpdateStatus).mockRejectedValueOnce(new ApiError("Confirmation expired", 403, "reauthentication_required"));
      fireEvent.click(screen.getByRole("combobox", { name: "Idle grace period" }));
      expect(screen.getByRole("listbox", { name: "Idle grace period" })).toBeTruthy();
      await act(async () => { poll(); });
      expect(screen.queryByRole("listbox", { name: "Idle grace period" })).toBeNull();
      fireEvent.click(screen.getByRole("button", { name: "Confirm your identity" }));
      fireEvent.click(screen.getByRole("button", { name: "Authorize view update controls" }));
      await waitFor(() => expect(screen.queryByRole("button", { name: "Confirm your identity" })).toBeNull());
      expect(screen.getByRole("combobox", { name: "Idle grace period" }).textContent).toContain("30 minutes");
      expect(updates.updateUpdatePolicy).not.toHaveBeenCalled();
      fireEvent.click(screen.getByRole("button", { name: "Check now" }));
      await screen.findByText("Update check completed.");
      expect(screen.getByRole("combobox", { name: "Idle grace period" }).textContent).toContain("30 minutes");
    } finally { interval.mockRestore(); }
  });

  it("keeps connectivity failures retryable without pretending they require credentials", async () => {
    vi.mocked(updates.getUpdateStatus).mockRejectedValueOnce(new ApiError("Server unreachable", 0, "server_unreachable"));
    render(<UpdatesPanel />);
    fireEvent.click(await screen.findByRole("button", { name: "Try again" }));
    expect(await screen.findByText("A newer StreamHome commit is available.")).toBeTruthy();
    expect(screen.queryByRole("button", { name: "Confirm your identity" })).toBeNull();
  });

  it("opens complete release diagnostics separately without triggering an update", async () => {
    render(<UpdatesPanel />);
    const trigger = await screen.findByRole("button", { name: "Release and recovery details" });
    expect(screen.queryByText(baseStatus.transactionId!)).toBeNull();
    trigger.focus();
    fireEvent.click(trigger);
    expect(screen.getByRole("dialog", { name: "Release and recovery details" })).toBeTruthy();
    expect(screen.getByText(baseStatus.transactionId!)).toBeTruthy();
    fireEvent.keyDown(document.activeElement!, { key: "Escape" });
    await waitFor(() => expect(document.activeElement).toBe(trigger));
    expect(updates.checkForUpdates).not.toHaveBeenCalled();
    expect(updates.installUpdate).not.toHaveBeenCalled();
  });

});
