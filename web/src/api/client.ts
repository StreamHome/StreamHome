export class ApiError extends Error {
  constructor(message: string, public status = 0, public code = "request_failed", public retryAfterSeconds?: number) {
    super(message);
    this.name = "ApiError";
  }
}

export interface ApiRequestInit extends RequestInit {
  timeoutMs?: number;
  /** Session hydration classifies unauthorized responses without navigation. */
  redirectOnUnauthorized?: boolean;
}

const DEFAULT_API_TIMEOUT_MS = 45_000;

export async function apiFetch<T>(path: string, options: ApiRequestInit = {}): Promise<T> {
  const { timeoutMs = DEFAULT_API_TIMEOUT_MS, redirectOnUnauthorized = true, ...requestOptions } = options;
  const headers = new Headers(requestOptions.headers || {});
  if (!headers.has("Content-Type") && requestOptions.body && typeof requestOptions.body === "string") {
    headers.set("Content-Type", "application/json");
  }

  const requestController = new AbortController();
  let abortSource: "caller" | "timeout" | null = null;
  const abortFromCaller = () => {
    abortSource ??= "caller";
    requestController.abort(requestOptions.signal?.reason);
  };
  if (requestOptions.signal?.aborted) abortFromCaller();
  else requestOptions.signal?.addEventListener("abort", abortFromCaller, { once: true });
  const timeout = window.setTimeout(() => {
    abortSource ??= "timeout";
    requestController.abort();
  }, timeoutMs);
  const checkCancellation = () => {
    if (!requestController.signal.aborted) return;
    throw abortSource === "timeout"
      ? new ApiError("The server took too long to respond.", 0, "request_timeout")
      : new ApiError("The request was cancelled.", 0, "request_cancelled");
  };

  try {
    checkCancellation();
    let response: Response;
    try {
      response = await fetch(path, {
        ...requestOptions,
        headers,
        signal: requestController.signal,
        credentials: requestOptions.credentials ?? "same-origin",
      });
    } catch (error) {
      checkCancellation();
      if (error instanceof DOMException && error.name === "AbortError") throw new ApiError("The request was cancelled.", 0, "request_cancelled");
      throw new ApiError(navigator.onLine ? "StreamHome could not reach the server." : "This device is offline.", 0, navigator.onLine ? "server_unreachable" : "offline");
    }
    checkCancellation();
    if (!response.ok) {
      let errorMessage = "API request failed";
      let errorCode = "request_failed";
      let retryAfterSeconds: number | undefined;
      try {
        const errorData = await response.json();
        const detail = errorData.detail;
        if (typeof detail === "string") errorMessage = detail;
        else if (detail && typeof detail === "object") {
          errorMessage = detail.message || errorMessage;
          errorCode = detail.code || errorCode;
          retryAfterSeconds = detail.retryAfterSeconds;
        } else errorMessage = errorData.message || errorMessage;
      } catch (error) {
        // An interrupted body is a cancelled/timed-out request, not an HTTP error.
        checkCancellation();
        if (error instanceof DOMException && error.name === "AbortError") throw error;
        // A non-JSON error body retains the HTTP status and fallback message.
      }
      checkCancellation();
      const authenticationFailure = ["request_failed", "not_authenticated", "session_expired", "invalid_session"].includes(errorCode);
      if (redirectOnUnauthorized && response.status === 401 && authenticationFailure && window.location.pathname !== "/login") {
        window.location.href = "/login";
      }
      throw new ApiError(errorMessage, response.status, errorCode, retryAfterSeconds);
    }

    let text: string;
    try { text = await response.text(); }
    catch { checkCancellation(); throw new ApiError("The server response could not be read.", 0, "response_unreadable"); }
    checkCancellation();
    if (!text) return {} as T;
    try { return JSON.parse(text) as T; }
    catch { throw new ApiError("The server returned an invalid response.", 0, "invalid_response"); }
  } catch (error) {
    checkCancellation();
    throw error;
  } finally {
    // The request owns its deadline and caller listener through body consumption.
    window.clearTimeout(timeout);
    requestOptions.signal?.removeEventListener("abort", abortFromCaller);
  }
}

export function apiGet<T>(path: string, options?: ApiRequestInit): Promise<T> {
  return apiFetch<T>(path, { ...options, method: "GET" });
}
export function apiPost<T>(path: string, body?: unknown, options?: ApiRequestInit): Promise<T> {
  return apiFetch<T>(path, { ...options, method: "POST", body: body !== undefined ? JSON.stringify(body) : undefined });
}
export function apiPut<T>(path: string, body?: unknown, options?: ApiRequestInit): Promise<T> {
  return apiFetch<T>(path, { ...options, method: "PUT", body: body !== undefined ? JSON.stringify(body) : undefined });
}
export function apiDelete<T>(path: string, options?: ApiRequestInit): Promise<T> {
  return apiFetch<T>(path, { ...options, method: "DELETE" });
}
