import React from "react";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import * as auth from "../../../api/auth";
import { ApiError } from "../../../api/client";
import type { IntegrationCredentialInfo } from "../../../types/api";
import { ApiKeysPanel } from "./ApiKeysPanel";

vi.mock("../../../api/auth", () => ({
  getIntegrationCredentials: vi.fn(),
  getIntegrationScopes: vi.fn(),
  createIntegrationCredential: vi.fn(),
  updateIntegrationCredential: vi.fn(),
  revokeIntegrationCredential: vi.fn(),
}));

vi.mock("../SudoModal", () => ({
  SudoModal: ({ isOpen, onSuccess }: { isOpen: boolean; onSuccess: () => void | Promise<void> }) => isOpen
    ? <button type="button" onClick={() => void onSuccess()}>Complete reauthorization</button>
    : null,
}));

const scopes = [
  { id: "ingest" as const, label: "Manage media", description: "Submit movies and episodes and manage supported media sidecars." },
  { id: "downloads:read" as const, label: "View download queue", description: "Read current and recent ingestion task status." },
  { id: "downloads:cancel" as const, label: "Cancel downloads", description: "Cancel ingestion workers and remove download tasks." },
];

const sender: IntegrationCredentialInfo = {
  id: "credential-1",
  name: "Bedroom sender",
  tokenHint: "shk_abcd…123456",
  scopes: ["ingest"],
  createdAt: 1_900_000_000,
  expiresAt: null,
  revokedAt: null,
  lastUsedAt: 1_900_000_100,
};

const monitor: IntegrationCredentialInfo = {
  id: "credential-2",
  name: "Queue monitor",
  tokenHint: "shk_efgh…654321",
  scopes: ["downloads:read"],
  createdAt: 1_900_000_000,
  expiresAt: null,
  revokedAt: null,
  lastUsedAt: null,
};

describe("ApiKeysPanel", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.mocked(auth.getIntegrationCredentials).mockResolvedValue([sender, monitor]);
    vi.mocked(auth.getIntegrationScopes).mockResolvedValue(scopes);
  });

  it("lists server-wide keys and makes Manage media readiness explicit", async () => {
    render(<ApiKeysPanel />);
    expect(await screen.findByRole("heading", { name: "Existing API keys" })).toBeTruthy();
    expect(screen.getByText("Bedroom sender")).toBeTruthy();
    expect(screen.getByText("Queue monitor")).toBeTruthy();
    expect(screen.getByText("Manage media enabled")).toBeTruthy();
    expect(screen.getByText("Manage media disabled")).toBeTruthy();
    expect(screen.getByRole("button", { name: "Edit permissions" })).toBeTruthy();
  });

  it("keeps the selected permission badge in sync without changing the server scope id", async () => {
    render(<ApiKeysPanel />);
    fireEvent.click(await screen.findByLabelText("Manage media"));
    expect(screen.getByText("Manage media not selected")).toBeTruthy();
    expect(auth.createIntegrationCredential).not.toHaveBeenCalled();
    fireEvent.click(screen.getByLabelText("Manage media"));
    expect(screen.getByText("Manage media selected")).toBeTruthy();
  });

  it("creates a named API key with Manage media selected by default and shows its secret once", async () => {
    vi.mocked(auth.createIntegrationCredential).mockResolvedValue({
      credential: { ...sender, id: "credential-3", name: "Living room sender" },
      token: "shk_complete_show_once_secret",
    });
    render(<ApiKeysPanel />);
    fireEvent.change(await screen.findByLabelText("API key name"), { target: { value: "Living room sender" } });
    expect((screen.getByLabelText("Manage media") as HTMLInputElement).checked).toBe(true);
    fireEvent.click(screen.getByLabelText("View download queue"));
    fireEvent.click(screen.getByRole("combobox", { name: "API key expiration" }));
    fireEvent.click(screen.getByRole("option", { name: "90 days" }));
    fireEvent.click(screen.getByRole("button", { name: "Create API key" }));
    await waitFor(() => expect(auth.createIntegrationCredential).toHaveBeenCalledWith(
      "Living room sender",
      ["ingest", "downloads:read"],
      90,
    ));
    expect(await screen.findByText("shk_complete_show_once_secret")).toBeTruthy();
    expect(screen.getByRole("button", { name: "I saved the key" })).toBeTruthy();
  });

  it("edits permissions and revokes one key without rotating the others", async () => {
    vi.mocked(auth.updateIntegrationCredential).mockResolvedValue({ ...sender, name: "Bedroom automation", scopes: ["ingest", "downloads:cancel"] });
    vi.mocked(auth.revokeIntegrationCredential).mockResolvedValue({ revoked: true });
    render(<ApiKeysPanel />);
    fireEvent.click(await screen.findByRole("button", { name: "Edit" }));
    fireEvent.change(screen.getByLabelText("Edit name for Bedroom sender"), { target: { value: "Bedroom automation" } });
    fireEvent.click(screen.getByLabelText("Cancel downloads for Bedroom sender"));
    fireEvent.click(screen.getByRole("button", { name: "Save changes" }));
    await waitFor(() => expect(auth.updateIntegrationCredential).toHaveBeenCalledWith(
      "credential-1",
      "Bedroom automation",
      ["ingest", "downloads:cancel"],
    ));
    fireEvent.click((await screen.findAllByRole("button", { name: "Revoke" }))[0]);
    fireEvent.click(screen.getByRole("button", { name: "Confirm revoke" }));
    await waitFor(() => expect(auth.revokeIntegrationCredential).toHaveBeenCalledWith("credential-1"));
    expect(screen.getByText("Queue monitor")).toBeTruthy();
  });

  it("opens reauthorization when the recent administrator confirmation expires", async () => {
    vi.mocked(auth.getIntegrationCredentials)
      .mockRejectedValueOnce(new ApiError("Confirm your password and authenticator code to continue.", 403, "reauthentication_required"))
      .mockResolvedValueOnce([sender]);
    render(<ApiKeysPanel />);
    expect(await screen.findByText(/recent administrator authorization expired/i)).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "Complete reauthorization" }));
    await waitFor(() => expect(auth.getIntegrationCredentials).toHaveBeenCalledTimes(2));
    expect(await screen.findByText("Bedroom sender")).toBeTruthy();
  });

  it("opens permission guidance without changing a key or the selected create permissions", async () => {
    render(<ApiKeysPanel />);
    await screen.findByText("Bedroom sender");
    expect(screen.queryByText(/inspect its error code/)).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "MediaSender permissions and troubleshooting" }));
    expect(screen.getByRole("dialog", { name: "MediaSender permissions and troubleshooting" })).toBeTruthy();
    expect(screen.getByText(/inspect its error code/)).toBeTruthy();
    expect((screen.getByLabelText("Manage media") as HTMLInputElement).checked).toBe(true);
    expect(auth.createIntegrationCredential).not.toHaveBeenCalled();
    expect(auth.updateIntegrationCredential).not.toHaveBeenCalled();
    expect(auth.revokeIntegrationCredential).not.toHaveBeenCalled();
  });
});
