import { useEffect, useState } from "react";
import { useAuthStore } from "../../stores/authStore";
import type { DownloadEvent } from "../../types/api";
import { normalizeDownload } from "../../api/downloads";

export type DownloadConnectionState = "disconnected" | "connecting" | "connected" | "reconnecting";

export function parseDownloadPayload(payload: string): DownloadEvent[] | null {
  try {
    const parsed = JSON.parse(payload) as unknown;
    const values = Array.isArray(parsed) ? parsed : [parsed];
    const downloads = values.map(normalizeDownload).filter((item): item is DownloadEvent => item !== null);
    return downloads.length === values.length ? downloads : null;
  } catch {
    return null;
  }
}

export function useDownloadStream() {
  const isAuthenticated = useAuthStore((state) => state.isAuthenticated);
  const [downloads, setDownloads] = useState<DownloadEvent[]>([]);
  const [connectionState, setConnectionState] = useState<DownloadConnectionState>(isAuthenticated ? "connecting" : "disconnected");

  useEffect(() => {
    if (!isAuthenticated) {
      setDownloads([]);
      setConnectionState("disconnected");
      return;
    }
    setDownloads([]);
    setConnectionState("connecting");
    const source = new EventSource("/api/downloads/stream", { withCredentials: true });
    source.onopen = () => setConnectionState("connected");
    source.onerror = () => setConnectionState("reconnecting");
    source.onmessage = (event) => {
      const next = parseDownloadPayload(event.data);
      if (next === null) return;
      setDownloads(next);
      setConnectionState("connected");
    };
    return () => source.close();
  }, [isAuthenticated]);

  return { downloads, connectionState };
}
