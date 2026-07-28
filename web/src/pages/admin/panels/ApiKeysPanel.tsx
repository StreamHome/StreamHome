import React, { useCallback, useEffect, useMemo, useState } from "react";
import { AnimatePresence, motion } from "framer-motion";
import {
  createIntegrationCredential,
  getIntegrationCredentials,
  getIntegrationScopes,
  revokeIntegrationCredential,
  updateIntegrationCredential,
} from "../../../api/auth";
import { ApiError } from "../../../api/client";
import { MOTION_TIMINGS } from "../../../motion/motionSystem";
import type { IntegrationCredentialInfo, IntegrationScope, IntegrationScopeDefinition } from "../../../types/api";
import { SudoModal } from "../SudoModal";

function formatTime(value?: number | null): string {
  return value
    ? new Intl.DateTimeFormat(undefined, { dateStyle: "medium", timeStyle: "short" }).format(new Date(value * 1000))
    : "Never";
}

function credentialState(credential: IntegrationCredentialInfo): "active" | "expired" | "revoked" {
  if (credential.revokedAt) return "revoked";
  if (credential.expiresAt && credential.expiresAt <= Date.now() / 1000) return "expired";
  return "active";
}

export function ApiKeysPanel() {
  const [credentials, setCredentials] = useState<IntegrationCredentialInfo[]>([]);
  const [scopeDefinitions, setScopeDefinitions] = useState<IntegrationScopeDefinition[]>([]);
  const [name, setName] = useState("");
  const [selectedScopes, setSelectedScopes] = useState<IntegrationScope[]>(["ingest"]);
  const [expiration, setExpiration] = useState("never");
  const [generatedToken, setGeneratedToken] = useState("");
  const [generatedName, setGeneratedName] = useState("");
  const [copyStatus, setCopyStatus] = useState<"" | "copied" | "failed">("");
  const [editingId, setEditingId] = useState<string | null>(null);
  const [editingName, setEditingName] = useState("");
  const [editingScopes, setEditingScopes] = useState<IntegrationScope[]>([]);
  const [confirmRevokeId, setConfirmRevokeId] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [message, setMessage] = useState("");
  const [reauthorizationRequired, setReauthorizationRequired] = useState(false);

  const handleError = useCallback((requestError: unknown, fallback: string) => {
    if (requestError instanceof ApiError && requestError.code === "reauthentication_required") {
      setReauthorizationRequired(true);
      setError("Your recent administrator authorization expired. Reauthorize to continue managing API keys.");
      return;
    }
    setError(requestError instanceof Error ? requestError.message : fallback);
  }, []);

  const load = useCallback(async (signal?: AbortSignal) => {
    setLoading(true);
    setError("");
    try {
      const [nextCredentials, nextScopes] = await Promise.all([
        getIntegrationCredentials(signal),
        getIntegrationScopes(signal),
      ]);
      setCredentials(nextCredentials);
      setScopeDefinitions(nextScopes);
      setReauthorizationRequired(false);
    } catch (requestError) {
      if (!signal?.aborted) handleError(requestError, "API keys could not be loaded.");
    } finally {
      if (!signal?.aborted) setLoading(false);
    }
  }, [handleError]);

  useEffect(() => {
    const controller = new AbortController();
    void load(controller.signal);
    return () => controller.abort();
  }, [load]);

  const counts = useMemo(() => credentials.reduce((result, credential) => {
    result[credentialState(credential)] += 1;
    return result;
  }, { active: 0, expired: 0, revoked: 0 }), [credentials]);
  const activeSenders = useMemo(
    () => credentials.filter((credential) => credentialState(credential) === "active" && credential.scopes.includes("ingest")).length,
    [credentials],
  );

  const toggleScope = (scope: IntegrationScope, editing = false) => {
    const selected = editing ? editingScopes : selectedScopes;
    const update = editing ? setEditingScopes : setSelectedScopes;
    update(selected.includes(scope) ? selected.filter((item) => item !== scope) : [...selected, scope]);
  };

  const createApiKey = async (event: React.FormEvent) => {
    event.preventDefault();
    setBusy(true);
    setError("");
    setMessage("");
    setCopyStatus("");
    try {
      const expiresInDays = expiration === "never" ? null : Number(expiration);
      const result = await createIntegrationCredential(name, selectedScopes, expiresInDays);
      setGeneratedToken(result.token);
      setGeneratedName(result.credential.name);
      setName("");
      setSelectedScopes(["ingest"]);
      setExpiration("never");
      setMessage("API key created. Save the secret now; StreamHome will not display it again.");
      await load();
    } catch (requestError) {
      handleError(requestError, "The API key could not be created.");
    } finally {
      setBusy(false);
    }
  };

  const copyToken = async () => {
    try {
      await navigator.clipboard.writeText(generatedToken);
      setCopyStatus("copied");
    } catch {
      setCopyStatus("failed");
    }
  };

  const downloadToken = () => {
    const blob = new Blob([
      `StreamHome API key\nName: ${generatedName}\nGenerated: ${new Date().toISOString()}\n\n${generatedToken}\n`,
    ], { type: "text/plain" });
    const href = URL.createObjectURL(blob);
    const anchor = document.createElement("a");
    anchor.href = href;
    anchor.download = `streamhome-api-key-${generatedName.toLowerCase().replace(/[^a-z0-9]+/g, "-").replace(/^-|-$/g, "") || "credential"}.txt`;
    anchor.click();
    URL.revokeObjectURL(href);
  };

  const beginEdit = (credential: IntegrationCredentialInfo) => {
    setEditingId(credential.id);
    setEditingName(credential.name);
    setEditingScopes([...credential.scopes]);
    setConfirmRevokeId(null);
    setError("");
    setMessage("");
  };

  const saveEdit = async (event: React.FormEvent) => {
    event.preventDefault();
    if (!editingId) return;
    setBusy(true);
    setError("");
    setMessage("");
    try {
      await updateIntegrationCredential(editingId, editingName, editingScopes);
      setEditingId(null);
      setMessage("API key settings updated.");
      await load();
    } catch (requestError) {
      handleError(requestError, "The API key could not be updated.");
    } finally {
      setBusy(false);
    }
  };

  const revoke = async (credential: IntegrationCredentialInfo) => {
    if (confirmRevokeId !== credential.id) {
      setConfirmRevokeId(credential.id);
      setEditingId(null);
      return;
    }
    setBusy(true);
    setError("");
    setMessage("");
    try {
      await revokeIntegrationCredential(credential.id);
      setConfirmRevokeId(null);
      setMessage(`API key “${credential.name}” revoked. Other API keys remain active.`);
      await load();
    } catch (requestError) {
      handleError(requestError, "The API key could not be revoked.");
    } finally {
      setBusy(false);
    }
  };

  const reauthorized = async () => {
    setReauthorizationRequired(false);
    await load();
  };

  return <section className="admin-panel admin-panel--api-keys admin-security">
    <header className="admin-panel__header"><p>INTEGRATIONS / MACHINE ACCESS</p><h1>API keys</h1><span>Create separate credentials for MediaSender clients and automation, then control exactly what each application can do.</span></header>
    <div className="security-content api-key-content">
      <div className="security-heading"><div><p className="security-eyebrow">Server-wide credentials</p><h2>Application access</h2><span>API keys belong to the server account, not to a viewing profile. Secrets are displayed only once.</span></div><button type="button" onClick={() => void load()} disabled={loading || busy}>Refresh</button></div>
      <AnimatePresence>{(error || message) && <motion.div className={error ? "security-notice security-notice--error" : "security-notice"} initial={{ opacity: 0, y: -6 }} animate={{ opacity: 1, y: 0 }} exit={{ opacity: 0 }} transition={{ duration: MOTION_TIMINGS.notice }} role={error ? "alert" : "status"}>{error || message}{reauthorizationRequired && <button type="button" onClick={() => setReauthorizationRequired(true)}>Reauthorize</button>}</motion.div>}</AnimatePresence>
      <section className="security-overview api-key-overview" aria-label="API key summary">
        <article><span>Active keys</span><strong>{counts.active}</strong><small>Currently accepted by the server</small></article>
        <article><span>Media senders</span><strong>{activeSenders}</strong><small>Active keys with Add media permission</small></article>
        <article><span>Expired</span><strong>{counts.expired}</strong><small>No longer accepted</small></article>
        <article><span>Revoked</span><strong>{counts.revoked}</strong><small>Permanently disabled</small></article>
      </section>
      <section className="security-card security-integrations">
        <header><div><p className="security-eyebrow">Create credential</p><h3>New API key</h3></div><span>Add media selected</span></header>
        <p>Use a different named key for each extension or application. New keys start with Add media enabled so MediaSender can submit movies and episodes.</p>
        <form className="security-integration-create" onSubmit={createApiKey}>
          <label>Key name<input aria-label="API key name" maxLength={80} value={name} onChange={(event) => setName(event.target.value)} placeholder="Living room MediaSender" required /></label>
          <label>Expiration<select aria-label="API key expiration" value={expiration} onChange={(event) => setExpiration(event.target.value)}><option value="never">Never</option><option value="30">30 days</option><option value="90">90 days</option><option value="365">1 year</option></select></label>
          <fieldset><legend>Permissions</legend><div className="security-scope-grid">{scopeDefinitions.map((scope) => <label key={scope.id}><input aria-label={scope.label} type="checkbox" checked={selectedScopes.includes(scope.id)} onChange={() => toggleScope(scope.id)} /><span><strong>{scope.label}</strong><small>{scope.description}</small></span></label>)}</div></fieldset>
          <button className="security-primary" disabled={busy || loading || Boolean(generatedToken) || !name.trim() || selectedScopes.length === 0}>Create API key</button>
        </form>
        {generatedToken && <section className="security-integration-secret" aria-labelledby="generated-api-key-title"><div><p className="security-eyebrow">Shown once</p><h4 id="generated-api-key-title">Save the API key for {generatedName}</h4><p>StreamHome stores only a secure hash. This secret cannot be displayed again after you dismiss it.</p></div><code>{generatedToken}</code><div className="security-inline-actions"><button type="button" onClick={() => void copyToken()}>{copyStatus === "copied" ? "Copied" : "Copy key"}</button><button type="button" onClick={downloadToken}>Download .txt</button><button type="button" className="security-primary" onClick={() => { setGeneratedToken(""); setGeneratedName(""); setCopyStatus(""); }}>I saved the key</button></div>{copyStatus === "failed" && <small role="status">Clipboard access was blocked. Select the key above and copy it manually.</small>}</section>}
      </section>
      <section className="security-card security-integrations api-key-inventory">
        <header><div><p className="security-eyebrow">Credential inventory</p><h3>Existing API keys</h3></div><span>{counts.active} active</span></header>
        <p>A key marked Media add enabled is permitted to call <code>/api/add-movie</code>. If an enabled key receives a 403, inspect the returned error code rather than changing its permissions blindly.</p>
        <div className="security-integration-list">
          {credentials.map((credential) => {
            const state = credentialState(credential);
            const mediaEnabled = credential.scopes.includes("ingest");
            return <article key={credential.id} data-state={state}>
              <div className="security-integration-summary"><div><strong>{credential.name}</strong><b>{state}</b><i className="api-key-readiness" data-ready={mediaEnabled}>{mediaEnabled ? "Media add enabled" : "Media add disabled"}</i></div><code>{credential.tokenHint || "Legacy key"}</code><span>Created {formatTime(credential.createdAt)} · Last used {formatTime(credential.lastUsedAt)}</span><span>{credential.expiresAt ? `Expires ${formatTime(credential.expiresAt)}` : "Does not expire"}</span><ul>{credential.scopes.map((scope) => <li key={scope}>{scopeDefinitions.find((item) => item.id === scope)?.label || scope}</li>)}</ul></div>
              {editingId === credential.id ? <form className="security-integration-edit" onSubmit={saveEdit}><label>Key name<input aria-label={`Edit name for ${credential.name}`} maxLength={80} value={editingName} onChange={(event) => setEditingName(event.target.value)} required /></label><fieldset><legend>Permissions</legend><div className="security-scope-grid">{scopeDefinitions.map((scope) => <label key={scope.id}><input aria-label={`${scope.label} for ${credential.name}`} type="checkbox" checked={editingScopes.includes(scope.id)} onChange={() => toggleScope(scope.id, true)} /><span><strong>{scope.label}</strong><small>{scope.description}</small></span></label>)}</div></fieldset><div className="security-inline-actions"><button type="button" onClick={() => setEditingId(null)}>Cancel</button><button className="security-primary" disabled={busy || !editingName.trim() || editingScopes.length === 0}>Save changes</button></div></form> : <div className="security-integration-actions">{state === "active" && <button type="button" onClick={() => beginEdit(credential)}>{mediaEnabled ? "Edit" : "Edit permissions"}</button>}{!credential.revokedAt && <button type="button" className="security-danger" onClick={() => void revoke(credential)}>{confirmRevokeId === credential.id ? "Confirm revoke" : "Revoke"}</button>}{confirmRevokeId === credential.id && <button type="button" onClick={() => setConfirmRevokeId(null)}>Cancel</button>}</div>}
            </article>;
          })}
          {!loading && credentials.length === 0 && <p className="security-integration-empty">No API keys have been created.</p>}
          {loading && credentials.length === 0 && <p className="security-integration-empty">Loading API keys…</p>}
        </div>
      </section>
    </div>
    <SudoModal isOpen={reauthorizationRequired} actionLabel="Manage API keys" onCancel={() => setReauthorizationRequired(false)} onSuccess={reauthorized} />
  </section>;
}
