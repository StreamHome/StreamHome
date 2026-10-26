import React from "react";
import { ProgressBar } from "../../../components/ui/ProgressBar";
import { downloadFraction } from "../../../utils/media";
import { useDownloadStream } from "../useDownloadStream";
import { DownloadFailureNotice } from "../DownloadFailureNotice";

export function EmberDownloads() {
  const { downloads, connectionState } = useDownloadStream();
  const connected = connectionState === "connected";
  const connectionLabel = connected ? "Connected" : connectionState === "reconnecting" ? "Reconnecting" : "Connecting";

  return <section className="ember-downloads"><header className="ember-page-heading"><div><p>SERVER QUEUE / LIVE</p><h1>Server downloads</h1><small>Live ingestion progress reported by the server.</small></div><span data-connected={connected}>{connectionLabel}</span></header>{downloads.length ? <div className="ember-downloads__list">{downloads.map((download) => {
    const failed = /fail|error/i.test(download.status);
    const completed = /complete|finish/i.test(download.status);
    return <article key={download.id}><header><div><small>{download.status}</small><h2>{download.title}</h2></div><strong>{Math.round(download.progress)}%</strong></header>{failed ? <DownloadFailureNotice download={download} /> : !completed && <p>{download.speed} · ETA {download.eta}</p>}<ProgressBar progress={downloadFraction(download.progress)} /></article>;
  })}</div> : <div className="ember-state-panel ember-downloads__empty"><p>{connected ? "QUEUE IDLE" : "QUEUE LINK"}</p><h2>{connected ? "No download activity" : connectionState === "reconnecting" ? "Reconnecting to the server" : "Connecting to the server"}</h2><span>{connected ? "The server has not reported an active ingestion task." : "Waiting for the authenticated download event stream."}</span></div>}</section>;
}
