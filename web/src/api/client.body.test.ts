import { afterEach, describe, expect, it, vi } from "vitest";
import { apiGet, apiPost } from "./client";

afterEach(() => { vi.useRealTimers(); vi.unstubAllGlobals(); });
function streamingFetch(status = 200) {
  let body: ReadableStreamDefaultController<Uint8Array>;
  let aborted = false;
  const fetchMock = vi.fn((_path: string, options?: RequestInit) => {
    const stream = new ReadableStream<Uint8Array>({ start(controller) { body = controller; } });
    options?.signal?.addEventListener("abort", () => { aborted = true; body.error(new DOMException("Aborted", "AbortError")); }, { once: true });
    return Promise.resolve(new Response(stream, { status, headers: { "Content-Type": "application/json" } }));
  });
  vi.stubGlobal("fetch", fetchMock);
  return { fetchMock, finish(value: unknown) { if (!aborted) { body.enqueue(new TextEncoder().encode(JSON.stringify(value))); body.close(); } } };
}

describe("API body lifetime", () => {
  it.each([200, 503, 401])("keeps the deadline through a stalled %s body", async (status) => {
    vi.useFakeTimers();
    const fixture = streamingFetch(status);
    const outcome = apiGet("/api/stalled-body", { timeoutMs: 25, redirectOnUnauthorized: false }).then(() => "fulfilled", (error) => error.code);
    await vi.advanceTimersByTimeAsync(25);
    fixture.finish({ detail: "late" });
    expect(await outcome).toBe("request_timeout");
    expect(vi.getTimerCount()).toBe(0);
  });
  it.each([200, 503])("forwards caller cancellation after %s headers", async (status) => {
    const controller = new AbortController();
    const fixture = streamingFetch(status);
    const outcome = apiGet("/api/body", { signal: controller.signal }).catch((error) => error.code);
    await Promise.resolve();
    await Promise.resolve();
    controller.abort("route changed");
    fixture.finish({ detail: "late" });
    expect(await outcome).toBe("request_cancelled");
  });
  it("cleans its deadline/listener after a successful body and preserves request defaults", async () => {
    vi.useFakeTimers();
    const controller = new AbortController();
    const removed = vi.spyOn(controller.signal, "removeEventListener");
    const fixture = streamingFetch();
    const request = apiPost<{ ok: boolean }>("/api/example", { value: 3 }, { signal: controller.signal, timeoutMs: 25 });
    fixture.finish({ ok: true });
    expect(await request).toEqual({ ok: true });
    expect(vi.getTimerCount()).toBe(0);
    expect(removed).toHaveBeenCalledWith("abort", expect.any(Function));
    const options = fixture.fetchMock.mock.calls[0][1]!;
    expect(options.credentials).toBe("same-origin");
    expect(new Headers(options.headers).get("Content-Type")).toBe("application/json");
    controller.abort();
  });
  it("retains structured HTTP errors and retry metadata after a complete body", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => new Response(JSON.stringify({ detail: { message: "Wait", code: "rate_limited", retryAfterSeconds: 12 } }), { status: 429 })));
    await expect(apiGet("/api/rate")).rejects.toMatchObject({ status: 429, code: "rate_limited", retryAfterSeconds: 12, message: "Wait" });
  });
  it("retains HTTP status for malformed error bodies but rejects malformed success JSON", async () => {
    const mock = vi.fn().mockResolvedValueOnce(new Response("not JSON", { status: 503 })).mockResolvedValueOnce(new Response("not JSON"));
    vi.stubGlobal("fetch", mock);
    await expect(apiGet("/api/error")).rejects.toMatchObject({ status: 503 });
    await expect(apiGet("/api/success")).rejects.toMatchObject({ status: 0, code: "invalid_response" });
  });
  it("types a broken successful body as outcome-unknown without retrying", async () => {
    const fetchMock = vi.fn(async () => ({ ok: true, text: async () => { throw new TypeError("Body stream terminated"); } }));
    vi.stubGlobal("fetch", fetchMock);
    await expect(apiPost("/api/synthetic-operation", {})).rejects.toMatchObject({ status: 0, code: "response_unreadable" });
    expect(fetchMock).toHaveBeenCalledTimes(1);
  });
  it("does not start a request that was already cancelled", async () => {
    const controller = new AbortController(); controller.abort();
    const fetchMock = vi.fn(); vi.stubGlobal("fetch", fetchMock);
    await expect(apiGet("/api/cancelled", { signal: controller.signal })).rejects.toMatchObject({ code: "request_cancelled" });
    expect(fetchMock).not.toHaveBeenCalled();
  });  it.each(["caller", "timeout"] as const)("keeps the first abort winner when %s fires first and body rejection settles later", async (first) => {
    vi.useFakeTimers(); const caller=new AbortController(); let rejectBody!: (error:unknown)=>void;
    vi.stubGlobal("fetch",vi.fn(async()=>({ok:true,text:()=>new Promise<string>((_resolve,reject)=>{rejectBody=reject;})})));
    const result=apiGet("/api/ordering",{signal:caller.signal,timeoutMs:25}).catch(error=>error.code);
    await Promise.resolve();await Promise.resolve();
    if(first==="caller") {caller.abort();vi.advanceTimersByTime(25);} else {vi.advanceTimersByTime(25);caller.abort();}
    rejectBody(new DOMException("Aborted","AbortError"));
    expect(await result).toBe(first==="caller"?"request_cancelled":"request_timeout");expect(vi.getTimerCount()).toBe(0);
  });
  it.each([true,false])("classifies failed network reads with online=%s", async (online) => {
    vi.stubGlobal("navigator",{onLine:online});vi.stubGlobal("fetch",vi.fn(async()=>{throw new TypeError("Network unavailable");}));
    await expect(apiGet("/api/network")).rejects.toMatchObject({status:0,code:online?"server_unreachable":"offline"});
  });
  it.each(["session_expired","invalid_setup_code","invalid_factor","reauthentication_required"])("preserves the redirect policy for401 code %s", async (code) => {
    const original=window;const location={pathname:"/profiles",href:""};
    vi.stubGlobal("window",{setTimeout:original.setTimeout.bind(original),clearTimeout:original.clearTimeout.bind(original),location});
    vi.stubGlobal("fetch",vi.fn(async()=>new Response(JSON.stringify({detail:{message:"Synthetic rejection",code}}),{status:401})));
    await expect(apiGet("/api/auth/example")).rejects.toMatchObject({status:401,code});
    expect(location.href).toBe(code==="session_expired"?"/login":"");
  });

});
