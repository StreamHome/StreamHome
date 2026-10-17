import React, { useCallback, useEffect, useRef, useState } from "react";
import { formatStorageBytes } from "../../../utils/format";
import * as storage from "../../../api/storage";
import { ApiError } from "../../../api/client";
import { getSettings } from "../../../api/system";
import { Button } from "../../../components/ui/Button";
import { GlassPane } from "../../../components/ui/GlassPane";
import { AdminSelect } from "../../../components/ui/AdminSelect";
import { appUrl, parseAppQuery, STORAGE_VIEWS, type StorageView } from "../../../navigation/queryState";
import type { SystemSettings } from "../../../types/api";
import { SudoModal } from "../SudoModal";
import { AdminHelp } from "../AdminHelp";
import { DriveSetupCard } from "./DriveSetupCard";
import "./StorageWorkspace.css";
import { useStorageAutoRead } from "./useStorageAutoRead";

type Unit = "GiB" | "TiB";
type Pending = { requestId: string; operationId?: string; kind: "policy" | "retention" | "cleanup" };
export interface StorageDraft {
  localBudget: string; localUnit: Unit; cloudBudget: string; cloudUnit: Unit;
  offloadEnabled: boolean; prefetchNextEpisode: boolean; hevcCompressionMode: storage.StoragePolicyDraft["hevcCompressionMode"];
  revision: number; review?: storage.StorageReview; pending?: Pending;
}
const factor = (unit: Unit) => unit === "TiB" ? 1024 ** 4 : 1024 ** 3;
export { formatStorageBytes } from "../../../utils/format";
const budgetInput = (bytes: number | null) => bytes === null ? { value: "", unit: "GiB" as Unit } : bytes >= 1024 ** 4 && bytes % (1024 ** 4) === 0 ? { value: String(bytes / 1024 ** 4), unit: "TiB" as Unit } : { value: String(bytes / 1024 ** 3), unit: "GiB" as Unit };
const initialDraft = (p: storage.StoragePolicy): StorageDraft => { const l = budgetInput(p.localBudgetBytes), c = budgetInput(p.cloudBudgetBytes); return { localBudget: l.value, localUnit: l.unit, cloudBudget: c.value, cloudUnit: c.unit, offloadEnabled: p.offloadEnabled, prefetchNextEpisode: p.prefetchNextEpisode, hevcCompressionMode: p.hevcCompressionMode, revision: p.revision }; };
const bytes = (value: string, unit: Unit) => value.trim() === "" ? null : Math.round(Number(value) * factor(unit));
const policyDraft = (d: StorageDraft): storage.StoragePolicyDraft => ({ localBudgetBytes: bytes(d.localBudget, d.localUnit), cloudBudgetBytes: bytes(d.cloudBudget, d.cloudUnit), offloadEnabled: d.offloadEnabled, prefetchNextEpisode: d.prefetchNextEpisode, hevcCompressionMode: d.hevcCompressionMode });
const samePolicy = (a: storage.StoragePolicyDraft, b: storage.StoragePolicyDraft) => JSON.stringify(storage.storageDraftToWire(a)) === JSON.stringify(storage.storageDraftToWire(b));
const messageFor = (e: unknown) => e instanceof Error ? e.message : "Storage is unavailable. Check status before continuing.";
const humanize = (s: string) => { const text = s.replace(/_/g, " "); return text.charAt(0).toUpperCase() + text.slice(1); };
const kindLabel = (value: string) => ({ refresh: "Storage measurement", policy: "Policy update", policy_save: "Policy update", keep_local: "Keep local", cleanup: "Local cleanup", offload: "Cloud offloading", prefetch: "Next episode preparation", backup_sync: "Backup copy" }[value] ?? "Storage operation");
const phaseLabel = (value: string) => ({ queued: "Waiting to start", starting: "Starting", measuring_local: "Measuring local usage", measuring_cloud: "Measuring Drive usage", reconciling_assets: "Checking stored copies", measured: "Usage measurement finished", saved: "Policy saved", uploading: "Uploading to Drive", downloading: "Downloading to this server", verifying: "Verifying the stored copy", releasing_cache: "Clearing idle playback cache", releasing_local_copy: "Releasing verified local copies", preparing_playback: "Preparing playback files", ready_offline: "Ready offline on this server", protection_removed: "Keep local protection removed", retained_local_copy: "Local copy retained", cleanup_debt: "Temporary cleanup pending", completed: "Finished", failed: "Operation could not finish", blocked: "Waiting for an issue to be resolved", interrupted: "Interrupted; retained data needs checking", recovery_required: "Recovery needs attention", reconciled: "Stored copies checked" }[value] ?? "Progress is being checked");
const errorExplanation = (value: string | null) => ({
  storage_retained_data_protected: "Caches and subtitles are retained. Review a new media-only cleanup plan.",
  storage_retained_data_blocks_capacity: "No eligible video or audio can be released. Caches and subtitles are retained; new work needs more available space.",
  storage_no_releasable_media: "No verified media copies can be released. Caches and subtitles remain protected.",
  storage_cleanup_plan_expired: "Storage protection or the reviewed files changed. Review cleanup again.",
  no_safe_candidates: "No media can currently be released without violating data protection.",
  storage_inventory_unreadable: "Some managed files could not be read. Check the server's storage access permissions.",
  storage_inventory_limit: "The file inventory exceeded the measurement limit. Usage is incomplete.",
  storage_filesystem_unavailable: "The server storage filesystem is unavailable.",
  storage_measurement_unknown: "Local usage could not be measured.",
  local_measurement_failed: "Local usage could not be measured.",
  measurement_unavailable: "Usage could not be measured.",
  storage_cloud_measurement_unknown: "Drive usage has not been measured yet.",
  storage_cloud_measurement_failed: "Drive usage could not be measured.",
  storage_cloud_measurement_invalid: "Drive returned incomplete capacity information.",
  drive_permission_denied: "Drive denied access. Review the folder connection permissions.",
  drive_auth_required: "The Drive connection needs authorization again.",
  drive_unauthorized: "The Drive connection needs authorization again.",
  drive_quota_exceeded: "The Google account has no remaining storage capacity.",
  drive_network_error: "The server could not reach Google Drive.",
  rclone_failed: "The Drive storage check could not finish.",
  storage_budgets_required: "Set both storage budgets before enabling offloading.",
  drive_not_connected: "Connect Google Drive to measure cloud usage.",
  drive_unavailable: "Google Drive could not be reached.",
  provider_unavailable: "Google Drive could not be reached.",
  drive_timeout: "Google Drive took too long to respond.",
  drive_rate_limited: "Google Drive is temporarily limiting requests.",
  drive_not_found: "The selected Drive folder could not be found.",
  storage_disk_space_insufficient: "Free disk space is below the server reserve.",
  storage_local_budget_exceeded: "The local storage budget has been reached.",
  storage_cloud_budget_exceeded: "The cloud folder budget has been reached.",
  storage_operation_interrupted: "The operation was interrupted. Existing copies are retained for recovery.",
  storage_operation_failed: "The operation could not finish. Existing copies remain protected.",
  storage_verification_failed: "The stored copy could not be verified. The local original was retained.",
  storage_candidate_cleanup_pending: "The verified copy is available; temporary cleanup remains pending.",
}[value ?? ""] ?? "The server could not complete this storage check.");
const changeLabel = (field: string) => ({ local_budget_bytes: "Local budget", cloud_budget_bytes: "Cloud folder budget", offload_enabled: "Automatic offloading", prefetch_next_episode: "Next episode preparation", hevc_compression_mode: "HEVC compression" }[field] ?? humanize(field));
const changeValue = (field: string, value: unknown) => field.includes("budget") ? value === null ? "Not set" : typeof value === "number" ? formatStorageBytes(value) : "Unavailable" : typeof value === "boolean" ? value ? "Enabled" : "Paused" : field === "hevc_compression_mode" ? ({ auto: "Automatic", on: "Always enabled", off: "Disabled" }[String(value)] ?? String(value)) : String(value);
const displayDate = (s: string | null) => s && Number.isFinite(Date.parse(s)) ? new Date(s).toLocaleString() : "Not measured";
const complete = (o: storage.StorageOperation) => ["completed", "succeeded", "saved", "failed", "cancelled", "blocked"].includes(o.status);
const requestId = () => crypto.randomUUID();

