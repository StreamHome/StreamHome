import React from "react";
import { act, renderHook } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { useAuthStore } from "../../stores/authStore";
import { parseDownloadPayload, useDownloadStream } from "./useDownloadStream";

class MockEventSource {
  static instances: MockEventSource[] = [];
  onopen: ((event: Event) => void) | null = null;
  onerror: ((event: Event) => void) | null = null;
  onmessage: ((event: MessageEvent<string>) => void) | null = null;
  close = vi.fn();

  constructor(readonly url: string, readonly options?: EventSourceInit) {
    MockEventSource.instances.push(this);
  }

  open() { this.onopen?.(new Event("open")); }
  fail() { this.onerror?.(new Event("error")); }
  message(data: string) { this.onmessage?.(new MessageEvent("message", { data })); }
}

const event = { id: "task-1", title: "Example Movie", status: "Downloading", progress: 42, speed: "8 MB/s", eta: "00:01:20" };

describe("download event stream", () => {
  beforeEach(() => {
    MockEventSource.instances = [];
    vi.stubGlobal("EventSource", MockEventSource);
    useAuthStore.setState({ token: null, isAuthenticated: true, isHydrated: true });
  });

  afterEach(() => {
    vi.unstubAllGlobals();
    useAuthStore.setState({ token: null, email: null, isAuthenticated: false, isHydrated: true });
  });

  it("normalizes array and single-event payloads without accepting invalid data", () => {
    expect(parseDownloadPayload(JSON.stringify([event]))).toEqual([event]);
    expect(parseDownloadPayload(JSON.stringify({ ...event, id: 7, progress: 140 }))).toEqual([{ ...event, id: "7", progress: 100 }]);
    expect(parseDownloadPayload("not json")).toBeNull();
    expect(parseDownloadPayload(JSON.stringify([{ title: "Missing id", progress: 10 }]))).toBeNull();
    expect(parseDownloadPayload("[]")).toEqual([]);
  });

  it("keeps the latest queue while EventSource reconnects and cleans up on unmount", () => {
    const { result, unmount } = renderHook(() => useDownloadStream());
    const source = MockEventSource.instances[0];
    expect(source.url).toBe("/api/downloads/stream");
    expect(source.options?.withCredentials).toBe(true);
    expect(result.current.connectionState).toBe("connecting");

    act(() => source.open());
    expect(result.current.connectionState).toBe("connected");
    act(() => source.message(JSON.stringify([event])));
    expect(result.current.downloads).toEqual([event]);

    act(() => source.fail());
    expect(result.current.connectionState).toBe("reconnecting");
    expect(result.current.downloads).toEqual([event]);
    act(() => source.message("malformed"));
    expect(result.current.downloads).toEqual([event]);
    act(() => source.message(JSON.stringify({ ...event, progress: 64 })));
    expect(result.current.connectionState).toBe("connected");
    expect(result.current.downloads[0].progress).toBe(64);

    act(() => useAuthStore.setState({ token: null, isAuthenticated: false }));
    expect(result.current.connectionState).toBe("disconnected");
    expect(result.current.downloads).toEqual([]);
    expect(source.close).toHaveBeenCalledOnce();
    unmount();
  });
});
