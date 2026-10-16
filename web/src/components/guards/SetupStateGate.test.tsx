import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { getSetupStatus } from "../../api/setup";
import { SetupStateGate } from "./SetupStateGate";

vi.mock("../../api/setup", () => ({ getSetupStatus: vi.fn() }));

const configured = { required: false, unlocked: false, webPort: 3000, serverVersion: "1.0.0", mediaPath: "server/media", databasePath: "server/database.db" };

function renderGate(path: string) {
  return render(<MemoryRouter initialEntries={[path]}><SetupStateGate><Routes>
    <Route path="/setup" element={<p>Setup wizard</p>} />
    <Route path="/login" element={<p>Login page</p>} />
    <Route path="/profiles" element={<p>Profiles</p>} />
  </Routes></SetupStateGate></MemoryRouter>);
}

afterEach(cleanup);

describe("SetupStateGate", () => {
  beforeEach(() => vi.resetAllMocks());

  it("redirects normal routes to setup while the server is unconfigured", async () => {
    vi.mocked(getSetupStatus).mockResolvedValue({ ...configured, required: true });
    renderGate("/profiles");
    await waitFor(() => expect(screen.getByText("Setup wizard")).toBeTruthy());
  });

  it("prevents reopening setup after installation", async () => {
    vi.mocked(getSetupStatus).mockResolvedValue(configured);
    renderGate("/setup");
    await waitFor(() => expect(screen.getByText("Login page")).toBeTruthy());
  });
  it("distinguishes failed status from a locked server and retries without reloading", async () => {
    vi.mocked(getSetupStatus).mockRejectedValueOnce(new Error("Unavailable"));
    renderGate("/profiles");
    await screen.findByRole("heading", { name: "StreamHome is unavailable" });
    expect(screen.queryByText("Setup wizard")).toBeNull();
    vi.mocked(getSetupStatus).mockResolvedValue({ ...configured, required: true });
    fireEvent.click(screen.getByRole("button", { name: "Retry" }));
    await screen.findByText("Setup wizard");
  });

  it("aborts and ignores status completion after unmount", async () => {
    let resolve!: (value: typeof configured) => void;
    vi.mocked(getSetupStatus).mockReturnValueOnce(new Promise((yes) => { resolve = yes; }));
    const { unmount } = renderGate("/profiles");
    const signal = vi.mocked(getSetupStatus).mock.calls[0][0]?.signal;
    unmount(); expect(signal?.aborted).toBe(true);
    await act(async () => resolve(configured));
    expect(screen.queryByText("Profiles")).toBeNull();
  });

});
