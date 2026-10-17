import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { ApiError } from "./client";
import { getStorageStatus, normalizeStorageStatus, normalizeStorageReview, saveStoragePolicy, setStorageKeep, getStorageOperation } from "./storage";
const draft = { local_budget_bytes: 60e9, cloud_budget_bytes: 2e12, offload_enabled: false, prefetch_next_episode: false, hevc_compression_mode: "auto" };
const policy = { ...draft, revision: 8, disk_reserve_bytes: 8e9 };
const wire = { policy, local: { written_bytes: null, reserved_bytes: 0, protected_bytes: null, free_bytes: null, disk_reserve_bytes: 8e9, categories: [], measured_at: null, stale: true, error_code: "measurement_unavailable" }, cloud: { configured: true, reachable: null, health_state: "unknown", folder_name: "Folder", used_bytes: null, account_total_bytes: null, account_free_bytes: null, measured_at: null, stale: true, error_code: null }, offload: { state: "paused", reason: null }, capabilities: { offload: true, keep_local: true, cleanup: false }, operation: null };
const operation = { id: "op", kind: "policy", status: "completed", phase: "saved", created_at: "2026-10-17T00:00:00Z", updated_at: "2026-10-17T00:00:00Z", media_id: null, title: null, bytes_total: null, bytes_done: null, released_bytes: 0, error_code: null, message: null };
const json = (v: unknown) => new Response(JSON.stringify(v), { status: 200, headers: { "Content-Type": "application/json" } });
let fetcher: ReturnType<typeof vi.fn>;
beforeEach(() => { fetcher = vi.fn(); vi.stubGlobal("fetch", fetcher); });
afterEach(() => vi.unstubAllGlobals());
describe("storage API normalization and review binding", () => {
  it("keeps unknown usage null and normalizes separate folder, disk and account measures", async () => {
    fetcher.mockResolvedValue(json(wire)); const result = await getStorageStatus(); expect(result.local.writtenBytes).toBeNull(); expect(result.local.reservedBytes).toBe(0); expect(result.cloud.usedBytes).toBeNull(); expect(result.cloud.accountFreeBytes).toBeNull(); expect(result.capabilities.cleanup).toBe(false); expect(fetcher.mock.calls[0][0]).toBe("/api/admin/storage/status");
  });
  it.each(["written_bytes", "reserved_bytes", "free_bytes"])("rejects invalid %s instead of rendering false capacity", field => {
    expect(() => normalizeStorageStatus({ ...wire, local: { ...wire.local, [field]: -1 } })).toThrow(ApiError);
    expect(() => normalizeStorageStatus({ ...wire, local: { ...wire.local, [field]: "100" } })).toThrow(ApiError);
  });
  it("freezes the canonical review and sends its exact ID, draft and revision", async () => {
    const review = normalizeStorageReview({ review_id: "review-one", policy_revision: 8, expires_at: "2099-01-01T00:00:00Z", draft, changes: [], effects: [] }); expect(Object.isFrozen(review)).toBe(true); expect(Object.isFrozen(review.draft)).toBe(true);
    fetcher.mockResolvedValue(json({ policy, operation })); await saveStoragePolicy(review, "request-one"); expect(fetcher).toHaveBeenCalledTimes(1); expect(fetcher.mock.calls[0][0]).toBe("/api/admin/storage/policy"); expect(fetcher.mock.calls[0][1].method).toBe("PUT"); expect(JSON.parse(fetcher.mock.calls[0][1].body)).toEqual({ review_id: "review-one", expected_revision: 8, draft, request_id: "request-one" });
  });
  it("treats malformed mutation acknowledgement as unknown and never retries it", async () => {
    const review = normalizeStorageReview({ review_id: "review-one", policy_revision: 8, expires_at: "2099-01-01T00:00:00Z", draft, changes: [], effects: [] }); fetcher.mockResolvedValue(json({ policy })); await expect(saveStoragePolicy(review, "request-one")).rejects.toMatchObject({ code: "invalid_response" }); expect(fetcher).toHaveBeenCalledTimes(1);
  });
  it("reads a durable operation acknowledgement by the original request ID", async () => {
    fetcher.mockResolvedValue(json({ operation }));
    expect((await getStorageOperation("request-one")).id).toBe("op");
    expect(fetcher.mock.calls[0][0]).toBe("/api/admin/storage/operations/request-one");
    expect(fetcher.mock.calls[0][1].method).toBe("GET");
  });
  it("requires server offline readiness proof in keep acknowledgements", async () => {
    fetcher.mockResolvedValue(json({ item: { id: "id", media_id: "tv_1", title: "Title", kind: "episode", local_state: "preparing", bytes: null, kept: true, protected_reason: null, operation_id: "op" }, operation })); await expect(setStorageKeep("tv_1", true, "request-keep")).rejects.toMatchObject({ code: "invalid_response" });
  });
});
