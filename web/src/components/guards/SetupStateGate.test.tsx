import { render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";
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
});
