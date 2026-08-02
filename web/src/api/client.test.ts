import { afterEach, describe, expect, it, vi } from "vitest";

import { apiGet } from "./client";


afterEach(() => {
  vi.useRealTimers();
  vi.unstubAllGlobals();
});

describe("API request lifecycle", () => {
  it("aborts a request that exceeds its deadline with a typed timeout", async () => {
    vi.useFakeTimers();
    vi.stubGlobal("fetch", vi.fn((_path: string, options?: RequestInit) => new Promise<Response>((_resolve, reject) => {
      options?.signal?.addEventListener("abort", () => reject(new DOMException("Aborted", "AbortError")), { once: true });
    })));

    const request = apiGet("/api/slow", { timeoutMs: 25 });
    const rejection = expect(request).rejects.toMatchObject({ code: "request_timeout" });
    await vi.advanceTimersByTimeAsync(25);

    await rejection;
  });

  it("distinguishes caller cancellation from a server timeout", async () => {
    const controller = new AbortController();
    vi.stubGlobal("fetch", vi.fn((_path: string, options?: RequestInit) => new Promise<Response>((_resolve, reject) => {
      options?.signal?.addEventListener("abort", () => reject(new DOMException("Aborted", "AbortError")), { once: true });
    })));

    const request = apiGet("/api/cancelled", { signal: controller.signal });
    controller.abort();

    await expect(request).rejects.toMatchObject({ code: "request_cancelled" });
  });
});
