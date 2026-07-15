import React, { useEffect, useState } from "react";
import { useAuthStore } from "../../stores/authStore";
import type { DownloadEvent } from "../../types/api";
import { downloadFraction } from "../../utils/media";
import { GlassPane } from "../../components/ui/GlassPane";
import { ProgressBar } from "../../components/ui/ProgressBar";

export function ServerDownloads() {
  const token = useAuthStore((state) => state.token);
  const [downloads, setDownloads] = useState<DownloadEvent[]>([]);
  const [connected, setConnected] = useState(false);

  useEffect(() => {
    if (!token) return;
    const source = new EventSource(`/api/downloads/stream?token=${encodeURIComponent(token)}`);
    source.onopen = () => setConnected(true);
    source.onerror = () => setConnected(false);
    source.onmessage = (event) => {
      try {
        const next = JSON.parse(event.data) as DownloadEvent[];
        setDownloads(Array.isArray(next) ? next : []);
      } catch {
        setConnected(false);
      }
    };
    return () => source.close();
  }, [token]);

  return (
    <section className="server-downloads mx-auto max-w-5xl px-6 py-10">
      <div className="mb-8 flex items-end justify-between gap-4">
        <div>
          <h1 className="text-3xl font-semibold">Server downloads</h1>
          <p className="mt-2 text-[var(--text-muted)]">Live ingestion progress reported by the server.</p>
        </div>
        <span className="text-xs uppercase tracking-wider text-[var(--text-muted)]">{connected ? "Connected" : "Reconnecting"}</span>
      </div>
      {!downloads.length ? (
        <GlassPane className="p-10 text-center text-[var(--text-muted)]" spotlight={false}>No download activity reported.</GlassPane>
      ) : (
        <div className="flex flex-col gap-4">
          {downloads.map((download) => (
            <GlassPane key={download.id} className="p-5" spotlight={false}>
              <div className="flex items-start justify-between gap-4">
                <div>
                  <h2 className="font-semibold">{download.title}</h2>
                  <p className="mt-1 text-sm text-[var(--text-muted)]">{download.status} · {download.speed} · ETA {download.eta}</p>
                </div>
                <span className="font-[family-name:var(--font-mono)] text-sm">{Math.round(download.progress)}%</span>
              </div>
              <ProgressBar className="mt-4" progress={downloadFraction(download.progress)} />
            </GlassPane>
          ))}
        </div>
      )}
    </section>
  );
}
