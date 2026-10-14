import { BlockList, isIP } from "node:net";
import type { IncomingMessage } from "node:http";
import { createProxyMiddleware } from "http-proxy-middleware";

function address(value: string): string {
  return value.startsWith("::ffff:") && isIP(value.slice(7)) === 4 ? value.slice(7) : value;
}

export function trustedProxy(value: string, cidrs: string): boolean {
  const normalized = address(value);
  const family = isIP(normalized);
  if (!family) return false;
  for (const raw of cidrs.split(",")) {
    const [network, prefix] = raw.trim().split("/");
    const networkAddress = address(network);
    const networkFamily = isIP(networkAddress);
    const bits = prefix === undefined ? (networkFamily === 4 ? 32 : 128) : Number(prefix);
    if (!networkFamily || !Number.isInteger(bits) || bits < 0 || bits > (networkFamily === 4 ? 32 : 128)) continue;
    const list = new BlockList();
    list.addSubnet(networkAddress, bits, networkFamily === 4 ? "ipv4" : "ipv6");
    if (list.check(normalized, family === 4 ? "ipv4" : "ipv6")) return true;
  }
  return false;
}

export function proxyForwardingHeaders(request: IncomingMessage, cidrs: string): Record<string, string> {
  const peer = address(request.socket.remoteAddress ?? "");
  const secure = "encrypted" in request.socket && Boolean(request.socket.encrypted);
  let client = peer;
  let protocol = secure ? "https" : "http";
  let host = request.headers.host ?? "";
  if (trustedProxy(peer, cidrs)) {
    const chain = String(request.headers["x-forwarded-for"] ?? "").split(",").map((value) => address(value.trim()));
    const validChain = chain.length > 0 && chain.every((value) => isIP(value));
    let boundary = 0;
    if (validChain) {
      for (let index = chain.length - 1; index >= 0; index -= 1) {
        if (!trustedProxy(chain[index], cidrs)) {
          boundary = index;
          break;
        }
      }
      client = chain[boundary];
    }
    const forwardedValue = (name: string): string | undefined => {
      const values = String(request.headers[name] ?? "").split(",").map((value) => value.trim());
      if (values.length === 1) return values[0] || undefined;
      return validChain && values.length === chain.length ? values[boundary] || undefined : undefined;
    };
    const forwardedProtocol = forwardedValue("x-forwarded-proto")?.toLowerCase();
    if (forwardedProtocol === "http" || forwardedProtocol === "https") protocol = forwardedProtocol;
    host = forwardedValue("x-forwarded-host") ?? host;
  }
  return {
    "x-forwarded-for": client,
    "x-forwarded-proto": protocol,
    "x-forwarded-host": host,
  };
}

export function createTrustedProxy(target: string, cidrs: string) {
  return createProxyMiddleware({
    target,
    changeOrigin: true,
    xfwd: false,
    on: {
      proxyReq: (proxyRequest, request) => {
        for (const name of proxyRequest.getHeaderNames()) {
          if (name === "forwarded" || name.startsWith("x-forwarded-")) proxyRequest.removeHeader(name);
        }
        for (const [name, value] of Object.entries(proxyForwardingHeaders(request, cidrs))) {
          proxyRequest.setHeader(name, value);
        }
      },
    },
  });
}
