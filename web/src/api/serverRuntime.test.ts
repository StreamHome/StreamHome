import { describe, expect, it } from "vitest";
import { webListener } from "../../serverRuntime";

const managed = { STREAMHOME_DEPLOYMENT_REVISION: "12249ab9-9cc4-4f07-8e5f-2fe3e2f1bf7b", WEB_BIND: "127.0.0.1" };
describe("managed web listeners", () => {
  it.each(["", "80", "8000", "65536", "3000suffix", " 3000", "+3000", "3e3", "٣٠٠٠"])("rejects malformed or reserved port %j without fallback", (WEB_PORT) => {
    expect(() => webListener({ ...managed, WEB_PORT })).toThrow();
  });
  it("binds the reviewed address and preserves legacy low ports", () => {
    expect(webListener({ ...managed, WEB_PORT: "3030" })).toEqual({ port: 3030, host: "127.0.0.1" });
    expect(webListener({ WEB_PORT: "80" })).toEqual({ port: 80, host: "0.0.0.0" });
    expect(webListener({ ...managed, WEB_PORT: "3030", WEB_BIND: "0.0.0.0" }).host).toBe("0.0.0.0");
  });
  it("requires a complete managed deployment identity and bind address", () => {
    expect(() => webListener({ ...managed, WEB_PORT: "3030", WEB_BIND: undefined })).toThrow();
    expect(() => webListener({ ...managed, WEB_PORT: "3030", STREAMHOME_DEPLOYMENT_REVISION: "invalid" })).toThrow();
  });
});
