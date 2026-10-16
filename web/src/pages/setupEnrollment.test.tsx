import { act, cleanup, renderHook, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { ApiError } from "../api/client";
import { beginSetupTOTP, cancelSetupTOTP, verifySetupTOTP } from "../api/setup";
import { useSetupEnrollment } from "./setupEnrollment";
vi.mock("../api/setup", () => ({ beginSetupTOTP: vi.fn(), cancelSetupTOTP: vi.fn(), verifySetupTOTP: vi.fn() }));
const deferred = <T,>() => { let resolve!: (value: T) => void; let reject!: (reason: unknown) => void; const promise = new Promise<T>((yes, no) => { resolve = yes; reject = no; }); return { promise, resolve, reject }; };
const value = (id = "synthetic-enrollment") => ({ enrollmentId: id, manualKey: "SYNTHETICKEY", qrImageUrl: "/synthetic-qr", expiresAt: Date.now() / 1000 + 600 });
afterEach(() => { cleanup(); vi.useRealTimers(); vi.restoreAllMocks(); });
beforeEach(() => { vi.resetAllMocks(); vi.mocked(beginSetupTOTP).mockImplementation(async () => value()); vi.mocked(cancelSetupTOTP).mockResolvedValue(undefined); vi.mocked(verifySetupTOTP).mockResolvedValue({ valid: true, enrollmentId: "synthetic-enrollment" }); });

async function enable(result: ReturnType<typeof renderHook<ReturnType<typeof useSetupEnrollment>, { email: string; frozen: boolean }>>["result"]) { act(() => result.current.choose(true)); await waitFor(() => expect(result.current.state.phase).toBe("ready")); }

describe("setup enrollment mutation ownership", () => {
  it("shows deterministic begin failure and explicit retry without radio regeneration", async () => {
    vi.mocked(beginSetupTOTP).mockRejectedValueOnce(new ApiError("Invalid account", 422, "invalid_email"));
    const { result } = renderHook(() => useSetupEnrollment("admin@example.test", false));
    act(() => result.current.choose(true)); await waitFor(() => expect(result.current.state.phase).toBe("error"));
    act(() => result.current.choose(true)); expect(beginSetupTOTP).toHaveBeenCalledTimes(1);
    act(() => { result.current.generate(); result.current.generate(); }); await waitFor(() => expect(result.current.state.phase).toBe("ready")); expect(beginSetupTOTP).toHaveBeenCalledTimes(2);
  });
  it("keeps invalid-code enrollment but retires structured missing enrollment", async () => {
    const { result } = renderHook(() => useSetupEnrollment("admin@example.test", false)); await enable(result);
    vi.mocked(verifySetupTOTP).mockRejectedValueOnce(new ApiError("Invalid code", 422, "invalid_totp"));
    act(() => result.current.verify("123456")); await waitFor(() => expect(result.current.state.message).toBe("Invalid code")); expect(result.current.state.enrollment).toBeTruthy();
    vi.mocked(verifySetupTOTP).mockRejectedValueOnce(new ApiError("Missing", 404, "totp_enrollment_not_found"));
    act(() => result.current.verify("123456")); await waitFor(() => expect(result.current.state.phase).toBe("expired")); expect(result.current.state.enrollment).toBeUndefined();
  });
  it("unknown no-ID begin requires explicit replacement and never cancels an invented ID", async () => {
    vi.mocked(beginSetupTOTP).mockRejectedValueOnce(new ApiError("Lost", 0, "request_timeout"));
    const { result, rerender } = renderHook(({ email }) => useSetupEnrollment(email, false), { initialProps: { email: "admin@example.test" } });
    act(() => result.current.choose(true)); await waitFor(() => expect(result.current.state.phase).toBe("unknown"));
    rerender({ email: "changed@example.test" }); act(() => { result.current.choose(false); result.current.choose(true); });
    expect(beginSetupTOTP).toHaveBeenCalledTimes(1); expect(cancelSetupTOTP).not.toHaveBeenCalled();
    act(() => result.current.generate()); await waitFor(() => expect(result.current.state.phase).toBe("ready"));
    vi.mocked(verifySetupTOTP).mockRejectedValueOnce(new ApiError("Late old work invalidated this ID", 410, "totp_enrollment_expired"));
    act(() => result.current.verify("123456")); await waitFor(() => expect(result.current.state.phase).toBe("expired")); expect(result.current.state.enrollment).toBeUndefined();
  });
  it("retires stale begin after email change and drains its acknowledged ID before replacement", async () => {
    const pending = deferred<ReturnType<typeof value>>(); vi.mocked(beginSetupTOTP).mockReturnValueOnce(pending.promise);
    const { result, rerender } = renderHook(({ email }) => useSetupEnrollment(email, false), { initialProps: { email: "admin@example.test" } });
    act(() => result.current.choose(true)); await waitFor(() => expect(beginSetupTOTP).toHaveBeenCalledTimes(1));
    rerender({ email: "new@example.test" });
    await act(async () => pending.resolve(value("stale-known")));
    await waitFor(() => expect(cancelSetupTOTP).toHaveBeenCalledWith("stale-known")); expect(result.current.state.enrollment).toBeUndefined();
    act(() => result.current.generate()); await waitFor(() => expect(result.current.state.phase).toBe("ready")); expect(beginSetupTOTP).toHaveBeenLastCalledWith("new@example.test");
  });
  it("retires verification on email change and does not accept its late success", async () => {
    const pending = deferred<{ valid: true; enrollmentId: string }>();
    const { result, rerender } = renderHook(({ email }) => useSetupEnrollment(email, false), { initialProps: { email: "admin@example.test" } }); await enable(result);
    vi.mocked(verifySetupTOTP).mockReturnValueOnce(pending.promise); act(() => result.current.verify("123456"));
    rerender({ email: "new@example.test" }); await act(async () => pending.resolve({ valid: true, enrollmentId: "synthetic-enrollment" }));
    expect(result.current.state.phase).not.toBe("verified"); await waitFor(() => expect(cancelSetupTOTP).toHaveBeenCalledTimes(1));
  });
  it("unknown cancellation blocks hidden replacement and exposes an explicit new enrollment", async () => {
    const { result } = renderHook(() => useSetupEnrollment("admin@example.test", false)); await enable(result);
    vi.mocked(cancelSetupTOTP).mockRejectedValueOnce(new ApiError("Lost cancel", 0, "request_timeout"));
    act(() => result.current.choose(false)); await waitFor(() => expect(result.current.state.phase).toBe("unknown"));
    act(() => result.current.choose(true)); expect(beginSetupTOTP).toHaveBeenCalledTimes(1);
    act(() => result.current.generate()); await waitFor(() => expect(result.current.state.phase).toBe("ready"));
  });
  it("expires verified local state automatically and removes its secret material", async () => {
    vi.useFakeTimers();
    const { result } = renderHook(() => useSetupEnrollment("admin@example.test", false));
    await act(async () => { result.current.choose(true); });
    expect(result.current.state.phase).toBe("ready");
    await act(async () => { result.current.verify("123456"); });
    expect(result.current.state.phase).toBe("verified");
    await act(async () => { vi.advanceTimersByTime(601000); });
    expect(result.current.state.phase).toBe("expired"); expect(result.current.state.enrollment).toBeUndefined();
    act(() => result.current.verify("123456")); expect(verifySetupTOTP).toHaveBeenCalledTimes(1);
  });
  it("freezes a verified enrollment across local expiry for an already submitted completion", async () => {
    const { result, rerender } = renderHook(({ frozen }) => useSetupEnrollment("admin@example.test", frozen), { initialProps: { frozen: false } }); await enable(result);
    act(() => result.current.verify("123456")); await waitFor(() => expect(result.current.state.phase).toBe("verified"));
    rerender({ frozen: true }); const previous = result.current.state;
    vi.spyOn(Date, "now").mockReturnValue(previous.enrollment!.expiresAt * 1000 + 5000);
    act(() => { result.current.generate(); result.current.choose(false); result.current.verify("123456"); });
    expect(result.current.state).toEqual(previous); expect(beginSetupTOTP).toHaveBeenCalledTimes(1); expect(cancelSetupTOTP).not.toHaveBeenCalled();
  });
  it("unmount drains a returned stale known ID without publishing secret state", async () => {
    const pending = deferred<ReturnType<typeof value>>(); vi.mocked(beginSetupTOTP).mockReturnValueOnce(pending.promise);
    const { result, unmount } = renderHook(() => useSetupEnrollment("admin@example.test", false)); act(() => result.current.choose(true));
    await waitFor(() => expect(beginSetupTOTP).toHaveBeenCalledTimes(1)); unmount(); await act(async () => pending.resolve(value("late-known")));
    await waitFor(() => expect(cancelSetupTOTP).toHaveBeenCalledWith("late-known"));
  });
  it("an enable queued before a lost cancellation is observed cannot create a hidden replacement", async () => {
    const { result } = renderHook(() => useSetupEnrollment("admin@example.test", false)); await enable(result);
    const cancel = deferred<void>(); vi.mocked(cancelSetupTOTP).mockReturnValueOnce(cancel.promise);
    act(() => { result.current.choose(false); result.current.choose(true); });
    await act(async () => cancel.reject(new ApiError("Lost cancel", 0, "request_timeout")));
    await waitFor(() => expect(result.current.state.phase).toBe("unknown")); expect(beginSetupTOTP).toHaveBeenCalledTimes(1);
  });

});
