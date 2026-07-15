import React, { useEffect, useState } from "react";
import { getSettings, updateSettings } from "../../../api/system";
import { Button } from "../../../components/ui/Button";
import { GlassPane } from "../../../components/ui/GlassPane";
import { Input } from "../../../components/ui/Input";
import type { SystemSettings } from "../../../types/api";
import { SudoModal } from "../SudoModal";

export function StoragePanel() {
  const [settings, setSettings] = useState<SystemSettings | null>(null);
  const [draft, setDraft] = useState<SystemSettings | null>(null);
  const [sudoOpen, setSudoOpen] = useState(false);
  const [error, setError] = useState("");
  const [message, setMessage] = useState("");

  useEffect(() => { getSettings().then((data) => { setSettings(data); setDraft(data); }).catch((requestError: unknown) => setError(requestError instanceof Error ? requestError.message : "Settings could not be loaded.")); }, []);

  const save = async () => {
    if (!draft) return;
    const saved = await updateSettings(draft);
    setSettings(saved); setDraft(saved); setMessage("Server settings saved."); setSudoOpen(false);
  };

  if (!draft) return <section className="p-8"><h1 className="text-3xl font-semibold">Storage and HEVC</h1><p className="mt-6 text-[var(--text-error)]">{error || "Loading server settings…"}</p></section>;
  const dirty = JSON.stringify(settings) !== JSON.stringify(draft);
  return (
    <section className="mx-auto max-w-4xl p-8"><h1 className="text-3xl font-semibold">Storage and HEVC</h1><p className="mt-2 text-[var(--text-muted)]">Settings loaded directly from the server.</p>
      <GlassPane className="mt-8 flex flex-col gap-6 p-7" spotlight={false}>
        <label className="flex flex-col gap-2"><span>Storage engine</span><select className="rounded-[var(--radius)] border border-[var(--glass-border)] bg-[var(--bg-surface-container-lowest)] px-4 py-3" value={draft.storageEngine} onChange={(event) => setDraft({ ...draft, storageEngine: event.target.value as SystemSettings["storageEngine"] })}><option value="LOCAL">Local</option><option value="CLOUD">Cloud</option></select></label>
        <Input label="Rclone remote path" value={draft.rcloneRemotePath} onChange={(event) => setDraft({ ...draft, rcloneRemotePath: event.target.value })} disabled={draft.storageEngine !== "CLOUD"} />
        <label className="flex flex-col gap-2"><span>HEVC compression</span><select className="rounded-[var(--radius)] border border-[var(--glass-border)] bg-[var(--bg-surface-container-lowest)] px-4 py-3" value={draft.hevcCompressionMode} onChange={(event) => setDraft({ ...draft, hevcCompressionMode: event.target.value as SystemSettings["hevcCompressionMode"] })}><option value="auto">Automatic</option><option value="on">On</option><option value="off">Off</option></select></label>
        <div><Button disabled={!dirty} onClick={() => { setMessage(""); setSudoOpen(true); }}>Save changes</Button></div>
        {message && <p className="text-sm text-green-400">{message}</p>}{error && <p className="text-sm text-[var(--text-error)]">{error}</p>}
      </GlassPane>
      <SudoModal isOpen={sudoOpen} actionLabel="Save server settings" onCancel={() => setSudoOpen(false)} onSuccess={save} />
    </section>
  );
}
