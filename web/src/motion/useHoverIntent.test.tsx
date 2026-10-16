import { act, renderHook } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { useHoverIntent } from "./useHoverIntent";

describe("profile theme hover intent", () => {
  beforeEach(() => vi.useFakeTimers());
  afterEach(() => vi.useRealTimers());

  it("waits for intent, cancels pointer passes, and supports immediate selection", () => {
    const { result } = renderHook(() => useHoverIntent("ember", 180));
    act(() => result.current.schedule("aurora"));
    act(() => vi.advanceTimersByTime(179));
    expect(result.current.value).toBe("ember");
    act(() => result.current.cancel("ember"));
    act(() => vi.advanceTimersByTime(1));
    expect(result.current.value).toBe("ember");
    act(() => result.current.schedule("cinema"));
    act(() => vi.advanceTimersByTime(180));
    expect(result.current.value).toBe("cinema");
    act(() => result.current.setImmediate("cinema"));
    expect(result.current.value).toBe("cinema");
  });
});
