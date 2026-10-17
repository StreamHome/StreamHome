import React, { useCallback, useEffect, useRef, useState } from "react";
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

type Unit = "GB" | "TB";
type Pending = { requestId: string; operationId?: string; kind: "policy" | "retention" | "cleanup" | "refresh" };
export interface StorageDraft {
  localBudget: string; localUnit: Unit; cloudBudget: string; cloudUnit: Unit;
  offloadEnabled: boolean; prefetchNextEpisode: boolean; hevcCompressionMode: storage.StoragePolicyDraft["hevcCompressionMode"];
  revision: number; review?: storage.StorageReview; pending?: Pending;
}
const factor = (unit: Unit) => unit === "TB" ? 1e12 : 1e9;
export const formatStorageBytes = (bytes: number | null) => bytes === null ? "Not measured" : bytes >= 1e12 ? `${new Intl.NumberFormat(undefined, { maximumFractionDigits: 2 }).format(bytes / 1e12)} TB` : bytes >= 1e9 ? `${new Intl.NumberFormat(undefined, { maximumFractionDigits: 2 }).format(bytes / 1e9)} GB` : bytes >= 1e6 ? `${new Intl.NumberFormat(undefined, { maximumFractionDigits: 1 }).format(bytes / 1e6)} MB` : `${new Intl.NumberFormat().format(bytes)} B`;
const budgetInput = (bytes: number | null) => bytes === null ? { value: "", unit: "GB" as Unit } : bytes >= 1e12 && bytes % 1e12 === 0 ? { value: String(bytes / 1e12), unit: "TB" as Unit } : { value: String(bytes / 1e9), unit: "GB" as Unit };
const initialDraft = (p: storage.StoragePolicy): StorageDraft => { const l = budgetInput(p.localBudgetBytes), c = budgetInput(p.cloudBudgetBytes); return { localBudget: l.value, localUnit: l.unit, cloudBudget: c.value, cloudUnit: c.unit, offloadEnabled: p.offloadEnabled, prefetchNextEpisode: p.prefetchNextEpisode, hevcCompressionMode: p.hevcCompressionMode, revision: p.revision }; };
const bytes = (value: string, unit: Unit) => value.trim() === "" ? null : Math.round(Number(value) * factor(unit));
const policyDraft = (d: StorageDraft): storage.StoragePolicyDraft => ({ localBudgetBytes: bytes(d.localBudget, d.localUnit), cloudBudgetBytes: bytes(d.cloudBudget, d.cloudUnit), offloadEnabled: d.offloadEnabled, prefetchNextEpisode: d.prefetchNextEpisode, hevcCompressionMode: d.hevcCompressionMode });
const samePolicy = (a: storage.StoragePolicyDraft, b: storage.StoragePolicyDraft) => JSON.stringify(storage.storageDraftToWire(a)) === JSON.stringify(storage.storageDraftToWire(b));
const messageFor = (e: unknown) => e instanceof Error ? e.message : "Storage is unavailable. Check status before continuing.";
const humanize = (s: string) => s.replace(/_/g, " ");
const changeLabel = (field: string) => ({ local_budget_bytes: "Local budget", cloud_budget_bytes: "Cloud folder budget", offload_enabled: "Automatic offloading", prefetch_next_episode: "Next episode preparation", hevc_compression_mode: "HEVC compression" }[field] ?? humanize(field));
const changeValue = (field: string, value: unknown) => field.includes("budget") ? value === null ? "Not set" : typeof value === "number" ? formatStorageBytes(value) : "Unavailable" : typeof value === "boolean" ? value ? "Enabled" : "Paused" : field === "hevc_compression_mode" ? ({ auto: "Automatic", on: "Always enabled", off: "Disabled" }[String(value)] ?? String(value)) : String(value);
const displayDate = (s: string | null) => s && Number.isFinite(Date.parse(s)) ? new Date(s).toLocaleString() : "Not measured";
const complete = (o: storage.StorageOperation) => ["completed", "succeeded", "saved", "failed", "cancelled", "blocked"].includes(o.status);
const requestId = () => crypto.randomUUID();

