import React from "react";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
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

  it("confirms and authorizes an immediate preflight separately from idle installation", async () => {
    render(<UpdatesPanel />);
    await screen.findByText("A newer StreamHome commit is available.");

    fireEvent.click(screen.getByRole("button", { name: "Update now" }));
    expect(await screen.findByRole("heading", { name: "Update StreamHome now?" })).toBeTruthy();
    expect(screen.getByText(/Browser presence, playback/)).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "Continue to authorization" }));
    fireEvent.click(screen.getByRole("button", { name: "Authorize install the update now" }));

    await waitFor(() => expect(updates.installUpdate).toHaveBeenCalledWith("now"));
    expect(await screen.findByText(/Immediate preflight started/)).toBeTruthy();
    expect(screen.getByText("Immediate mode ignores the window")).toBeTruthy();
  });

  it("saves administrator-selected idle and automatic-update policy", async () => {
    render(<UpdatesPanel />);
    await screen.findByText("A newer StreamHome commit is available.");

    fireEvent.click(screen.getByRole("checkbox", { name: /Automatic updates/ }));
    fireEvent.change(screen.getAllByRole("combobox")[0], { target: { value: "30" } });
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
});
