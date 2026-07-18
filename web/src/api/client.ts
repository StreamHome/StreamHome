export class ApiError extends Error {
  constructor(message: string, public status = 0, public code = "request_failed", public retryAfterSeconds?: number) {
    super(message);
    this.name = "ApiError";
  }
}

export async function apiFetch<T>(path: string, options: RequestInit = {}): Promise<T> {
  const token = localStorage.getItem("streamhome_token");
  const headers = new Headers(options.headers || {});
  
  if (token && !headers.has("Authorization")) {
    headers.set("Authorization", `Bearer ${token}`);
  }
  
  if (!headers.has("Content-Type") && options.body && typeof options.body === "string") {
    headers.set("Content-Type", "application/json");
  }

  let response: Response;
  try {
    response = await fetch(path, { ...options, headers });
  } catch (error) {
    if (error instanceof DOMException && error.name === "AbortError") throw new ApiError("The server took too long to respond.", 0, "request_timeout");
    throw new ApiError(navigator.onLine ? "StreamHome could not reach the server." : "This device is offline.", 0, navigator.onLine ? "server_unreachable" : "offline");
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
    
    if (response.status === 401 && token && !["invalid_credentials", "invalid_factor", "challenge_expired"].includes(errorCode)) {
      localStorage.removeItem("streamhome_token");
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

export function apiGet<T>(path: string, options?: RequestInit): Promise<T> {
  return apiFetch<T>(path, { ...options, method: "GET" });
}

export function apiPost<T>(path: string, body?: unknown, options?: RequestInit): Promise<T> {
  return apiFetch<T>(path, {
    ...options,
    method: "POST",
    body: body !== undefined ? JSON.stringify(body) : undefined,
  });
}

export function apiDelete<T>(path: string, options?: RequestInit): Promise<T> {
  return apiFetch<T>(path, { ...options, method: "DELETE" });
}
