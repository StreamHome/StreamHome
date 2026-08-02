export class ApiError extends Error {
  constructor(message: string, public status = 0, public code = "request_failed", public retryAfterSeconds?: number) {
    super(message);
    this.name = "ApiError";
  }
}

export interface ApiRequestInit extends RequestInit {
  timeoutMs?: number;
}

const DEFAULT_API_TIMEOUT_MS = 45_000;

export async function apiFetch<T>(path: string, options: ApiRequestInit = {}): Promise<T> {
  const { timeoutMs = DEFAULT_API_TIMEOUT_MS, ...requestOptions } = options;
  const headers = new Headers(requestOptions.headers || {});
  
  if (!headers.has("Content-Type") && requestOptions.body && typeof requestOptions.body === "string") {
    headers.set("Content-Type", "application/json");
  }

  const requestController = new AbortController();
  let timedOut = false;
  const abortFromCaller = () => requestController.abort(requestOptions.signal?.reason);
  if (requestOptions.signal?.aborted) abortFromCaller();
  else requestOptions.signal?.addEventListener("abort", abortFromCaller, { once: true });
  const timeout = window.setTimeout(() => {
    timedOut = true;
    requestController.abort();
  }, timeoutMs);

  let response: Response;
  try {
    response = await fetch(path, {
      ...requestOptions,
      headers,
      signal: requestController.signal,
      credentials: requestOptions.credentials ?? "same-origin",
    });
  } catch (error) {
    if (error instanceof DOMException && error.name === "AbortError") {
      if (timedOut) throw new ApiError("The server took too long to respond.", 0, "request_timeout");
      throw new ApiError("The request was cancelled.", 0, "request_cancelled");
    }
    throw new ApiError(navigator.onLine ? "StreamHome could not reach the server." : "This device is offline.", 0, navigator.onLine ? "server_unreachable" : "offline");
  } finally {
    window.clearTimeout(timeout);
    requestOptions.signal?.removeEventListener("abort", abortFromCaller);
  }
  
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
    } catch {
      // Ignore if not JSON
    }
    
    const authenticationFailure = ["request_failed", "not_authenticated", "session_expired", "invalid_session"].includes(errorCode);
    if (response.status === 401 && authenticationFailure) {
      if (window.location.pathname !== "/login") {
        window.location.href = "/login";
      }
    }
    
    throw new ApiError(errorMessage, response.status, errorCode, retryAfterSeconds);
  }
  
  // Some endpoints might return empty body on 204 or DELETE
  const text = await response.text();
  if (!text) {
    return {} as T;
  }
  
  return JSON.parse(text) as T;
}

export function apiGet<T>(path: string, options?: ApiRequestInit): Promise<T> {
  return apiFetch<T>(path, { ...options, method: "GET" });
}

export function apiPost<T>(path: string, body?: unknown, options?: ApiRequestInit): Promise<T> {
  return apiFetch<T>(path, {
    ...options,
    method: "POST",
    body: body !== undefined ? JSON.stringify(body) : undefined,
  });
}

export function apiPut<T>(path: string, body?: unknown, options?: ApiRequestInit): Promise<T> {
  return apiFetch<T>(path, {
    ...options,
    method: "PUT",
    body: body !== undefined ? JSON.stringify(body) : undefined,
  });
}

export function apiDelete<T>(path: string, options?: ApiRequestInit): Promise<T> {
  return apiFetch<T>(path, { ...options, method: "DELETE" });
}
