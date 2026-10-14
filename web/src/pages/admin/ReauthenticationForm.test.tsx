import React from "react";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import * as auth from "../../api/auth";
import { ApiError } from "../../api/client";
import { ReauthenticationForm } from "./ReauthenticationForm";
import { SudoModal } from "./SudoModal";
import { Input } from "../../components/ui/Input";

vi.mock("../../api/auth", () => ({ beginReauthentication: vi.fn(), verifyReauthentication: vi.fn() }));
const challenge = { requires2fa: true as const, challengeToken: "fixture-challenge", expiresInSeconds: 300, email: "fixture@example.test", message: "Factor required" };
const confirmed = { reauthenticated: true as const, validForSeconds: 600 };
const enterPassword = () => { fireEvent.change(screen.getByLabelText("Password"), { target: { value: "fixture-password" } }); fireEvent.click(screen.getByRole("button", { name: "Continue" })); };

afterEach(cleanup);
beforeEach(() => { vi.resetAllMocks(); vi.mocked(auth.beginReauthentication).mockResolvedValue(confirmed); vi.mocked(auth.verifyReauthentication).mockResolvedValue(confirmed); });

describe("shared identity confirmation", () => {
  it("preserves an explicit input ID, help description and named error", () => {
    render(<><p id="password-help">Account password</p><Input id="password" label="Password" aria-describedby="password-help" error="Invalid password" /></>);
    const input = screen.getByLabelText("Password");
    expect(input.id).toBe("password");
    expect(input.getAttribute("aria-describedby")).toBe("password-help password-error");
    expect(input.getAttribute("aria-invalid")).toBe("true");
    expect(document.getElementById("password-error")?.textContent).toBe("Invalid password");
  });

  it("assigns unique generated IDs to separate labeled inputs", () => {
    render(<><Input label="First" /><Input label="Second" /></>);
    expect(screen.getByLabelText("First").id).not.toBe(screen.getByLabelText("Second").id);
  });

  it("keeps password-only confirmation single-step", async () => {
    const done = vi.fn();
    render(<ReauthenticationForm onSuccess={done} />);
    enterPassword();
    await waitFor(() => expect(done).toHaveBeenCalledTimes(1));
    expect(auth.verifyReauthentication).not.toHaveBeenCalled();
    expect((screen.getByLabelText("Password") as HTMLInputElement).value).toBe("");
  });

  it("does not enter the factor step when the password is wrong", async () => {
    vi.mocked(auth.beginReauthentication).mockRejectedValue(new ApiError("Incorrect password", 401, "invalid_credentials"));
    const done = vi.fn(); render(<ReauthenticationForm onSuccess={done} />); enterPassword();
    expect(await screen.findByRole("alert")).toHaveProperty("textContent", "Incorrect password");
    expect(screen.queryByLabelText("Authenticator code")).toBeNull();
    expect(auth.verifyReauthentication).not.toHaveBeenCalled(); expect(done).not.toHaveBeenCalled();
  });

  it("replaces completed password controls and accepts full-code paste/autofill and deletion", async () => {
    vi.mocked(auth.beginReauthentication).mockResolvedValue(challenge);
    const done = vi.fn(); render(<ReauthenticationForm onSuccess={done} />); enterPassword();
    const code = await screen.findByLabelText("Authenticator code");
    expect(screen.queryByLabelText("Password")).toBeNull();
    expect(screen.queryByRole("button", { name: "Show password" })).toBeNull();
    expect(code.getAttribute("autocomplete")).toBe("one-time-code");
    fireEvent.change(code, { target: { value: "123456" } });
    fireEvent.change(code, { target: { value: "12345" } });
    expect((code as HTMLInputElement).value).toBe("12345");
    fireEvent.change(code, { target: { value: "654321" } });
    fireEvent.click(screen.getByRole("button", { name: "Verify authenticator" }));
    await waitFor(() => expect(done).toHaveBeenCalledTimes(1));
    expect(auth.verifyReauthentication).toHaveBeenCalledWith({ challengeToken: "fixture-challenge", method: "totp", code: "654321" });
  });

  it("switches to the existing recovery method without reusing the authenticator input", async () => {
    vi.mocked(auth.beginReauthentication).mockResolvedValue(challenge);
    const done = vi.fn(); render(<ReauthenticationForm onSuccess={done} />); enterPassword();
    fireEvent.change(await screen.findByLabelText("Authenticator code"), { target: { value: "123456" } });
    fireEvent.click(screen.getByRole("button", { name: "Use recovery code" }));
    const recovery = screen.getByLabelText("Recovery code"); expect((recovery as HTMLInputElement).value).toBe("");
    fireEvent.change(recovery, { target: { value: "AAAA-BBBB-CCCC-DDDD" } });
    fireEvent.click(screen.getByRole("button", { name: "Verify recovery code" }));
    await waitFor(() => expect(done).toHaveBeenCalledTimes(1));
    expect(auth.verifyReauthentication).toHaveBeenCalledWith({ challengeToken: "fixture-challenge", method: "recovery", code: "AAAA-BBBB-CCCC-DDDD" });
  });

  it("restarts at the password when the challenge expires", async () => {
    vi.mocked(auth.beginReauthentication).mockResolvedValue(challenge);
    vi.mocked(auth.verifyReauthentication).mockRejectedValue(new ApiError("Verification expired", 401, "challenge_expired"));
    render(<ReauthenticationForm onSuccess={vi.fn()} />); enterPassword();
    fireEvent.change(await screen.findByLabelText("Authenticator code"), { target: { value: "123456" } });
    fireEvent.click(screen.getByRole("button", { name: "Verify authenticator" }));
    await screen.findByText("Verification expired");
    await waitFor(() => expect(screen.queryByLabelText("Authenticator code")).toBeNull());
    expect((screen.getByLabelText("Password") as HTMLInputElement).value).toBe("");
  });

  it("does not execute the pending action after cancellation during verification", async () => {
    let complete!: (value: typeof confirmed) => void;
    vi.mocked(auth.beginReauthentication).mockImplementation(() => new Promise((resolve) => { complete = resolve; }));
    const done = vi.fn(); const view = render(<SudoModal isOpen onCancel={vi.fn()} onSuccess={done} actionLabel="View status" />);
    fireEvent.change(screen.getByLabelText("Password"), { target: { value: "fixture-password" } });
    fireEvent.click(screen.getByRole("button", { name: "Authorize" }));
    view.rerender(<SudoModal isOpen={false} onCancel={vi.fn()} onSuccess={done} actionLabel="View status" />);
    complete(confirmed);
    await waitFor(() => expect(auth.beginReauthentication).toHaveBeenCalledTimes(1));
    expect(done).not.toHaveBeenCalled();
  });

  it("clears factor state on close/reopen and contains/restores dialog focus", async () => {
    vi.mocked(auth.beginReauthentication).mockResolvedValue(challenge);
    const trigger = document.createElement("button"); document.body.append(trigger); trigger.focus();
    const cancel = vi.fn(); const done = vi.fn();
    const view = render(<SudoModal isOpen onCancel={cancel} onSuccess={done} actionLabel="View status" />);
    const password = screen.getByLabelText("Password"); expect(document.activeElement).toBe(password);
    expect(screen.getByRole("dialog", { name: "Confirm your identity" })).toBeTruthy();
    const cancelButton = screen.getByRole("button", { name: "Cancel" }); cancelButton.focus();
    fireEvent.keyDown(window, { key: "Tab" }); expect(document.activeElement).toBe(password);
    password.focus(); fireEvent.keyDown(window, { key: "Tab", shiftKey: true }); expect(document.activeElement).toBe(cancelButton);
    fireEvent.change(password, { target: { value: "fixture-password" } }); fireEvent.click(screen.getByRole("button", { name: "Authorize" }));
    await screen.findByLabelText("Authenticator code");
    fireEvent.keyDown(window, { key: "Escape" }); expect(cancel).toHaveBeenCalledTimes(1);
    view.rerender(<SudoModal isOpen={false} onCancel={cancel} onSuccess={done} actionLabel="View status" />);
    expect(document.activeElement).toBe(trigger);
    view.rerender(<SudoModal isOpen onCancel={cancel} onSuccess={done} actionLabel="View status" />);
    expect((screen.getByLabelText("Password") as HTMLInputElement).value).toBe("");
    expect(screen.queryByLabelText("Authenticator code")).toBeNull();
    trigger.remove();
  });
});
