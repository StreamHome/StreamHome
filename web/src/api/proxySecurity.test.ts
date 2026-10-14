// @vitest-environment node
import type { IncomingMessage } from "node:http";
import { describe, expect, it } from "vitest";
import { proxyForwardingHeaders, trustedProxy } from "../../proxySecurity";

const message = (peer: string, headers: IncomingMessage["headers"]): IncomingMessage => ({
  headers,
  socket: { remoteAddress: peer },
}) as IncomingMessage;

describe("trusted proxy forwarding", () => {
  it("matches configured IPv4 and IPv6 proxy networks", () => {
    expect(trustedProxy("::ffff:127.0.0.1", "127.0.0.1/32,::1/128")).toBe(true);
    expect(trustedProxy("::1", "::1/128")).toBe(true);
    expect(trustedProxy("10.4.0.3", "10.4.0.0/24")).toBe(true);
    expect(trustedProxy("10.5.0.3", "10.4.0.0/24")).toBe(false);
    expect(trustedProxy("198.51.100.20", "invalid/999")).toBe(false);
  });

  it("replaces all untrusted identity and protocol values", () => {
    expect(proxyForwardingHeaders(message("198.51.100.20", {
      host: "192.168.1.25:3000",
      "x-forwarded-for": "203.0.113.50",
      "x-forwarded-proto": "https",
      "x-forwarded-host": "attacker.example",
    }), "127.0.0.1/32")).toEqual({
      "x-forwarded-for": "198.51.100.20",
      "x-forwarded-proto": "http",
      "x-forwarded-host": "192.168.1.25:3000",
    });
  });

  it("preserves HTTPS through a complete trusted proxy chain", () => {
    expect(proxyForwardingHeaders(message("127.0.0.1", {
      host: "localhost:3000",
      "x-forwarded-for": "198.51.100.20, 127.0.0.1",
      "x-forwarded-proto": "https, http",
      "x-forwarded-host": "watch.example:443",
    }), "127.0.0.1/32")).toEqual({
      "x-forwarded-for": "198.51.100.20",
      "x-forwarded-proto": "https",
      "x-forwarded-host": "watch.example:443",
    });
  });

  it("discards forged prefix values before an untrusted hop", () => {
    const result = proxyForwardingHeaders(message("127.0.0.1", {
      host: "watch.example",
      "x-forwarded-for": "203.0.113.50, 198.51.100.20",
      "x-forwarded-proto": "https, http",
      "x-forwarded-host": "attacker.example, watch.example",
    }), "127.0.0.1/32");
    expect(result["x-forwarded-for"]).toBe("198.51.100.20");
    expect(result["x-forwarded-proto"]).toBe("http");
    expect(result["x-forwarded-host"]).toBe("watch.example");
  });

  it("fails closed for ambiguous protocol chains", () => {
    const result = proxyForwardingHeaders(message("127.0.0.1", {
      host: "watch.example",
      "x-forwarded-for": "198.51.100.20",
      "x-forwarded-proto": "https, http",
    }), "127.0.0.1/32");
    expect(result["x-forwarded-proto"]).toBe("http");
  });
});