function Panel({ title, children, className = "", label }: { title?: string; children: React.ReactNode; className?: string; label?: string }) {
  return <GlassPane as="section" className={`ss-panel ${className}`} spotlight={false} aria-label={label ?? title}>{title && <div className="ss-panelhead"><h2>{title}</h2></div>}{children}</GlassPane>;
}
function OperationRow({ item }: { item: storage.StorageOperation }) {
  return <article className="ss-activity"><span aria-hidden="true">↗</span><div><strong>{item.title || humanize(item.kind)} · {humanize(item.status)}</strong><p>{item.message || humanize(item.phase)}{item.bytesDone !== null ? ` · ${formatStorageBytes(item.bytesDone)}${item.bytesTotal !== null ? ` of ${formatStorageBytes(item.bytesTotal)}` : ""}` : ""}{item.releasedBytes > 0 ? ` · ${formatStorageBytes(item.releasedBytes)} released` : ""}</p>{item.errorCode && <p className="ss-danger">{humanize(item.errorCode)}</p>}</div><time dateTime={item.updatedAt}>{displayDate(item.updatedAt)}</time></article>;
}

export function StoragePanel({ retainedDraft, onDraftChange, view: controlledView, onViewChange }: { retainedDraft?: StorageDraft; onDraftChange?: (draft: StorageDraft | undefined) => void; view?: StorageView; onViewChange?: (view: StorageView) => void } = {}) {
  const [status, setStatus] = useState<storage.StorageStatus | null>(null);
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
  const [sudo, setSudo] = useState<"read" | "policy" | "cleanup" | { item: storage.RetentionItem } | null>(null);
  const [cleanup, setCleanup] = useState<storage.CleanupReview | null>(null);
  const [breakdown, setBreakdown] = useState(false);
  const [connection, setConnection] = useState(false);
  const [retention, setRetention] = useState<storage.StoragePage<storage.RetentionItem> | null>(null);
  const [activity, setActivity] = useState<storage.StoragePage<storage.StorageOperation> | null>(null);
  const [pageBusy, setPageBusy] = useState(false);
  const [pageError, setPageError] = useState("");
  const live = useRef(false);
  const reads = useRef(0);
  const pages = useRef(0);
  const heading = useRef<HTMLHeadingElement>(null);
  const reviewHeading = useRef<HTMLHeadingElement>(null);
  const storeDraft = (next: StorageDraft | undefined) => { draftRef.current = next; setDraft(next); onDraftChange?.(next); };
  const handleError = useCallback((e: unknown) => { setError(messageFor(e)); if (e instanceof ApiError && e.code === "reauthentication_required") setReauthNeeded(true); }, []);
  const load = useCallback(async () => {
    const read = ++reads.current;
    setLoading(true); setError("");
    try {
      const [next, system] = await Promise.all([storage.getStorageStatus(), getSettings().catch(() => null)]);
      if (!live.current || reads.current !== read) return;
      setStatus(next); setSettings(system); setReauthNeeded(false);
      if (!draftRef.current) { const initial = initialDraft(next.policy); draftRef.current = initial; setDraft(initial); }
    } catch (e) { if (live.current && reads.current === read) handleError(e); }
    finally { if (live.current && reads.current === read) setLoading(false); }
  }, [handleError]);
  useEffect(() => { live.current = true; void load(); return () => { live.current = false; reads.current += 1; pages.current += 1; }; }, [load]);
  const loadPage = useCallback(async (kind: "retention" | "activity", cursor?: string) => {
    const read = ++pages.current;
    setPageBusy(true); setPageError("");
    try {
      if (kind === "retention") { const result = await storage.getStorageRetention(cursor); if (live.current && read === pages.current) setRetention(previous => ({ ...result, items: cursor ? [...(previous?.items ?? []), ...result.items] : result.items })); }
      else { const result = await storage.getStorageActivity(cursor); if (live.current && read === pages.current) setActivity(previous => ({ ...result, items: cursor ? [...(previous?.items ?? []), ...result.items] : result.items })); }
    } catch (e) { if (live.current && read === pages.current) { setPageError(messageFor(e)); if (e instanceof ApiError && e.code === "reauthentication_required") setReauthNeeded(true); } }
    finally { if (live.current && read === pages.current) setPageBusy(false); }
  }, []);
  useEffect(() => { if (status && view !== "policy") void loadPage(view === "activity" ? "activity" : "retention"); return () => { pages.current += 1; }; }, [Boolean(status), view, loadPage]);
  useEffect(() => { if (draft?.review || cleanup || breakdown) reviewHeading.current?.focus(); }, [draft?.review, cleanup, breakdown]);
  const changeView = (next: StorageView) => { setCleanup(null); setBreakdown(false); if (onViewChange) onViewChange(next); else setLocalView(next); };
  const edit = (next: Partial<StorageDraft>) => { if (busyRef.current || draftRef.current?.pending) return; const current = draftRef.current; if (!current) return; storeDraft({ ...current, ...next, review: undefined }); setCleanup(null); setMessage(""); };
  const dirty = Boolean(draft && status && !samePolicy(policyDraft(draft), status.policy));
  const validation: { local?: string; cloud?: string } = {};
  if (draft) {
    const p = policyDraft(draft);
    for (const key of ["local", "cloud"] as const) { const n = key === "local" ? p.localBudgetBytes : p.cloudBudgetBytes; if (n !== null && (!Number.isSafeInteger(n) || n <= 0)) validation[key] = "Enter a total budget greater than zero, or leave it unset."; if (draft.offloadEnabled && n === null) validation[key] = "Set a total budget before enabling automatic offloading."; }
    if (p.localBudgetBytes !== null && status?.local.protectedBytes !== null && status?.local.protectedBytes !== undefined && p.localBudgetBytes < status.local.protectedBytes) validation.local = `Protected data needs at least ${formatStorageBytes(status.local.protectedBytes)}.`;
    if (p.cloudBudgetBytes !== null && status?.cloud.usedBytes !== null && status?.cloud.usedBytes !== undefined && p.cloudBudgetBytes < status.cloud.usedBytes) validation.cloud = `This folder already uses ${formatStorageBytes(status.cloud.usedBytes)}.`;
  }
  const blocked = busy || Boolean(draft?.pending) || reauthNeeded;
  const reviewValid = Boolean(draft?.review && status && draft.review.policyRevision === status.policy.revision && Date.parse(draft.review.expiresAt) > Date.now() && samePolicy(draft.review.draft, policyDraft(draft)));
  const preview = async () => {
    if (!draft || !status || blocked || Object.keys(validation).length) return;
    busyRef.current = true; setBusy(true); setError("");
    const owner = draftRef.current;
    try { const review = await storage.previewStoragePolicy(policyDraft(draft), status.policy.revision); if (live.current && draftRef.current === owner) { if (!samePolicy(review.draft, policyDraft(draft))) throw new ApiError("The server review does not match this draft. Review the changes again.", 0, "invalid_response"); storeDraft({ ...draft, review }); } }
    catch (e) { if (live.current) handleError(e); }
    finally { if (live.current) { busyRef.current = false; setBusy(false); } }
  };
  const mutate = async (kind: Pending["kind"], action: (id: string) => Promise<storage.StorageOperation | null>) => {
    if (busyRef.current || draftRef.current?.pending) return;
    const current = draftRef.current;
    if (!current) return;
    const pending = { requestId: requestId(), kind };
    storeDraft({ ...current, pending }); busyRef.current = true; setBusy(true); setError(""); setMessage("");
    try { const operation = await action(pending.requestId); if (!live.current) return; const latest = draftRef.current; if (latest?.pending?.requestId === pending.requestId) storeDraft(kind === "policy" ? undefined : { ...latest, pending: operation && !complete(operation) ? { ...pending, operationId: operation.id } : undefined }); setSudo(null); setCleanup(null); setMessage(kind === "policy" ? "Storage policy saved. Space has not necessarily been released." : "The server accepted the operation. Check status for progress."); await load(); }
    catch (e) {
      if (!live.current) return;
      if (e instanceof ApiError && e.status >= 400 && e.status < 500 && e.code !== "storage_save_outcome_uncertain") { const latest = draftRef.current; if (latest) storeDraft({ ...latest, pending: undefined, review: kind === "policy" ? undefined : latest.review }); handleError(e); }
      else setError("The change was sent, but its result is unknown. Check status before continuing.");
      setSudo(null);
    } finally { if (live.current) { busyRef.current = false; setBusy(false); } }
  };
  const checkOperation = async () => {
    const pending = draftRef.current?.pending;
    if (!pending || busyRef.current) return;
    busyRef.current = true; setBusy(true); setError("");
    try { const op = await storage.getStorageOperation(pending.operationId ?? pending.requestId); if (!live.current) return; setMessage(`${humanize(op.kind)} · ${humanize(op.status)}. ${op.message ?? humanize(op.phase)}`); if (complete(op)) { const current = draftRef.current; if (current) storeDraft(pending.kind === "policy" && !["failed", "cancelled", "blocked"].includes(op.status) ? undefined : { ...current, pending: undefined, review: undefined }); await load(); if (view !== "policy") await loadPage(view === "activity" ? "activity" : "retention"); } }
    catch (e) { if (live.current) handleError(e); }
    finally { if (live.current) { busyRef.current = false; setBusy(false); } }
  };
  const perform = async (action: typeof sudo) => {
    if (action === "read") { setSudo(null); await load(); return; }
    if (action === "policy" && draft?.review && reviewValid) { const exact = draft.review; await mutate("policy", async id => { return (await storage.saveStoragePolicy(exact, id)).operation; }); }
    else if (action === "cleanup" && cleanup && Date.parse(cleanup.expiresAt) > Date.now()) { const exact = cleanup; await mutate("cleanup", async id => { return await storage.runStorageCleanup(exact, id); }); }
    else if (action && typeof action === "object") { const item = action.item; await mutate("retention", async id => { const result = await storage.setStorageKeep(item.id, !item.kept, id); if (live.current) setRetention(previous => previous && { ...previous, items: previous.items.map(i => i.id === result.item.id ? result.item : i) }); return result.operation; }); }
    else { setSudo(null); setError("This review expired or changed. Review it again before saving."); }
  };
  const budgetField = (kind: "local" | "cloud") => {
    if (!draft) return null;
    const local = kind === "local", label = local ? "Total local budget" : "Total cloud budget";
    return <div className="ss-budgets"><label className="ss-label" htmlFor={`storage-${kind}-budget`}>{label}</label><div className="ss-inputrow"><input id={`storage-${kind}-budget`} type="number" inputMode="decimal" min="0.000000001" step="any" value={local ? draft.localBudget : draft.cloudBudget} placeholder="Not set" disabled={blocked} aria-invalid={Boolean(validation[kind])} aria-describedby={`storage-${kind}-help storage-${kind}-error`} onChange={e => edit(local ? { localBudget: e.target.value } : { cloudBudget: e.target.value })} /><AdminSelect label={`${label} unit`} value={local ? draft.localUnit : draft.cloudUnit} disabled={blocked} options={[{ value: "GB", label: "GB" }, { value: "TB", label: "TB" }]} onChange={value => edit(local ? { localUnit: value as Unit } : { cloudUnit: value as Unit })} /></div><p className="ss-help" id={`storage-${kind}-help`}>{local ? "Includes media, cache, temporary files and essential data." : "Applies to the selected folder, including existing files."} Leave unset to keep the current allocation unrestricted.</p><p id={`storage-${kind}-error`} className="ss-error" role={validation[kind] ? "alert" : undefined}>{validation[kind]}</p></div>;
  };
  const offload = (expanded: boolean) => status && draft && <Panel label="Automatic offloading" className="ss-policy"><div className="ss-policyhead"><div><h2>Automatic offloading</h2><p>{!status.cloud.configured ? "Connect Google Drive to enable automatic placement." : draft.offloadEnabled ? "Keep frequently used video and audio close. Move eligible files to Drive when local space is needed." : "New offloads are paused. Existing cloud titles remain available while Drive is connected."}</p></div><button className="ss-switch" type="button" role="switch" aria-label="Automatic offloading" aria-checked={draft.offloadEnabled} disabled={blocked || (!draft.offloadEnabled && (!status.capabilities.offload || !status.cloud.configured))} onClick={() => edit({ offloadEnabled: !draft.offloadEnabled })}><span className="ss-track" aria-hidden="true" />{draft.offloadEnabled ? "On" : "Paused"}</button></div><div className="ss-ruleline"><span className="ss-state">{draft.offloadEnabled !== status.policy.offloadEnabled ? "Pending save" : humanize(status.offload.state)}</span><span className="ss-rulepill">Start at <em>90%</em></span><span className="ss-rulepill">Free down to <em>80%</em></span></div>{status.offload.reason && <p className="ss-help">{humanize(status.offload.reason)}</p>}{!status.capabilities.offload && <p className="ss-help">Automatic offloading is unavailable on this server.</p>}{expanded ? <><div className="ss-step"><span>1</span><div><strong>Clear idle playback cache</strong><p>Remove temporary playback data that can be prepared again.</p></div></div><div className="ss-step"><span>2</span><div><strong>Release cloud-backed local copies</strong><p>Only after the matching Drive copy is confirmed available.</p></div></div><div className="ss-step"><span>3</span><div><strong>Offload least recently used media</strong><p>Upload video and separate audio, verify the copy, then release local space.</p></div></div><div className="ss-connection"><h3>Always protected</h3><p className="ss-help">Active playback · Keep local items · Subtitles · Artwork · Database</p><label className="ss-check"><input type="checkbox" checked={draft.prefetchNextEpisode} disabled={blocked} onChange={e => edit({ prefetchNextEpisode: e.target.checked })} /><span>Prepare the next episode<small>One episode ahead, only when the local budget allows.</small></span></label></div></> : <button className="ss-link" type="button" onClick={() => changeView("policy")}>View placement rules →</button>}</Panel>;
  const query = parseAppQuery(window.location.search);
  return <section className="admin-panel admin-panel--storage"><div className="storage-workspace">
    <header className="ss-heading"><div><div className="ss-mono">SERVER / STORAGE</div><h1 ref={heading} tabIndex={-1}>Storage</h1><p>Manage local space and automatic cloud storage.</p></div><div className="ss-actions"><Button variant="secondary" disabled={!dirty || blocked} onClick={() => { if (status) { storeDraft(initialDraft(status.policy)); setCleanup(null); setMessage("Draft discarded."); } }}>Discard changes</Button><Button className="ss-primary ss-button" disabled={!dirty || blocked || Object.keys(validation).length > 0} onClick={() => void preview()}>{busy ? "Working…" : "Review changes"}</Button></div></header>
    <nav className="ss-tabs" aria-label="Storage views">{STORAGE_VIEWS.map(item => <a key={item} href={appUrl(query.profile, "admin", { section: "storage", storageView: item, adminProfile: query.adminProfile })} aria-current={view === item ? "page" : undefined} onClick={e => { if (e.button === 0 && !e.metaKey && !e.ctrlKey && !e.shiftKey && !e.altKey) { e.preventDefault(); changeView(item); } }}>{item[0].toUpperCase() + item.slice(1)}</a>)}</nav>
    {error && <p className="ss-banner" role="alert">{error}</p>}{message && <p className="ss-banner" role="status">{message}</p>}
    {reauthNeeded && <Button variant="secondary" onClick={() => setSudo("read")}>Confirm identity</Button>}
    {draft?.pending && <div className="ss-banner"><div><strong>Operation awaiting confirmation</strong><p>Its result is not confirmed. Checking status does not repeat the change.</p></div><Button variant="secondary" disabled={busy} onClick={() => void checkOperation()}>Check status</Button></div>}
    {!status || !draft ? <Panel title={loading ? "Loading storage…" : "Storage unavailable"}><p className="ss-muted">{loading ? "Contacting the server." : "Storage data could not be loaded."}</p>{!loading && <Button variant="secondary" onClick={() => void load()}>Try again</Button>}</Panel> : <div className="ss-stack">
      {draft.review && <section className="ss-sheet" aria-label="Review storage changes"><div className="ss-sectionhead"><h2 ref={reviewHeading} tabIndex={-1}>Review changes</h2><span className="ss-mono">Server preview</span></div>{draft.review.changes.map((change, i) => <div key={i} className="ss-reviewrow"><span>{changeLabel(change.field)}</span><strong>{changeValue(change.field, change.before)} → {changeValue(change.field, change.after)}</strong></div>)}<ul className="ss-review-list">{draft.review.effects.map((effect, i) => <li key={i}>{effect}</li>)}</ul><p className="ss-help">Saving does not mean that space has already been reclaimed. Review expires {displayDate(draft.review.expiresAt)}.</p>{!reviewValid && <p className="ss-error" role="alert">This review expired or the server policy changed. Review changes again.</p>}<div className="ss-sheetfooter"><Button variant="secondary" disabled={blocked} onClick={() => edit({})}>Back to editing</Button><Button className="ss-button ss-primary" disabled={!reviewValid || blocked} onClick={() => void perform("policy")}>Save changes</Button></div></section>}
      {cleanup && <section className="ss-sheet" aria-label="Cleanup review"><h2 ref={reviewHeading} tabIndex={-1}>Review cleanup</h2><p className="ss-help">Estimated release: {formatStorageBytes(cleanup.estimatedReleaseBytes)}. Playback and kept items stay protected.</p>{cleanup.steps.map((step, i) => <div className="ss-reviewrow" key={i}><span>{humanize(step.kind)}</span><strong>{formatStorageBytes(step.bytes)}</strong></div>)}{cleanup.blocker && <p className="ss-error" role="alert">{humanize(cleanup.blocker)}</p>}<div className="ss-sheetfooter"><Button variant="secondary" onClick={() => setCleanup(null)}>Cancel</Button><Button disabled={blocked || Boolean(cleanup.blocker) || Date.parse(cleanup.expiresAt) <= Date.now()} onClick={() => void perform("cleanup")}>Run cleanup</Button></div></section>}
      {status.cloud.configured && (status.cloud.reachable === false || status.cloud.stale || status.cloud.errorCode) && <div className="ss-banner"><div><strong>{status.cloud.reachable === false ? "Drive unavailable" : "Drive usage needs refreshing"}</strong><p>Local originals stay protected. Cloud values may be last known measurements.</p></div></div>}
      {view === "overview" && <><div className="ss-capacity"><Panel label="Local storage"><div className="ss-panelhead"><h2>Local storage</h2><span className="ss-state">Always active</span></div><div className="ss-usage"><strong>{formatStorageBytes(status.local.writtenBytes === null || status.local.reservedBytes === null ? null : status.local.writtenBytes + status.local.reservedBytes)}</strong><span>of {status.policy.localBudgetBytes === null ? "an unset budget" : `${formatStorageBytes(status.policy.localBudgetBytes)} allocated`}</span></div><div className="ss-meter" role="img" aria-label={`Local allocation: ${status.local.categories.map(c => `${c.label} ${formatStorageBytes(c.bytes)}`).join(", ") || "not measured"}`}>{status.local.categories.map((c, i) => <span key={c.key} className={["", "ss-segcache", "ss-segcore", "ss-segreserved"][Math.min(i, 3)]} style={{ width: `${Math.max(0, Math.min(100, c.bytes / (status.policy.localBudgetBytes || (status.local.writtenBytes ?? 0) + (status.local.reservedBytes ?? 0) || 1) * 100))}%` }} />)}</div><div className="ss-meterlabel"><span>{formatStorageBytes(status.local.writtenBytes)} on disk · {formatStorageBytes(status.local.reservedBytes)} reserved</span><button className="ss-link" type="button" onClick={() => setBreakdown(!breakdown)}>View breakdown</button></div><div className="ss-budgetside">{budgetField("local")}<div className="ss-hardware"><div><span>Free on server disk</span><strong>{formatStorageBytes(status.local.freeBytes)}</strong></div><div><span>Minimum disk reserve</span><strong>{formatStorageBytes(status.local.diskReserveBytes)}</strong></div><div><span>{status.local.stale ? "Last known measurement" : "Measured"}</span><strong>{displayDate(status.local.measuredAt)}</strong></div></div></div></Panel><Panel label="Google Drive storage"><div className="ss-panelhead"><h2>Google Drive</h2><span className="ss-state">{!status.cloud.configured ? "Not configured" : status.cloud.reachable === true ? "Connected" : status.cloud.reachable === false ? "Unavailable" : "Not checked"}</span></div>{status.cloud.configured ? <><div className="ss-usage"><strong>{formatStorageBytes(status.cloud.usedBytes)}</strong><span>of {status.policy.cloudBudgetBytes === null ? "an unset folder budget" : `${formatStorageBytes(status.policy.cloudBudgetBytes)} folder budget`}</span></div><div className="ss-meter" role="img" aria-label={`Drive folder usage: ${formatStorageBytes(status.cloud.usedBytes)}`}><span style={{ width: status.cloud.usedBytes !== null && status.policy.cloudBudgetBytes ? `${Math.min(100, status.cloud.usedBytes / status.policy.cloudBudgetBytes * 100)}%` : "0%" }} /></div><div className="ss-meterlabel"><span>{status.cloud.folderName || "Configured folder"}</span><span>{status.cloud.stale ? "Last known usage" : "Folder usage"}</span></div></> : <p className="ss-muted ss-small">Add a Drive folder to make automatic offloading available.</p>}<div className="ss-budgetside">{budgetField("cloud")}<div className="ss-hardware"><div><span>Free in Google account</span><strong>{formatStorageBytes(status.cloud.accountFreeBytes)}</strong></div><div><span>Account capacity</span><strong>{formatStorageBytes(status.cloud.accountTotalBytes)}</strong></div><div><span>{status.cloud.stale ? "Last known measurement" : "Measured"}</span><strong>{displayDate(status.cloud.measuredAt)}</strong></div></div></div><div className="ss-connection"><div className="ss-actions">{status.cloud.configured && <Button variant="ghost" disabled={blocked} onClick={() => void mutate("refresh", async id => await storage.refreshStorage(id))}>Check connection</Button>}<button className="ss-link" type="button" onClick={() => setConnection(!connection)} aria-expanded={connection}>{connection ? "Hide connection details" : status.cloud.configured ? "Connection details" : "Connect Google Drive"}</button></div></div></Panel></div>
      {breakdown && <section className="ss-sheet" aria-label="Local allocation"><div className="ss-sectionhead"><h2 ref={reviewHeading} tabIndex={-1}>Local allocation</h2><Button variant="ghost" onClick={() => setBreakdown(false)}>Close</Button></div>{status.local.categories.length ? status.local.categories.map(c => <div className="ss-reviewrow" key={c.key}><span>{c.label}</span><strong>{formatStorageBytes(c.bytes)}</strong></div>) : <p className="ss-muted">Allocation has not been measured.</p>}<p className="ss-help">Essential data and reservations are included in the total budget. Disk free space is measured separately.</p></section>}
      {connection && <DriveSetupCard settings={settings} onSettingsRefresh={load} initiallyExpanded={!status.cloud.configured} />}{offload(false)}
      <section aria-label="Local retention"><div className="ss-sectionhead"><div><h3>Local retention</h3><p>Playback and explicit keeps take priority.</p></div></div>{retention?.items.map(item => <article className="ss-file" key={item.id}><span className="ss-fileicon" aria-hidden="true">{item.kind === "episode" ? "▤" : "▷"}</span><div><strong>{item.title}</strong><p>{formatStorageBytes(item.bytes)} · {humanize(item.localState)} · {item.offlineReady ? "Ready offline on this server" : item.kept ? "Keep requested · not ready offline" : "Not kept"}</p>{item.protectedReason && <p>{humanize(item.protectedReason)}</p>}</div><div className="ss-actions"><Button variant="secondary" aria-pressed={item.kept} disabled={blocked || !status.capabilities.keepLocal} onClick={() => void perform({ item })}>{item.kept ? "Remove keep" : "Keep local"}</Button></div></article>)}{retention && retention.items.length === 0 && <p className="ss-empty">No media is available for local retention.</p>}{!status.capabilities.keepLocal && <p className="ss-help">Keep local is unavailable on this server.</p>}{retention?.nextCursor && <Button variant="secondary" disabled={pageBusy} onClick={() => void loadPage("retention", retention.nextCursor!)}>Load more media</Button>}</section><Panel title="Storage tools"><div className="ss-actions"><Button variant="secondary" disabled={blocked} onClick={() => void mutate("refresh", async id => { return await storage.refreshStorage(id); })}>Refresh usage</Button><Button variant="secondary" disabled={blocked || !status.capabilities.cleanup || dirty} onClick={async () => { if (busyRef.current) return; busyRef.current = true; setBusy(true); try { const plan = await storage.previewStorageCleanup(); if (live.current) setCleanup(plan); } catch (e) { if (live.current) handleError(e); } finally { if (live.current) { busyRef.current = false; setBusy(false); } } }}>Review cleanup</Button></div>{!status.capabilities.cleanup && <p className="ss-help">Manual cleanup is unavailable on this server.</p>}{dirty && <p className="ss-help">Save or discard policy changes before reviewing cleanup.</p>}<p className="ss-help">Usage refresh measures current storage; it does not change your policy.</p></Panel></>}
      {view === "policy" && <>{offload(true)}<Panel title="Media processing"><p className="ss-muted ss-small">Compression is managed separately from storage placement.</p><div className="ss-formgrid"><div className="admin-control"><AdminSelect label="HEVC compression" aria-describedby="hevc-mode-help" value={draft.hevcCompressionMode} disabled={blocked} onChange={value => edit({ hevcCompressionMode: value as StorageDraft["hevcCompressionMode"] })} options={[{ value: "auto", label: "Automatic" }, { value: "on", label: "Always enabled" }, { value: "off", label: "Disabled" }]} /><p className="ss-help" id="hevc-mode-help">{draft.hevcCompressionMode === "off" ? "Background HEVC compression is disabled." : draft.hevcCompressionMode === "on" ? "Compress eligible local videos during idle time, including on lower-capacity servers." : "Compress eligible local videos when idle and CPU capacity is sufficient."}</p></div></div><AdminHelp label="HEVC compatibility and processing"><p>Automatic enables background HEVC compression when the server is idle and has enough CPU capacity. Always enabled also permits it on lower-capacity servers, while still waiting for idle time. Disabled stops background compression. Eligible existing local videos can also be converted.</p><p>HEVC can reduce file size but takes processing time and may be less compatible with older devices.</p></AdminHelp></Panel></>}
      {view === "activity" && <section aria-label="Storage activity"><div className="ss-sectionhead"><div><h2>Storage activity</h2><p>Uploads are verified before local space is released.</p></div><Button variant="ghost" disabled={pageBusy} onClick={() => void loadPage("activity")}>Refresh activity</Button></div>{activity?.items.map(item => <OperationRow key={item.id} item={item} />)}{activity && activity.items.length === 0 && <p className="ss-empty">No storage activity has been recorded.</p>}{activity?.nextCursor && <Button variant="secondary" disabled={pageBusy} onClick={() => void loadPage("activity", activity.nextCursor!)}>Load more activity</Button>}</section>}
      {pageBusy && <p role="status" className="ss-busy">Loading {view === "activity" ? "activity" : "media"}…</p>}{pageError && view !== "policy" && <div><p role="alert" className="ss-error">{pageError}</p><Button variant="secondary" onClick={() => void loadPage(view === "activity" ? "activity" : "retention")}>Try again</Button></div>}
    </div>}
    <footer className="ss-footnote"><span>{dirty ? "Unsaved changes. Kept while you browse admin sections; leaving Admin Center discards them." : !status ? "Storage status unavailable" : "Settings up to date"}</span><span>Local storage always stays active.</span></footer>
    <SudoModal isOpen={sudo !== null} actionLabel={sudo === "read" ? "Read storage status" : sudo === "policy" ? "Save storage policy" : sudo === "cleanup" ? "Run storage cleanup" : "Change local retention"} onCancel={() => setSudo(null)} onSuccess={() => perform(sudo)} />
  </div></section>;
}