export function mergeStoragePage<T extends { id: string }>(previous: storage.StoragePage<T> | null, next: storage.StoragePage<T>, append: boolean): storage.StoragePage<T> {
  const ordered = append ? [...(previous?.items ?? []), ...next.items] : [...next.items, ...(previous?.items ?? [])];
  const fresh = new Map(next.items.map(item => [item.id, item]));
  const seen = new Set<string>();
  const items = ordered.filter(item => { if (seen.has(item.id)) return false; seen.add(item.id); return true; }).map(item => fresh.get(item.id) ?? item);
  // An overlapping head bridges into the cached range. A disjoint head must
  // retain its own continuation so a burst of new records cannot hide the gap.
  const cachedIds = new Set(previous?.items.map(item => item.id));
  const overlaps = next.items.some(item => cachedIds.has(item.id));
  return { items, nextCursor: append || !previous || !overlaps ? next.nextCursor : previous.nextCursor };
}

const segmentClass = (index: number) => ["", "ss-segcache", "ss-segcore", "ss-segreserved"][Math.min(index, 3)];
const age = (value: string | null) => {
  if (!value || !Number.isFinite(Date.parse(value))) return "Not measured";
  const minutes = Math.max(0, Math.floor((Date.now() - Date.parse(value)) / 60000));
  return minutes < 1 ? "less than a minute ago" : minutes < 60 ? `${minutes} min ago` : minutes < 1440 ? `${Math.floor(minutes / 60)} hr ago` : `${Math.floor(minutes / 1440)} days ago`;
};
function Measurement({ label, value }: { label: string; value: { measuredAt: string | null; stale: boolean; errorCode: string | null; refresh?: storage.StorageRefresh } }) {
  const failed = value.refresh?.state === "error" || Boolean(value.errorCode);
  const running = value.refresh?.state === "running";
  const phases: Record<string, string> = { scanning_local: "Measuring local usage", waiting_capacity: "Waiting for storage work to finish", measuring_folder: "Measuring Drive folder", measuring_account: "Measuring account capacity", reconciling_assets: "Checking stored copies" };
  return <div className="ss-measurement">
    <p className="ss-help">{label} last successful measurement: <time title={displayDate(value.measuredAt)} dateTime={value.measuredAt ?? undefined}>{age(value.measuredAt)}</time></p>
    {running && <p className="ss-help" role="status">{phases[value.refresh?.phase ?? ""] ?? "Updating measurements"}…</p>}
    {value.refresh?.state === "unconfigured" ? <p className="ss-help">Measurements begin after Drive is configured.</p> : <>
      {failed && <p className="ss-error">{errorExplanation(value.refresh?.errorCode ?? value.errorCode)}</p>}
      {(failed || value.stale) && <p className="ss-help">{value.measuredAt ? "Last successful values are retained." : "No successful measurement is available yet."} Automatic checks continue.</p>}
    </>}
  </div>;
}

function Panel({ title, children, className = "", label }: { title?: string; children: React.ReactNode; className?: string; label?: string }) {
  return <GlassPane as="section" className={`ss-panel ${className}`} spotlight={false} aria-label={label ?? title}>{title && <div className="ss-panelhead"><h2>{title}</h2></div>}{children}</GlassPane>;
}
function OperationRow({ item }: { item: storage.StorageOperation }) {
  return <article className="ss-activity"><span aria-hidden="true">↗</span><div><strong>{item.title || kindLabel(item.kind)} · {humanize(item.status)}</strong><p>{item.message || phaseLabel(item.phase)}{item.bytesDone !== null ? ` · ${formatStorageBytes(item.bytesDone)}${item.bytesTotal !== null ? ` of ${formatStorageBytes(item.bytesTotal)}` : ""}` : ""}{item.releasedBytes > 0 ? ` · ${formatStorageBytes(item.releasedBytes)} released` : ""}</p>{item.errorCode && <p className="ss-danger">{errorExplanation(item.errorCode)}</p>}</div><time dateTime={item.updatedAt}>{displayDate(item.updatedAt)}</time></article>;
}

