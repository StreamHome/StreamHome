import type { SetupCompleteResponse } from "../api/setup";

export const HEVC_LABELS = { auto: "Automatic", on: "Always", off: "Never" } as const;

/** Match the server's supported origin policy while rejecting browser URL repairs. */
export function parseSetupPublicOrigin(value: string): string | null {
  const raw = value.trim();
  const match = /^(https?):\/\/([^/?#\\\s]+)(\/?)$/i.exec(raw);
  if (!match || match[2].includes("@")) return null;
  const scheme = match[1].toLowerCase();
  const authority = match[2];
  const parts = /^(\[[0-9a-fA-F:.]+\]|[^:]+)(?::([0-9]+))?$/.exec(authority);
  if (!parts || (authority.includes(":") && !authority.startsWith("[") && authority.split(":").length > 2)) return null;
  if (parts[2] !== undefined && (!/^\d+$/.test(parts[2]) || Number(parts[2]) < 1 || Number(parts[2]) > 65535)) return null;
  let url: URL;
  try { url = new URL(raw); } catch { return null; }
  const hostname = parts[1].replace(/^\[|\]$/g, "").replace(/\.$/, "").toLowerCase();
  let local = hostname === "localhost";
  if (parts[1].startsWith("[")) {
    // URL validates IPv6 syntax. HTTP supports only loopback and RFC4193, as the server does.
    const normalized = url.hostname.replace(/^\[|\]$/g, "").toLowerCase();
    local = normalized === "::1" || /^f[cd][0-9a-f]{2}:/.test(normalized);
  } else if (/^\d+(?:\.\d+){3}$/.test(hostname)) {
    const octets = hostname.split(".").map(Number);
    if (hostname.split(".").some((part) => part.length > 1 && part.startsWith("0")) || octets.some((part) => part > 255)) return null;
    local = octets[0] === 127 || octets[0] === 10 || (octets[0] === 172 && octets[1] >= 16 && octets[1] <= 31) || (octets[0] === 192 && octets[1] === 168);
  } else {
    // Numeric shorthand/hex hosts can be silently rewritten by WHATWG URL parsing.
    if (url.hostname.replace(/\.$/, "").toLowerCase() !== hostname || hostname.length > 253 || !hostname.split(".").every((label) => label.length >= 1 && label.length <= 63 && /^[a-z0-9](?:[a-z0-9-]*[a-z0-9])?$/.test(label))) return null;
  }
  if (scheme === "http" && !local) return null;
  return `${scheme}://${authority}`;
}

export function validSetupPort(value: number): boolean {
  return Number.isInteger(value) && value >= 1 && value <= 65535;
}

export function formatSetupSecrets(result: Pick<SetupCompleteResponse, "ingestionToken" | "recoveryCodes">): string {
  return ["MediaSender ingestion token:", result.ingestionToken, "Keep this token private. Use it only to connect MediaSender to this server.", ...(result.recoveryCodes.length ? ["", "TOTP recovery codes:", "Each code can be used once. Keep these codes private.", ...result.recoveryCodes] : [])].join("\n");
}

export function revealSetupStep(container: HTMLElement, row: HTMLElement): void {
  const outer = container.getBoundingClientRect();
  const inner = row.getBoundingClientRect();
  if (inner.top < outer.top) container.scrollTop -= outer.top - inner.top;
  else if (inner.bottom > outer.bottom) container.scrollTop += inner.bottom - outer.bottom;
}
