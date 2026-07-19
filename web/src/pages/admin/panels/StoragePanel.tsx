import React, { useEffect, useState } from "react";
import { AnimatePresence, motion } from "framer-motion";
import { getSettings, testGoogleDrive, updateSettings } from "../../../api/system";
import { Button } from "../../../components/ui/Button";
import { GlassPane } from "../../../components/ui/GlassPane";
import type { SystemSettings } from "../../../types/api";
import { SudoModal } from "../SudoModal";
import { MOTION_TIMINGS } from "../../../motion/motionSystem";

export function StoragePanel() {
  const [settings, setSettings] = useState<SystemSettings | null>(null);
  const [draft, setDraft] = useState<SystemSettings | null>(null);
  const [sudoOpen, setSudoOpen] = useState(false);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [message, setMessage] = useState("");
  const [testingDrive, setTestingDrive] = useState(false);

  const loadSettings = () => {
    setLoading(true);
    setError("");
    getSettings()
      .then((data) => {
        setSettings(data);
        setDraft(data);
      })
      .catch((requestError: unknown) => setError(requestError instanceof Error ? requestError.message : "Settings could not be loaded."))
      .finally(() => setLoading(false));
  };

  useEffect(() => { loadSettings(); }, []);

  const save = async () => {
    if (!draft) return;
    setError("");
    const saved = await updateSettings(draft);
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
            <label className="admin-control">
              <span>Storage engine</span>
              <select value={draft.storageEngine} onChange={(event) => setDraft({ ...draft, storageEngine: event.target.value as SystemSettings["storageEngine"] })}>
                <option value="LOCAL">Local server storage</option>
              <option value="CLOUD" disabled={!draft.driveConfigured}>Google Drive</option>
              </select>
              <small>Choose where newly ingested media is stored.</small>
            </label>
            <div className="admin-control admin-drive-status" data-ready={draft.driveReachable === true}>
              <span>Google Drive</span>
              <strong>{draft.driveConfigured ? draft.driveReachable ? "Connected" : "Needs attention" : "Not configured"}</strong>
              <small>{draft.driveConfigured ? draft.rcloneRemotePath : "Connect Drive during setup before selecting cloud storage."}</small>
              {draft.driveConfigured && <Button type="button" variant="secondary" disabled={testingDrive} onClick={testDrive}>{testingDrive ? "Testing…" : "Test connection"}</Button>}
              <a href="https://github.com/WaqSea/StreamHome/blob/main/docs/google-drive.md" target="_blank" rel="noreferrer">Google Drive setup guide ↗</a>
            </div>
            <label className="admin-control">
              <span>HEVC compression</span>
              <select value={draft.hevcCompressionMode} onChange={(event) => setDraft({ ...draft, hevcCompressionMode: event.target.value as SystemSettings["hevcCompressionMode"] })}>
                <option value="auto">Automatic</option>
                <option value="on">Always enabled</option>
                <option value="off">Disabled</option>
              </select>
              <small>Automatic mode lets the server select the safest compression path.</small>
            </label>
          </div>
          <footer className="admin-settings-actions">
            <AnimatePresence mode="wait">
              {error ? <motion.p key="error" className="admin-form-message admin-form-message--error" role="alert" initial={{ opacity: 0, y: -5 }} animate={{ opacity: 1, y: 0 }} exit={{ opacity: 0 }} transition={{ duration: MOTION_TIMINGS.notice }}>{error}</motion.p> : message ? <motion.p key="message" className="admin-form-message admin-form-message--success" role="status" initial={{ opacity: 0, y: -5 }} animate={{ opacity: 1, y: 0 }} exit={{ opacity: 0 }} transition={{ duration: MOTION_TIMINGS.notice }}>{message}</motion.p> : <span>Changes require account authorization.</span>}
            </AnimatePresence>
            <Button className="admin-primary-action" type="submit" disabled={!dirty}>Save changes</Button>
          </footer>
        </form>
      </GlassPane>
      <SudoModal isOpen={sudoOpen} actionLabel="Save server settings" onCancel={() => setSudoOpen(false)} onSuccess={save} />
    </section>
  );
}
