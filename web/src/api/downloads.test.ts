import { describe, expect, it } from "vitest";
import { normalizeDownload, normalizeDownloadFailure } from "./downloads";

const task = { id: "task-1", title: "Example", status: "Failed", progress: 0, speed: "Failed", eta: "00:00:00" };
const failure = { code: "SOURCE_FORBIDDEN", message: "The source provider refused access from this server.", guidance: "Check source permissions or contact the source provider.", stage: "source_preparation", input_role: "video", http_status: 403 };

describe("download failure normalization", () => {
  it("supports old events and normalizes known context only", () => {
    expect(normalizeDownload(task)).toEqual(task);
    expect(normalizeDownload({ ...task, failure: { ...failure, private_url: "https://private.invalid" } })?.failure).toEqual({
      code: failure.code, message: failure.message, guidance: failure.guidance, stage: "source_preparation", inputRole: "video", httpStatus: 403,
    });
  });
  it("ignores malformed optional failures without losing the task", () => {
    for (const bad of [null, [], "private", { ...failure, code: "https://private.invalid" }, { ...failure, message: "<script>private</script>" }, { ...failure, guidance: "Cookie: private" }, { ...failure, message: "x".repeat(401) }]) {
      expect(normalizeDownload({ ...task, failure: bad })).toEqual(task);
    }
    expect(normalizeDownloadFailure({ ...failure, stage: [], input_role: "private", http_status: true })).toEqual({ code: failure.code, message: failure.message, guidance: failure.guidance });
  });
  it("requires consistent provider evidence and hides stale details on nonfailed tasks", () => {
    const cf = { ...failure, provider: "cloudflare", provider_code: "1005" };
    expect(normalizeDownloadFailure(cf)?.providerCode).toBe("1005");
    expect(normalizeDownloadFailure({ ...cf, http_status: 404 })?.providerCode).toBeUndefined();
    expect(normalizeDownloadFailure({ ...cf, provider_code: 1005 })?.providerCode).toBeUndefined();
    for (const status of ["Pending", "Downloading", "Completed"]) {
      expect(normalizeDownload({ ...task, status, failure: cf })?.failure).toBeUndefined();
    }
  });
});
