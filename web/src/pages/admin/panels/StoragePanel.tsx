import React, { useEffect, useRef, useState } from "react";
import { AnimatePresence, motion } from "framer-motion";
import { getSettings, testGoogleDrive, updateSettings } from "../../../api/system";
import { Button } from "../../../components/ui/Button";
import { GlassPane } from "../../../components/ui/GlassPane";
import { AdminSelect } from "../../../components/ui/AdminSelect";
import type { SystemSettings } from "../../../types/api";
import { SudoModal } from "../SudoModal";
import { MOTION_TIMINGS } from "../../../motion/motionSystem";
import { AdminHelp } from "../AdminHelp";

export type StorageDraft = Pick<SystemSettings, "storageEngine" | "hevcCompressionMode">;

export function StoragePanel({ retainedDraft, onDraftChange }: { retainedDraft?: StorageDraft; onDraftChange?: (draft: StorageDraft | undefined) => void } = {}) {
  const [settings, setSettings] = useState<SystemSettings | null>(null);
  const [draft, setDraft] = useState<SystemSettings | null>(null);
  const [sudoOpen, setSudoOpen] = useState(false);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [message, setMessage] = useState("");
  const [testingDrive, setTestingDrive] = useState(false);
  const draftRef = useRef(retainedDraft);
  const active = useRef(true);
  const requestId = useRef(0);

  const loadSettings = () => {
    const request = ++requestId.current;
    setLoading(true);
    setError("");
    getSettings()
      .then((data) => {
        if (!active.current || request !== requestId.current) return;
        setSettings(data);
        setDraft({ ...data, ...draftRef.current });
      })
      .catch((requestError: unknown) => { if (active.current && request === requestId.current) setError(requestError instanceof Error ? requestError.message : "Settings could not be loaded."); })
      .finally(() => { if (active.current && request === requestId.current) setLoading(false); });
  };

  useEffect(() => { active.current = true; loadSettings(); return () => { active.current = false; requestId.current += 1; }; }, []);

  const changeDraft = (next: SystemSettings) => {
    setDraft(next);
    const preferences = { storageEngine: next.storageEngine, hevcCompressionMode: next.hevcCompressionMode };
    const dirty = !settings || preferences.storageEngine !== settings.storageEngine || preferences.hevcCompressionMode !== settings.hevcCompressionMode;
    draftRef.current = dirty ? preferences : undefined;
    onDraftChange?.(draftRef.current);
    setMessage("");
  };

  const save = async () => {
    if (!draft) return;
    setError("");
    const saved = await updateSettings(draft);
    if (!active.current) return;
    onDraftChange?.(undefined);
    draftRef.current = undefined;
    setSettings(saved);
    setDraft(saved);
    setMessage("Server settings saved.");
    setSudoOpen(false);
  };

  const testDrive = async () => {
    setTestingDrive(true);
    setError("");
    setMessage("");
    try {
      await testGoogleDrive();
      setMessage("Google Drive is reachable and authorized.");
      loadSettings();
    } catch (requestError) {
      setError(requestError instanceof Error ? requestError.message : "Google Drive could not be tested.");
    } finally {
      setTestingDrive(false);
    }
  };

  const header = (
    <header className="admin-panel__header">
      <p>SERVER / STORAGE</p>
      <h1>Storage and HEVC</h1>
      <span>Configure the server storage target and media compression policy.</span>
    </header>
  );

  if (!draft) {
    return (
      <section className="admin-panel admin-panel--storage">
        {header}
        <GlassPane className="admin-state-card" spotlight={false}>
          <p>{loading ? "CONTACTING SERVER" : "SETTINGS UNAVAILABLE"}</p>
          <h2>{loading ? "Loading server settings…" : "Storage settings could not be loaded."}</h2>
          {error && <span role="alert">{error}</span>}
          {!loading && <Button variant="secondary" onClick={loadSettings}>Try again</Button>}
        </GlassPane>
      </section>
    );
  }

  const dirty = JSON.stringify(settings) !== JSON.stringify(draft);
  return (
    <section className="admin-panel admin-panel--storage">
      {header}
      <GlassPane className="admin-settings-card" spotlight={false}>
        <form onSubmit={(event) => { event.preventDefault(); if (dirty) { setMessage(""); setSudoOpen(true); } }}>
          <div className="admin-settings-grid">
            <div className="admin-control">
              <AdminSelect label="Storage engine" aria-describedby="storage-engine-help" value={draft.storageEngine} onChange={(value) => changeDraft({ ...draft, storageEngine: value as SystemSettings["storageEngine"] })} options={[{ value: "LOCAL", label: "Local server storage" }, { value: "CLOUD", label: "Google Drive", disabled: !draft.driveConfigured }]} />
              <small id="storage-engine-help">Choose where newly ingested media is stored.</small>
            </div>
            <div className="admin-control">
              <AdminSelect label="HEVC compression" aria-describedby="hevc-mode-help" value={draft.hevcCompressionMode} onChange={(value) => changeDraft({ ...draft, hevcCompressionMode: value as SystemSettings["hevcCompressionMode"] })} options={[{ value: "auto", label: "Automatic" }, { value: "on", label: "Always enabled" }, { value: "off", label: "Disabled" }]} />
              <small id="hevc-mode-help">{draft.hevcCompressionMode === "off" ? "Background HEVC compression is disabled." : draft.hevcCompressionMode === "on" ? "Compress eligible local videos during idle time, including on lower-capacity servers." : "Compress eligible local videos when idle and CPU capacity is sufficient."}</small>
            </div>
          </div>
          <footer className="admin-settings-actions">
            <AnimatePresence mode="wait">
              {error ? <motion.p key="error" className="admin-form-message admin-form-message--error" role="alert" initial={{ opacity: 0, y: -5 }} animate={{ opacity: 1, y: 0 }} exit={{ opacity: 0 }} transition={{ duration: MOTION_TIMINGS.notice }}>{error}</motion.p> : message ? <motion.p key="message" className="admin-form-message admin-form-message--success" role="status" initial={{ opacity: 0, y: -5 }} animate={{ opacity: 1, y: 0 }} exit={{ opacity: 0 }} transition={{ duration: MOTION_TIMINGS.notice }}>{message}</motion.p> : <span>{dirty ? "Unsaved changes. Kept while you browse admin sections; leaving Admin Center discards them." : "Changes require account authorization."}</span>}
            </AnimatePresence>
            <Button className="admin-primary-action" type="submit" disabled={!dirty}>Save changes</Button>
            {dirty && <Button type="button" variant="ghost" onClick={() => { if (settings) changeDraft(settings); }}>Discard changes</Button>}
          </footer>
        </form>
        <div className="admin-storage-help">
          <AdminHelp label="HEVC compatibility and processing"><p>Automatic enables background HEVC compression when the server is idle and has enough CPU capacity. Always enabled also permits it on lower-capacity servers, while still waiting for idle time. Disabled stops background compression. Eligible existing local videos can also be converted.</p><p>HEVC can reduce file size but takes processing time and may be less compatible with older devices.</p></AdminHelp>
          <AdminHelp label="Google Drive connection"><p>Google Drive stores newly ingested media in the configured server remote. Configure the connection on the server before selecting cloud storage.</p><p>Testing the connection checks whether the server can reach its configured remote. It does not save your storage or compression changes.</p></AdminHelp>
        </div>
        <section className="admin-control admin-drive-status" aria-label="Google Drive status" data-ready={draft.driveReachable === true}>
              <span>Google Drive</span>
              <strong>{draft.driveConfigured ? draft.driveReachable ? "Connected" : "Needs attention" : "Not configured"}</strong>
              <small>{draft.driveConfigured ? draft.rcloneRemotePath : "Configure the server’s Google Drive connection using the setup guide below. Cloud storage stays unavailable until the connection is configured."}</small>
              {draft.driveConfigured && <Button type="button" variant="secondary" disabled={testingDrive} onClick={testDrive}>{testingDrive ? "Testing…" : "Test connection"}</Button>}
              <a href="https://github.com/StreamHome/StreamHome/blob/v0.1.0-alpha.1/docs/google-drive.md" target="_blank" rel="noreferrer">Google Drive setup guide ↗</a>
            </section>
      </GlassPane>
      <SudoModal isOpen={sudoOpen} actionLabel="Save server settings" onCancel={() => setSudoOpen(false)} onSuccess={save} />
    </section>
  );
}
