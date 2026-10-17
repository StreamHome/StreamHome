import { ApiError, apiGet, apiPost, apiPut } from "./client";

export interface StoragePolicyDraft { offloadEnabled: boolean; localBudgetBytes: number | null; cloudBudgetBytes: number | null; prefetchNextEpisode: boolean; hevcCompressionMode: "auto" | "on" | "off" }
export interface StoragePolicy extends StoragePolicyDraft { revision: number; diskReserveBytes: number }
export interface StorageOperation { id: string; kind: string; status: string; phase: string; createdAt: string; updatedAt: string; mediaId: string | null; title: string | null; bytesTotal: number | null; bytesDone: number | null; releasedBytes: number; errorCode: string | null; message: string | null }
export interface StorageStatus {
  policy: StoragePolicy;
  local: { writtenBytes: number | null; reservedBytes: number | null; protectedBytes: number | null; freeBytes: number | null; diskReserveBytes: number; categories: { key: string; label: string; bytes: number }[]; measuredAt: string | null; stale: boolean; errorCode: string | null };
  cloud: { configured: boolean; reachable: boolean | null; healthState: string; folderName: string | null; usedBytes: number | null; accountTotalBytes: number | null; accountFreeBytes: number | null; measuredAt: string | null; stale: boolean; errorCode: string | null };
  offload: { state: "paused" | "monitoring" | "working" | "blocked"; reason: string | null };
  capabilities: { offload: boolean; keepLocal: boolean; cleanup: boolean };
  operation: StorageOperation | null;
}
export interface StorageReview { readonly reviewId: string; readonly expiresAt: string; readonly policyRevision: number; readonly draft: Readonly<StoragePolicyDraft>; readonly changes: readonly { field: string; before: unknown; after: unknown }[]; readonly effects: readonly string[] }
export interface RetentionItem { id: string; mediaId: string; title: string; kind: "movie" | "episode"; localState: "local" | "cloud" | "both" | "preparing" | "unavailable"; bytes: number | null; kept: boolean; offlineReady: boolean; protectedReason: string | null; operationId: string | null }
export interface StoragePage<T> { items: T[]; nextCursor: string | null }
export interface CleanupReview { readonly planId: string; readonly expiresAt: string; readonly estimatedReleaseBytes: number; readonly steps: readonly { kind: string; bytes: number }[]; readonly blocker: string | null }

