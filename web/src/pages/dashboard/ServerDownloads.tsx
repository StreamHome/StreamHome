import React from "react";
import { ProgressBar } from "../../components/ui/ProgressBar";
import { useThemeStore } from "../../stores/themeStore";
import { downloadFraction } from "../../utils/media";
import { useDownloadStream, type DownloadConnectionState } from "./useDownloadStream";
import { DownloadFailureNotice } from "./DownloadFailureNotice";

const CONNECTION_LABEL: Record<DownloadConnectionState, string> = {
  disconnected: "Offline",
  connecting: "Connecting",
  connected: "Live",
  reconnecting: "Reconnecting",
};

function statusKind(status: string) {
  const value = status.toLowerCase();
  if (value.includes("fail") || value.includes("error")) return "failed";
  if (value.includes("complete") || value.includes("finish")) return "completed";
  return "active";
}

export function ServerDownloads() {
  const theme = useThemeStore((state) => state.activeTheme);
  const { downloads, connectionState } = useDownloadStream();
  const connected = connectionState === "connected";
  const emptyTitle = connected ? "No download activity" : connectionState === "reconnecting" ? "Restoring the live queue" : "Connecting to the server";
  const emptyBody = connected ? "The server has not reported an active ingestion task." : "Waiting for the authenticated download event stream.";

  return (
    <section className="server-downloads" data-download-theme={theme}>
      <header className="server-downloads__header">
        <div>
          <p>SERVER QUEUE / LIVE</p>
          <h1>Server downloads</h1>
          <span>Live ingestion progress reported by the server.</span>
        </div>
        <strong data-state={connectionState}><i aria-hidden="true" />{CONNECTION_LABEL[connectionState]}</strong>
      </header>
      {!downloads.length ? (
        <div className="server-downloads__empty" data-state={connectionState} role="status">
          <small>{connected ? "QUEUE IDLE" : "QUEUE LINK"}</small>
          <h2>{emptyTitle}</h2>
          <p>{emptyBody}</p>
        </div>
      ) : (
        <div className="server-downloads__list" aria-live="polite">
          {downloads.map((download) => (
            <article key={download.id} data-status={statusKind(download.status)}>
              <header>
                <div><small>{download.status}</small><h2>{download.title}</h2></div>
                <strong>{Math.round(download.progress)}%</strong>
              </header>
              {statusKind(download.status) === "active" && <p>{download.speed} · ETA {download.eta}</p>}
              {statusKind(download.status) === "failed" && <DownloadFailureNotice download={download} />}
              <ProgressBar className="server-downloads__progress" progress={downloadFraction(download.progress)} />
            </article>
          ))}
        </div>
      )}
    </section>
  );
}