export function StoragePanel({ retainedDraft, onDraftChange, view: controlledView, onViewChange, active = true }: {
  retainedDraft?: StorageDraft;
  onDraftChange?: (draft: StorageDraft | undefined) => void;
  view?: StorageView;
  onViewChange?: (view: StorageView) => void;
  active?: boolean;
} = {}) {
  const [status, setStatus] = useState<storage.StorageStatus | null>(null);
  const statusRef = useRef<storage.StorageStatus | null>(null);
  const [settings, setSettings] = useState<SystemSettings | null>(null);
  const [draft, setDraft] = useState<StorageDraft | undefined>(retainedDraft);
  const draftRef = useRef(retainedDraft);
  const [localView, setLocalView] = useState<StorageView>("overview");
  const view = controlledView ?? localView;
  const [error, setError] = useState("");
  const [message, setMessage] = useState("");
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState(false);
  const busyRef = useRef(false);
  const [reauthNeeded, setReauthNeeded] = useState(false);
  const reauthRef = useRef(false);
  const [sudo, setSudo] = useState<"read" | "policy" | "cleanup" | { item: storage.RetentionItem } | null>(null);
  const [cleanup, setCleanup] = useState<storage.CleanupReview | null>(null);
  const [breakdown, setBreakdown] = useState(false);
  const breakdownTrigger = useRef<HTMLButtonElement>(null);
  const [connection, setConnection] = useState(false);
  const [retention, setRetention] = useState<storage.StoragePage<storage.RetentionItem> | null>(null);
  const [activity, setActivity] = useState<storage.StoragePage<storage.StorageOperation> | null>(null);
  const [pageBusy, setPageBusy] = useState(false);
  const [pageError, setPageError] = useState("");
  const [readEpoch, setReadEpoch] = useState(0);
  const accepted = useRef(new Set<string>());
  const policySync = useRef(false);
  const [policySyncNeeded, setPolicySyncNeeded] = useState(false);
  const live = useRef(false);
  const reads = useRef(0);
  const pages = useRef({ retention: 0, activity: 0 });
  const pageControllers = useRef(new Set<AbortController>());
  const appendOwners = useRef<{ retention: AbortController | null; activity: AbortController | null }>({ retention: null, activity: null });
  const loadedCursors = useRef({ retention: new Set<string>(), activity: new Set<string>() });
  const heading = useRef<HTMLHeadingElement>(null);
  const reviewHeading = useRef<HTMLHeadingElement>(null);
  const storeDraft = (next: StorageDraft | undefined) => {
    draftRef.current = next;
    setDraft(next);
    onDraftChange?.(next);
  };
  const handleError = useCallback((e: unknown) => {
    setError(messageFor(e));
    if (e instanceof ApiError && (e.code === "reauthentication_required" || e.status === 401 || e.status === 403)) {
      reauthRef.current = true;
      setReauthNeeded(true);
    }
  }, []);
  const load = async (signal?: AbortSignal) => {
    const read = ++reads.current;
    const current = () => live.current && !signal?.aborted && reads.current === read;
    if (!statusRef.current) setLoading(true);
    try {
      const next = await storage.getStorageStatus({ signal });
      if (!current()) return;
      const previous = statusRef.current;
      const currentDraft = draftRef.current;
      const clean = policySync.current || !currentDraft || (previous && !currentDraft.review && !currentDraft.pending && samePolicy(policyDraft(currentDraft), previous.policy));
      statusRef.current = next;
      setStatus(next);
      if (clean) storeDraft(initialDraft(next.policy));
      policySync.current = false;
      setPolicySyncNeeded(false);
      setError("");
      if (!settings) {
        const system = await getSettings({ signal }).catch(() => null);
        if (current()) setSettings(system);
      }
    } catch (e) {
      if (current()) handleError(e);
      throw e;
    } finally { if (current()) setLoading(false); }
  };
  useEffect(() => {
    live.current = true;
    return () => {
      live.current = false;
      reads.current += 1;
      pageControllers.current.forEach(controller => controller.abort());
    };
  }, []);
  useEffect(() => {
    if (!active) {
      pageControllers.current.forEach(controller => controller.abort());
      setSudo(null);
      setMessage("");
      setPageBusy(false);
    }
  }, [active]);
  useEffect(() => {
    const suspendPages = () => {
      if (document.visibilityState !== "hidden") return;
      pageControllers.current.forEach(controller => controller.abort());
      setPageBusy(false);
    };
    document.addEventListener("visibilitychange", suspendPages);
    return () => document.removeEventListener("visibilitychange", suspendPages);
  }, []);
  const loadPage = async (kind: "retention" | "activity", cursor?: string, parentSignal?: AbortSignal, refreshLoaded = false) => {
    // Explicit pagination owns its generation until settled. Background ranges
    // yield using the live owner, including when an append starts mid-cycle.
    const existingAppend = appendOwners.current[kind];
    if (existingAppend && !existingAppend.signal.aborted) return;
    const explicitAppend = Boolean(cursor) && !refreshLoaded && !parentSignal;
    const controller = new AbortController();
    if (explicitAppend) appendOwners.current[kind] = controller;
    const releaseAppend = () => {
      if (explicitAppend && appendOwners.current[kind] === controller) {
        appendOwners.current[kind] = null;
        if (live.current) setPageBusy(false);
      }
    };
    controller.signal.addEventListener("abort", releaseAppend, { once: true });
    const abort = () => controller.abort();
    parentSignal?.addEventListener("abort", abort, { once: true });
    if (parentSignal?.aborted) controller.abort();
    pageControllers.current.add(controller);
    const signal = controller.signal;
    const read = ++pages.current[kind];
    const current = () => live.current && !signal.aborted && pages.current[kind] === read;
    if (cursor && !refreshLoaded) setPageBusy(true);
    try {
      if (kind === "retention") {
        const result = await storage.getStorageRetention(cursor, { signal });
        if (current()) setRetention(previous => {
          const merged = mergeStoragePage(previous, result, Boolean(cursor));
          return refreshLoaded && previous ? { ...merged, nextCursor: previous.nextCursor } : merged;
        });
      } else {
        const result = await storage.getStorageActivity(cursor, { signal });
        if (current()) setActivity(previous => {
          const merged = mergeStoragePage(previous, result, Boolean(cursor));
          return refreshLoaded && previous ? { ...merged, nextCursor: previous.nextCursor } : merged;
        });
      }
      if (current()) { if (cursor && !refreshLoaded) loadedCursors.current[kind].add(cursor); setPageError(""); }
    } catch (e) {
      if (current()) {
        setPageError(messageFor(e));
        if (e instanceof ApiError && (e.code === "reauthentication_required" || e.status === 401 || e.status === 403)) handleError(e);
      }
      throw e;
    } finally {
      parentSignal?.removeEventListener("abort", abort);
      pageControllers.current.delete(controller);
      releaseAppend();
      controller.signal.removeEventListener("abort", releaseAppend);
    }
  };
  const checkOperation = async (signal: AbortSignal) => {
    const pending = draftRef.current?.pending;
    if (!pending || busyRef.current) return;
    try {
      const op = await storage.getStorageOperation(pending.operationId ?? pending.requestId, { signal });
      if (!live.current || signal.aborted || draftRef.current?.pending?.requestId !== pending.requestId) return;
      const current = draftRef.current;
      // A positive receipt resolves uncertainty, even when background work continues.
      if (!complete(op)) accepted.current.add(op.id);
      const succeeded = !["failed", "cancelled", "blocked"].includes(op.status);
      if (pending.kind === "policy" && succeeded) {
        policySync.current = true;
        setPolicySyncNeeded(true);
      }
      storeDraft({ ...current, pending: undefined, review: undefined });
      setError("");
      setMessage(complete(op) ? `${kindLabel(op.kind)}: ${humanize(op.status)}.` : "The server accepted the operation. Progress updates automatically in Activity.");
      if (pending.kind === "policy" && succeeded) await load(signal);
    } catch (e) {
      if (!signal.aborted && live.current) {
        if (e instanceof ApiError && e.status === 404) setError("The server has not confirmed this request yet. It remains pending; status checks continue automatically.");
        else handleError(e);
      }
      throw e;
    }
  };
  useStorageAutoRead(active, reauthNeeded, `${view}:${readEpoch}`, async signal => {
    await load(signal);
    if (signal.aborted || reauthRef.current) return 60000;
    await checkOperation(signal);
    if (signal.aborted || reauthRef.current) return 60000;
    for (const id of accepted.current) {
      let op: storage.StorageOperation;
      try { op = await storage.getStorageOperation(id, { signal }); }
      catch (e) { if (!signal.aborted) handleError(e); throw e; }
      if (signal.aborted) return 60000;
      if (complete(op)) accepted.current.delete(id);
    }
    if (view !== "policy" && !pageBusy) {
      const kind = view === "activity" ? "activity" : "retention";
      await loadPage(kind, undefined, signal);
      for (const cursor of loadedCursors.current[kind]) {
        if (signal.aborted) return 60000;
        if (appendOwners.current[kind] && !appendOwners.current[kind]!.signal.aborted) break;
        await loadPage(kind, cursor, signal, true);
      }
    }
    const current = statusRef.current;
    return draftRef.current?.pending || accepted.current.size || current?.offload.state === "working" || (current?.operation && !complete(current.operation)) || current?.local.refresh?.state === "running" || current?.cloud.refresh?.state === "running" ? 3000 : 15000;
  });
  const refreshRead = async () => { setSettings(null); setReadEpoch(epoch => epoch + 1); };
  useEffect(() => { if (active && (draft?.review || cleanup || breakdown)) reviewHeading.current?.focus(); }, [active, draft?.review, cleanup, breakdown]);
  useEffect(() => {
    setMessage("");
  }, [view]);
  useEffect(() => {
    if (!message) return;
    const timer = setTimeout(() => setMessage(""), 6000);
    return () => clearTimeout(timer);
  }, [message]);
  const closeBreakdown = () => { setBreakdown(false); breakdownTrigger.current?.focus(); };
  const changeView = (next: StorageView) => {
    setCleanup(null);
    setBreakdown(false);
    setMessage("");
    if (onViewChange) onViewChange(next); else setLocalView(next);
  };
  const edit = (next: Partial<StorageDraft>) => {
    if (busyRef.current || policySync.current || draftRef.current?.pending?.kind === "policy") return;
    const current = draftRef.current;
    if (!current) return;
    storeDraft({ ...current, ...next, review: undefined });
    setCleanup(null);
    setMessage("");
  };
  const dirty = Boolean(draft && status && !samePolicy(policyDraft(draft), status.policy));
  const validation: { local?: string; cloud?: string } = {};
  if (draft) {
    const p = policyDraft(draft);
    for (const key of ["local", "cloud"] as const) {
      const n = key === "local" ? p.localBudgetBytes : p.cloudBudgetBytes;
      if (n !== null && (!Number.isSafeInteger(n) || n <= 0)) validation[key] = "Enter a total budget greater than zero, or leave it unset.";
      if (draft.offloadEnabled && n === null) validation[key] = "Set a total budget before enabling automatic offloading.";
    }
    if (p.localBudgetBytes !== null && status?.local.protectedBytes != null && p.localBudgetBytes < status.local.protectedBytes) validation.local = `Protected data needs at least ${formatStorageBytes(status.local.protectedBytes)}.`;
    if (p.cloudBudgetBytes !== null && status?.cloud.usedBytes != null && p.cloudBudgetBytes < status.cloud.usedBytes) validation.cloud = `This folder already uses ${formatStorageBytes(status.cloud.usedBytes)}.`;
  }
  const blocked = busy || policySyncNeeded || draft?.pending?.kind === "policy" || reauthNeeded;
  const mutationBlocked = busy || policySyncNeeded || Boolean(draft?.pending) || reauthNeeded;
  const reviewValid = Boolean(draft?.review && status && draft.review.policyRevision === status.policy.revision && Date.parse(draft.review.expiresAt) > Date.now() && samePolicy(draft.review.draft, policyDraft(draft)));
  const preview = async () => {
    if (!draft || !status || mutationBlocked || Object.keys(validation).length) return;
    busyRef.current = true;
    setBusy(true);
    setError("");
    const owner = draftRef.current;
    try {
      const review = await storage.previewStoragePolicy(policyDraft(draft), status.policy.revision);
      if (live.current && draftRef.current === owner) {
        if (!samePolicy(review.draft, policyDraft(draft))) throw new ApiError("The server review does not match this draft. Review the changes again.", 0, "invalid_response");
        storeDraft({ ...draft, review });
      }
    } catch (e) { if (live.current) handleError(e); }
    finally { if (live.current) { busyRef.current = false; setBusy(false); } }
  };
  const mutate = async (kind: Pending["kind"], action: (id: string) => Promise<storage.StorageOperation | null>) => {
    if (busyRef.current || draftRef.current?.pending || reauthRef.current) return;
    const current = draftRef.current;
    if (!current) return;
    const pending = { requestId: requestId(), kind };
    storeDraft({ ...current, pending });
    busyRef.current = true;
    setBusy(true);
    setError("");
    setMessage("");
    try {
      const operation = await action(pending.requestId);
      if (!live.current) return;
      if (operation && !complete(operation)) accepted.current.add(operation.id);
      const latest = draftRef.current;
      if (latest?.pending?.requestId === pending.requestId) {
        if (kind === "policy") { policySync.current = true; setPolicySyncNeeded(true); }
        storeDraft({ ...latest, pending: undefined, review: kind === "policy" ? undefined : latest.review });
      }
      setSudo(null);
      setCleanup(null);
      setMessage(kind === "policy" ? "Storage policy saved. Space has not necessarily been released." : "The server accepted the operation. Progress updates automatically in Activity.");
    } catch (e) {
      if (!live.current) return;
      if (e instanceof ApiError && e.status >= 400 && e.status < 500 && e.code !== "storage_save_outcome_uncertain") {
        const latest = draftRef.current;
        if (latest) storeDraft({ ...latest, pending: undefined, review: kind === "policy" ? undefined : latest.review });
        handleError(e);
      } else setError("The change was sent, but its result is unknown. Status checks continue automatically; the change will not be repeated.");
      setSudo(null);
    } finally {
      if (live.current) { busyRef.current = false; setBusy(false); }
      // Start observation after the mutation acknowledgement has been classified.
      setReadEpoch(epoch => epoch + 1);
    }
  };
  const perform = async (action: typeof sudo) => {
    if (action === "read") {
      setSudo(null);
      reauthRef.current = false;
      setReauthNeeded(false);
      await refreshRead();
      return;
    }
    if (action === "policy" && draft?.review && reviewValid) {
      const exact = draft.review;
      await mutate("policy", async id => (await storage.saveStoragePolicy(exact, id)).operation);
    } else if (action === "cleanup" && cleanup && Date.parse(cleanup.expiresAt) > Date.now()) {
      const exact = cleanup;
      await mutate("cleanup", async id => await storage.runStorageCleanup(exact, id));
    } else if (action && typeof action === "object") {
      const item = action.item;
      await mutate("retention", async id => {
        const result = await storage.setStorageKeep(item.id, !item.kept, id);
        if (live.current) setRetention(previous => previous && { ...previous, items: previous.items.map(i => i.id === result.item.id ? result.item : i) });
        return result.operation;
      });
    } else { setSudo(null); setError("This review expired or changed. Review it again before saving."); }
  };
  const budgetField = (kind: "local" | "cloud") => {
    if (!draft) return null;
    const local = kind === "local", label = local ? "Total local budget" : "Total cloud budget";
    return <div className="ss-budgets">
      <label className="ss-label" htmlFor={`storage-${kind}-budget`}>{label}</label>
      <div className="ss-inputrow">
        <input id={`storage-${kind}-budget`} type="number" inputMode="decimal" min={1 / factor(local ? draft.localUnit : draft.cloudUnit)} step="any"
          value={local ? draft.localBudget : draft.cloudBudget} placeholder="Not set" disabled={blocked}
          aria-invalid={Boolean(validation[kind])} aria-describedby={`storage-${kind}-help storage-${kind}-error`}
          onChange={e => edit(local ? { localBudget: e.target.value } : { cloudBudget: e.target.value })} />
        <AdminSelect label={`${label} unit`} value={local ? draft.localUnit : draft.cloudUnit} disabled={blocked}
          options={[{ value: "GiB", label: "GiB" }, { value: "TiB", label: "TiB" }]}
          onChange={value => edit(local ? { localUnit: value as Unit } : { cloudUnit: value as Unit })} />
      </div>
      <p className="ss-help" id={`storage-${kind}-help`}>
        {local ? "Includes media, cache, temporary files and essential data." : "Applies to the selected folder, including existing files."} Leave unset to keep the current allocation unrestricted. Binary units: 1 GiB = 1024 MiB; 1 TiB = 1024 GiB.
      </p>
      <p id={`storage-${kind}-error`} className="ss-error" role={validation[kind] ? "alert" : undefined}>{validation[kind]}</p>
    </div>;
  };
  const offload = () => status && draft && <Panel label="Automatic offloading" className="ss-policy">
    <div className="ss-policyhead">
      <div><h2>Automatic offloading</h2><p>{!status.cloud.configured ? "Connect Google Drive to enable automatic placement." : draft.offloadEnabled ? "Keep frequently used video and audio close. Move eligible files to Drive when local space is needed." : "New offloads are paused. Existing cloud titles remain available while Drive is connected."}</p></div>
      <button className="ss-switch" type="button" role="switch" aria-label="Automatic offloading" aria-checked={draft.offloadEnabled}
        disabled={blocked || (!draft.offloadEnabled && (!status.capabilities.offload || !status.cloud.configured))}
        onClick={() => edit({ offloadEnabled: !draft.offloadEnabled })}>
        <span className="ss-track" aria-hidden="true" />{draft.offloadEnabled ? "On" : "Paused"}
      </button>
    </div>
    <div className="ss-ruleline"><span className="ss-state">{draft.offloadEnabled !== status.policy.offloadEnabled ? "Pending save" : humanize(status.offload.state)}</span><span className="ss-rulepill">Start at <em>90%</em></span><span className="ss-rulepill">Free down to <em>80%</em></span></div>
    {status.offload.reason && <p className="ss-help">{errorExplanation(status.offload.reason)}</p>}
    {!status.capabilities.offload && <p className="ss-help">Automatic offloading is unavailable on this server.</p>}
    {[ ["Release cloud-backed local copies", "Only after the matching Drive copy is confirmed available."], ["Offload least recently used media", "Upload video and separate audio, verify the copy, then release local space."] ].map(([title, description], i) => <div className="ss-step" key={title}><span>{i + 1}</span><div><strong>{title}</strong><p>{description}</p></div></div>)}
    <div className="ss-connection"><h3>Always protected</h3><p className="ss-help">Active playback · Keep local items · Subtitles · Artwork · Database</p>
      <label className="ss-check"><input type="checkbox" checked={draft.prefetchNextEpisode} disabled={blocked} onChange={e => edit({ prefetchNextEpisode: e.target.checked })} /><span>Prepare the next episode<small>One episode ahead, only when the local budget allows.</small></span></label>
    </div>
  </Panel>;
  const query = parseAppQuery(window.location.search);
  const localTotal = status?.local.writtenBytes != null && status.local.reservedBytes !== null ? status.local.writtenBytes + status.local.reservedBytes : null;
  const meterCategories = status ? [
    ...status.local.categories,
    ...(status.policy.localBudgetBytes !== null && status.local.reservedBytes !== null ? [{ key: "reserved", label: "Reserved", bytes: status.local.reservedBytes }] : []),
  ] : [];
  return <section className="admin-panel admin-panel--storage"><div className="storage-workspace">
    <header className="ss-heading">
      <div><div className="ss-mono">SERVER / STORAGE</div><h1 ref={heading} tabIndex={-1}>Storage</h1><p>Manage local space and automatic cloud storage.</p></div>
      {view !== "activity" && (dirty || draft?.review) && <div className="ss-actions">
        <Button variant="secondary" disabled={mutationBlocked} onClick={() => { if (status) { storeDraft(initialDraft(status.policy)); setCleanup(null); setMessage("Draft discarded."); } }}>Discard changes</Button>
        {!draft?.review && <Button className="ss-primary ss-button" disabled={mutationBlocked || Object.keys(validation).length > 0} onClick={() => void preview()}>{busy ? "Working…" : "Review changes"}</Button>}
      </div>}
    </header>
    <nav className="ss-tabs" aria-label="Storage views">{STORAGE_VIEWS.map(item => <a key={item}
      href={appUrl(query.profile, "admin", { section: "storage", storageView: item, adminProfile: query.adminProfile })}
      aria-current={view === item ? "page" : undefined} onClick={e => {
        if (e.button === 0 && !e.metaKey && !e.ctrlKey && !e.shiftKey && !e.altKey) { e.preventDefault(); changeView(item); }
      }}>{item[0].toUpperCase() + item.slice(1)}</a>)}</nav>
    {error && <p className="ss-banner" role="alert">{error}</p>}
    {message && <p className="ss-toast" role="status">{message}</p>}
    {reauthNeeded && <Button variant="secondary" onClick={() => setSudo("read")}>Confirm identity</Button>}
    {policySyncNeeded && <p className="ss-banner" role="status">Policy accepted. Waiting for the saved settings to be read; updates continue automatically.</p>}
    {draft?.pending && <div className="ss-banner"><div><strong>Operation awaiting confirmation</strong><p>Its result is not confirmed. Status checks continue automatically while this page is visible. The change will not be repeated.</p></div></div>}
    {!status || !draft ? <Panel title={loading ? "Loading storage…" : "Storage unavailable"}>
      <p className="ss-muted">{loading ? "Contacting the server." : "Storage data could not be loaded."}</p>
      {!loading && !reauthNeeded && <p className="ss-help">Retrying automatically while this page is visible.</p>}
    </Panel> : <div className="ss-stack">
      {draft.review && view !== "activity" && <section className="ss-sheet" aria-label="Review storage changes">
        <div className="ss-sectionhead"><h2 ref={reviewHeading} tabIndex={-1}>Review changes</h2><span className="ss-mono">Server preview</span></div>
        {draft.review.changes.map((change, i) => <div key={i} className="ss-reviewrow"><span>{changeLabel(change.field)}</span><strong>{changeValue(change.field, change.before)} → {changeValue(change.field, change.after)}</strong></div>)}
        <ul className="ss-review-list">{draft.review.effects.map((effect, i) => <li key={i}>{effect}</li>)}</ul>
        <p className="ss-help">Saving does not mean that space has already been reclaimed. Review expires {displayDate(draft.review.expiresAt)}.</p>
        {!reviewValid && <p className="ss-error" role="alert">This review expired or the server policy changed. Review changes again.</p>}
        <div className="ss-sheetfooter"><Button variant="secondary" disabled={blocked} onClick={() => edit({})}>Back to editing</Button><Button className="ss-button ss-primary" disabled={!reviewValid || mutationBlocked} onClick={() => void perform("policy")}>Save changes</Button></div>
      </section>}
      {cleanup && view === "overview" && <section className="ss-sheet" aria-label="Cleanup review">
        <h2 ref={reviewHeading} tabIndex={-1}>Review cleanup</h2><p className="ss-help">Estimated release: {formatStorageBytes(cleanup.estimatedReleaseBytes)}. Playback, kept items, caches and subtitles stay protected.</p>
        {cleanup.steps.map((step, i) => <div className="ss-reviewrow" key={i}><span>{humanize(step.kind)}</span><strong>{formatStorageBytes(step.bytes)}</strong></div>)}
        {cleanup.blocker && <p className="ss-error" role="alert">{errorExplanation(cleanup.blocker)}</p>}
        <div className="ss-sheetfooter"><Button variant="secondary" onClick={() => setCleanup(null)}>Cancel</Button><Button disabled={mutationBlocked || Boolean(cleanup.blocker) || Date.parse(cleanup.expiresAt) <= Date.now()} onClick={() => void perform("cleanup")}>Run cleanup</Button></div>
      </section>}
      {view === "overview" && <>
        <div className="ss-capacity">
          <Panel label="Local storage">
            <div className="ss-panelhead"><h2>Local storage</h2><span className="ss-state">Always active</span></div>
            <div className="ss-usage"><strong>{formatStorageBytes(localTotal)}</strong><span>{status.policy.localBudgetBytes === null ? "Budget not set" : `of ${formatStorageBytes(status.policy.localBudgetBytes)} allocated`}</span></div>
            {status.policy.localBudgetBytes === null ? <p className="ss-help">Written usage distribution · no budget limit set</p> : null}
            <div className="ss-meter" role="img" aria-label={`${status.policy.localBudgetBytes === null ? "Usage distribution" : "Local allocation"}: ${meterCategories.map(c => `${c.label} ${formatStorageBytes(c.bytes)}`).join(", ") || "not measured"}`}>
              {meterCategories.map((c, i) => <span key={c.key} className={c.key === "reserved" ? "ss-segreserved" : segmentClass(i)} style={{ width: `${Math.max(0, Math.min(100, c.bytes / (status.policy.localBudgetBytes ?? (status.local.categories.reduce((sum, category) => sum + category.bytes, 0) || 1)) * 100))}%` }} />)}
            </div>
            <ul className="ss-legend" aria-label="Usage distribution legend">{meterCategories.map((category, i) => <li key={category.key}><span className={category.key === "reserved" ? "ss-segreserved" : segmentClass(i)} aria-hidden="true" />{category.label} · {formatStorageBytes(category.bytes)}</li>)}</ul>
            <div className="ss-meterlabel"><span>{formatStorageBytes(status.local.writtenBytes)} on disk · {formatStorageBytes(status.local.reservedBytes)} reserved</span><button ref={breakdownTrigger} className="ss-link" type="button" aria-expanded={breakdown} onClick={() => breakdown ? closeBreakdown() : setBreakdown(true)}>View breakdown</button></div>
            <div className="ss-hardware"><div><span>Local budget</span><strong>{status.policy.localBudgetBytes === null ? "Not set" : formatStorageBytes(status.policy.localBudgetBytes)}</strong></div><div><span>Free on server disk</span><strong>{formatStorageBytes(status.local.freeBytes)}</strong></div><div><span>Minimum disk reserve</span><strong>{formatStorageBytes(status.local.diskReserveBytes)}</strong></div></div>
            <div className="ss-detailrow"><span>Retained files</span><strong>{formatStorageBytes(status.local.retainedBytes ?? null)}</strong></div>
            <p className="ss-help">Caches, subtitles, artwork and unclassified files are retained and included in disk usage. Capacity pressure pauses new work instead of deleting them.</p>
            <Measurement label="Local" value={status.local} />
            <button className="ss-link" type="button" onClick={() => changeView("policy")}>Edit local budget →</button>
          </Panel>
          <Panel label="Google Drive storage">
            <div className="ss-panelhead"><h2>Google Drive</h2><span className="ss-state">{!status.cloud.configured ? "Not configured" : status.cloud.reachable === true ? "Connected" : status.cloud.reachable === false ? "Unavailable" : "Not checked"}</span></div>
            {status.cloud.configured ? <>
              <div className="ss-usage"><strong>{formatStorageBytes(status.cloud.usedBytes)}</strong><span>{status.policy.cloudBudgetBytes === null ? "Folder budget not set" : `of ${formatStorageBytes(status.policy.cloudBudgetBytes)} folder budget`}</span></div>
              {status.policy.cloudBudgetBytes !== null && <div className="ss-meter" role="img" aria-label={`Drive folder usage: ${formatStorageBytes(status.cloud.usedBytes)}`}><span style={{ width: status.cloud.usedBytes !== null ? `${Math.min(100, status.cloud.usedBytes / status.policy.cloudBudgetBytes * 100)}%` : "0%" }} /></div>}
              <p className="ss-help">{status.cloud.folderName || "Configured folder"}</p>
            </> : <p className="ss-muted ss-small">Add a Drive folder to make automatic offloading available.</p>}
            <div className="ss-hardware"><div><span>Cloud folder budget</span><strong>{status.policy.cloudBudgetBytes === null ? "Not set" : formatStorageBytes(status.policy.cloudBudgetBytes)}</strong></div><div><span>Free in Google account</span><strong>{formatStorageBytes(status.cloud.accountFreeBytes)}</strong></div><div><span>Account capacity</span><strong>{formatStorageBytes(status.cloud.accountTotalBytes)}</strong></div></div>
            <p className="ss-help">Existing Drive caches and subtitles are retained and included in folder usage. Local copies are not automatically mirrored to Drive.</p>
            <Measurement label="Drive" value={status.cloud} />
            <button className="ss-link" type="button" onClick={() => changeView("policy")}>Edit cloud budget →</button>
            <div className="ss-connection"><button className="ss-link" type="button" onClick={() => setConnection(!connection)} aria-expanded={connection}>{connection ? "Hide connection details" : status.cloud.configured ? "Connection details" : "Connect Google Drive"}</button></div>
          </Panel>
        </div>
        {breakdown && <section className="ss-sheet" aria-label="Local allocation" onKeyDown={e => { if (e.key === "Escape") { e.stopPropagation(); closeBreakdown(); } }}>
          <div className="ss-sectionhead"><h2 ref={reviewHeading} tabIndex={-1}>Local allocation</h2><Button variant="ghost" onClick={closeBreakdown}>Close</Button></div>
          {status.local.categories.length ? status.local.categories.map(c => <div className="ss-reviewrow" key={c.key}><span>{c.label}</span><strong>{formatStorageBytes(c.bytes)}</strong></div>) : <p className="ss-muted">Allocation has not been measured.</p>}
          <p className="ss-help">Essential data and reservations are included in the total budget. Disk free space is measured separately.</p>
        </section>}
        {connection && active && <DriveSetupCard settings={settings} onSettingsRefresh={refreshRead} initiallyExpanded={!status.cloud.configured} />}
        <Panel title="Placement policy">
          <div className="ss-detailrow"><span>Automatic offloading</span><strong>{status.policy.offloadEnabled ? `Enabled · ${humanize(status.offload.state)}` : "Paused"}</strong></div>
          <div className="ss-detailrow"><span>Next episode preparation</span><strong>{status.policy.prefetchNextEpisode ? "Enabled" : "Disabled"}</strong></div>
          <div className="ss-detailrow"><span>HEVC compression</span><strong>{changeValue("hevc_compression_mode", status.policy.hevcCompressionMode)}</strong></div>
          {status.offload.reason && <p className="ss-help">{errorExplanation(status.offload.reason)}</p>}
          <button className="ss-link" type="button" onClick={() => changeView("policy")}>Edit placement policy →</button>
        </Panel>
        <section aria-label="Local retention">
          <div className="ss-sectionhead"><div><h3>Local retention</h3><p>Playback and explicit keeps take priority.</p></div></div>
          {retention?.items.map(item => <article className="ss-file" key={item.id}>
            <span className="ss-fileicon" aria-hidden="true">{item.kind === "episode" ? "▤" : "▷"}</span>
            <div><strong>{item.title}</strong><p>{formatStorageBytes(item.bytes)} · {humanize(item.localState)} · {item.offlineReady ? "Ready offline on this server" : item.kept ? "Keep requested · not ready offline" : "Not kept"}</p>{item.protectedReason && <p>{humanize(item.protectedReason)}</p>}</div>
            <div className="ss-actions"><Button variant="secondary" aria-pressed={item.kept} disabled={mutationBlocked || !status.capabilities.keepLocal} onClick={() => void perform({ item })}>{item.kept ? "Remove keep" : "Keep local"}</Button></div>
          </article>)}
          {retention && retention.items.length === 0 && <p className="ss-empty">No media is available for local retention.</p>}
          {!status.capabilities.keepLocal && <p className="ss-help">Keep local is unavailable on this server.</p>}
          {retention?.nextCursor && <Button variant="secondary" disabled={pageBusy} onClick={() => void loadPage("retention", retention.nextCursor!).catch(() => undefined)}>Load more media</Button>}
        </section>
        <Panel title="Storage tools">
          <Button variant="secondary" disabled={mutationBlocked || !status.capabilities.cleanup || dirty} onClick={async () => {
            if (busyRef.current) return;
            busyRef.current = true; setBusy(true);
            try { const plan = await storage.previewStorageCleanup(); if (live.current) setCleanup(plan); }
            catch (e) { if (live.current) handleError(e); }
            finally { if (live.current) { busyRef.current = false; setBusy(false); } }
          }}>Review cleanup</Button>
          {!status.capabilities.cleanup && <p className="ss-help">Manual cleanup is unavailable on this server.</p>}
          {dirty && <p className="ss-help">Save or discard policy changes before reviewing cleanup.</p>}
          <p className="ss-help">Usage and background progress update automatically while this page is visible.</p>
        </Panel>
      </>}
      {view === "policy" && <>
        <Panel title="Total storage budgets"><p className="ss-help">Caches and subtitles remain protected on this server and in Drive. Only eligible video and audio originals can be offloaded. If retained files fill the budget, new work waits for capacity.</p><div className="ss-formgrid ss-budgetside">{budgetField("local")}{budgetField("cloud")}</div></Panel>
        {offload()}
        <Panel title="Media processing">
          <p className="ss-muted ss-small">Compression is managed separately from storage placement.</p>
          <div className="ss-formgrid"><div className="admin-control">
            <AdminSelect label="HEVC compression" aria-describedby="hevc-mode-help" value={draft.hevcCompressionMode} disabled={blocked}
              onChange={value => edit({ hevcCompressionMode: value as StorageDraft["hevcCompressionMode"] })}
              options={[{ value: "auto", label: "Automatic" }, { value: "on", label: "Always enabled" }, { value: "off", label: "Disabled" }]} />
            <p className="ss-help" id="hevc-mode-help">{draft.hevcCompressionMode === "off" ? "Background HEVC compression is disabled." : draft.hevcCompressionMode === "on" ? "Compress eligible local videos during idle time, including on lower-capacity servers." : "Compress eligible local videos when idle and CPU capacity is sufficient."}</p>
          </div></div>
          <AdminHelp label="HEVC compatibility and processing"><p>Automatic enables background HEVC compression when the server is idle and has enough CPU capacity. Always enabled also permits it on lower-capacity servers, while still waiting for idle time. Disabled stops background compression. Eligible existing local videos can also be converted.</p><p>HEVC can reduce file size but takes processing time and may be less compatible with older devices.</p></AdminHelp>
        </Panel>
      </>}
      {view === "activity" && <section aria-label="Storage activity">
        <div className="ss-sectionhead"><div><h2>Storage activity</h2><p>Uploads are verified before local space is released. Progress updates automatically.</p></div></div>
        {activity?.items.map(item => <OperationRow key={item.id} item={item} />)}
        {activity && activity.items.length === 0 && <p className="ss-empty">No storage activity has been recorded.</p>}
        {activity?.nextCursor && <Button variant="secondary" disabled={pageBusy} onClick={() => void loadPage("activity", activity.nextCursor!).catch(() => undefined)}>Load more activity</Button>}
      </section>}
      {pageBusy && <p role="status" className="ss-busy">Loading {view === "activity" ? "activity" : "media"}…</p>}
      {pageError && view !== "policy" && <div><p role="alert" className="ss-error">{pageError}</p><p className="ss-help">Last loaded records remain visible. Retrying automatically.</p></div>}
    </div>}
    <footer className="ss-footnote"><span>{dirty ? "Unsaved changes. Kept while you browse admin sections; leaving Admin Center discards them." : !status ? "Storage status unavailable" : "Saved policy shown. Measurements have their own update times."}</span><span>Local storage always stays active.</span></footer>
    {active && <SudoModal isOpen={sudo !== null} actionLabel={sudo === "read" ? "Read storage status" : sudo === "policy" ? "Save storage policy" : sudo === "cleanup" ? "Run storage cleanup" : "Change local retention"} onCancel={() => setSudo(null)} onSuccess={() => perform(sudo)} />}
  </div></section>;
}