type RecordValue = Record<string, unknown>;
const invalid = (): never => { throw new ApiError("The server returned invalid storage data. Check status before continuing.", 0, "invalid_response"); };
const obj = (v: unknown): RecordValue => v && typeof v === "object" && !Array.isArray(v) ? v as RecordValue : invalid();
const str = (v: unknown): string => typeof v === "string" ? v : invalid();
const bool = (v: unknown): boolean => typeof v === "boolean" ? v : invalid();
const num = (v: unknown): number => typeof v === "number" && Number.isSafeInteger(v) && v >= 0 ? v : invalid();
const nullable = <T>(v: unknown, parse: (v: unknown) => T): T | null => v === null ? null : parse(v);
const list = <T>(v: unknown, parse: (v: unknown) => T): T[] => Array.isArray(v) ? v.map(parse) : invalid();
const oneOf = <T extends string>(v: unknown, options: readonly T[]): T => options.includes(v as T) ? v as T : invalid();
const draftFromWire = (v: unknown): StoragePolicyDraft => { const r = obj(v); return { offloadEnabled: bool(r.offload_enabled), localBudgetBytes: nullable(r.local_budget_bytes, num), cloudBudgetBytes: nullable(r.cloud_budget_bytes, num), prefetchNextEpisode: bool(r.prefetch_next_episode), hevcCompressionMode: oneOf(r.hevc_compression_mode, ["auto", "on", "off"] as const) }; };
export const storageDraftToWire = (d: Readonly<StoragePolicyDraft>) => ({ offload_enabled: d.offloadEnabled, local_budget_bytes: d.localBudgetBytes, cloud_budget_bytes: d.cloudBudgetBytes, prefetch_next_episode: d.prefetchNextEpisode, hevc_compression_mode: d.hevcCompressionMode });
const policyFromWire = (v: unknown): StoragePolicy => { const r = obj(v); return { ...draftFromWire(r), revision: num(r.revision), diskReserveBytes: num(r.disk_reserve_bytes) }; };
export function normalizeStorageOperation(v: unknown): StorageOperation { const r = obj(v); return { id: str(r.id), kind: str(r.kind), status: str(r.status), phase: str(r.phase), createdAt: str(r.created_at), updatedAt: str(r.updated_at), mediaId: nullable(r.media_id, str), title: nullable(r.title, str), bytesTotal: nullable(r.bytes_total, num), bytesDone: nullable(r.bytes_done, num), releasedBytes: num(r.released_bytes), errorCode: nullable(r.error_code, str), message: nullable(r.message, str) }; }
export function normalizeStorageStatus(v: unknown): StorageStatus {
  const r = obj(v), l = obj(r.local), c = obj(r.cloud), o = obj(r.offload), a = obj(r.capabilities);
  return { policy: policyFromWire(r.policy), local: { writtenBytes: nullable(l.written_bytes, num), reservedBytes: nullable(l.reserved_bytes, num), protectedBytes: nullable(l.protected_bytes, num), freeBytes: nullable(l.free_bytes, num), diskReserveBytes: num(l.disk_reserve_bytes), categories: list(l.categories, v => { const x = obj(v); return { key: str(x.key), label: str(x.label), bytes: num(x.bytes) }; }), measuredAt: nullable(l.measured_at, str), stale: bool(l.stale), errorCode: nullable(l.error_code, str) }, cloud: { configured: bool(c.configured), reachable: nullable(c.reachable, bool), healthState: str(c.health_state), folderName: nullable(c.folder_name, str), usedBytes: nullable(c.used_bytes, num), accountTotalBytes: nullable(c.account_total_bytes, num), accountFreeBytes: nullable(c.account_free_bytes, num), measuredAt: nullable(c.measured_at, str), stale: bool(c.stale), errorCode: nullable(c.error_code, str) }, offload: { state: oneOf(o.state, ["paused", "monitoring", "working", "blocked"] as const), reason: nullable(o.reason, str) }, capabilities: { offload: bool(a.offload), keepLocal: bool(a.keep_local), cleanup: bool(a.cleanup) }, operation: nullable(r.operation, normalizeStorageOperation) };
}
export function normalizeStorageReview(v: unknown): StorageReview { const r = obj(v); return Object.freeze({ reviewId: str(r.review_id), expiresAt: str(r.expires_at), policyRevision: num(r.policy_revision), draft: Object.freeze(draftFromWire(r.draft)), changes: Object.freeze(list(r.changes, v => { const c = obj(v); return Object.freeze({ field: str(c.field), before: c.before, after: c.after }); })), effects: Object.freeze(list(r.effects, str)) }); }
export function normalizeRetentionItem(v: unknown): RetentionItem { const r = obj(v); return { id: str(r.id), mediaId: str(r.media_id), title: str(r.title), kind: oneOf(r.kind, ["movie", "episode"] as const), localState: oneOf(r.local_state, ["local", "cloud", "both", "preparing", "unavailable"] as const), bytes: nullable(r.bytes, num), kept: bool(r.kept), offlineReady: bool(r.offline_ready), protectedReason: nullable(r.protected_reason, str), operationId: nullable(r.operation_id, str) }; }
function page<T>(v: unknown, parse: (v: unknown) => T): StoragePage<T> { const r = obj(v); return { items: list(r.items, parse), nextCursor: nullable(r.next_cursor, str) }; }
const base = "/api/admin/storage";
export const getStorageStatus = async () => normalizeStorageStatus(await apiGet<unknown>(`${base}/status`));
export const previewStoragePolicy = async (draft: StoragePolicyDraft, revision: number) => normalizeStorageReview(await apiPost<unknown>(`${base}/policy/preview`, { draft: storageDraftToWire(draft), expected_revision: revision }));
export async function saveStoragePolicy(review: StorageReview, requestId: string) { const r = obj(await apiPut<unknown>(`${base}/policy`, { review_id: review.reviewId, draft: storageDraftToWire(review.draft), expected_revision: review.policyRevision, request_id: requestId })); return { policy: policyFromWire(r.policy), operation: normalizeStorageOperation(r.operation) }; }
export const getStorageOperation = async (id: string) => normalizeStorageOperation(obj(await apiGet<unknown>(`${base}/operations/${encodeURIComponent(id)}`)).operation);
export const refreshStorage = async (requestId: string) => normalizeStorageOperation(obj(await apiPost<unknown>(`${base}/refresh`, { request_id: requestId })).operation);
export const getStorageRetention = async (cursor?: string) => page(await apiGet<unknown>(`${base}/retention${cursor ? `?cursor=${encodeURIComponent(cursor)}` : ""}`), normalizeRetentionItem);
export const getStorageActivity = async (cursor?: string) => page(await apiGet<unknown>(`${base}/activity${cursor ? `?cursor=${encodeURIComponent(cursor)}` : ""}`), normalizeStorageOperation);
export async function setStorageKeep(retentionId: string, keepLocal: boolean, requestId: string) { const r = obj(await apiPut<unknown>(`${base}/retention/${encodeURIComponent(retentionId)}`, { keep_local: keepLocal, request_id: requestId })); return { item: normalizeRetentionItem(r.item), operation: nullable(r.operation, normalizeStorageOperation) }; }
export async function previewStorageCleanup(): Promise<CleanupReview> { const r = obj(await apiPost<unknown>(`${base}/cleanup/preview`, {})); return Object.freeze({ planId: str(r.plan_id), expiresAt: str(r.expires_at), estimatedReleaseBytes: num(r.estimated_release_bytes), steps: Object.freeze(list(r.steps, v => { const x = obj(v); return Object.freeze({ kind: str(x.kind), bytes: num(x.bytes) }); })), blocker: nullable(r.blocker, str) }); }
export const runStorageCleanup = async (review: CleanupReview, requestId: string) => normalizeStorageOperation(obj(await apiPost<unknown>(`${base}/cleanup`, { plan_id: review.planId, request_id: requestId })).operation);
