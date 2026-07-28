import React, { useCallback, useEffect, useState } from "react";
import { getAdminProfileData } from "../../../api/adminProfiles";
import { Button } from "../../../components/ui/Button";
import { GlassPane } from "../../../components/ui/GlassPane";
import type { AdminMediaBrief, AdminProfileData } from "../../../types/api";

type DataTab = "overview" | "history" | "watchlist" | "recommendations" | "cache" | "activity";

const TABS: Array<{ id: DataTab; label: string }> = [
  { id: "overview", label: "Overview" },
  { id: "history", label: "History" },
  { id: "watchlist", label: "Watchlist" },
  { id: "recommendations", label: "Recommendations" },
  { id: "cache", label: "TMDB cache" },
  { id: "activity", label: "Activity" },
];

export function ProfileDataPanel({ profileId }: { profileId: string }) {
  const [data, setData] = useState<AdminProfileData | null>(null);
  const [tab, setTab] = useState<DataTab>("overview");
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");

  const load = useCallback((signal?: AbortSignal) => {
    setLoading(true);
    setError("");
    return getAdminProfileData(profileId, signal)
      .then(setData)
      .catch((requestError) => {
        if (!signal?.aborted) setError(requestError instanceof Error ? requestError.message : "Profile data could not be loaded.");
      })
      .finally(() => {
        if (!signal?.aborted) setLoading(false);
      });
  }, [profileId]);

  useEffect(() => {
    const controller = new AbortController();
    setData(null);
    void load(controller.signal);
    return () => controller.abort();
  }, [load]);

  return <section className="admin-panel admin-panel--profile-data">
    <header className="admin-panel__header">
      <p>PROFILE / DATA EXPLORER</p>
      <h1>Profile data</h1>
      <span>Inspect durable viewing, saved titles, personalization signals, and the shared TMDB cache without entering or impersonating the selected profile.</span>
    </header>
    <nav className="profile-data-tabs" aria-label="Profile data categories">
      {TABS.map((item) => <button key={item.id} type="button" data-active={tab === item.id} aria-current={tab === item.id ? "page" : undefined} onClick={() => setTab(item.id)}>{item.label}</button>)}
    </nav>
    {loading && !data ? <GlassPane className="admin-state-card" spotlight={false}><p>READING PROFILE DATA</p><h2>Loading the durable profile record…</h2></GlassPane> : error && !data ? <GlassPane className="admin-state-card" spotlight={false}><p>PROFILE DATA UNAVAILABLE</p><h2>The selected profile could not be inspected.</h2><div><span role="alert">{error}</span></div><Button onClick={() => void load()}>Try again</Button></GlassPane> : data && <>
      {tab === "overview" && <Overview data={data} />}
      {tab === "history" && <History data={data} />}
      {tab === "watchlist" && <Watchlist data={data} />}
      {tab === "recommendations" && <Recommendations data={data} />}
      {tab === "cache" && <Cache data={data} />}
      {tab === "activity" && <Activity data={data} />}
      {error && <p className="admin-form-message admin-form-message--error" role="alert">{error}</p>}
    </>}
  </section>;
}

function Overview({ data }: { data: AdminProfileData }) {
  const { overview } = data;
  return <div className="profile-data-stack">
    <div className="profile-data-metrics" aria-label="Profile data summary">
      <Metric label="Watch time" value={formatDuration(overview.watchSeconds)} detail={`${overview.counts.history} viewing attempts`} />
      <Metric label="Watchlist" value={String(overview.counts.watchlist)} detail={`${overview.counts.resumeStates} resume states`} />
      <Metric label="Recommendations" value={String(overview.counts.recommendations)} detail={`${overview.counts.preferences} explicit choices`} />
      <Metric label="Last activity" value={overview.lastActivityAt ? formatDate(overview.lastActivityAt, true) : "Never"} detail={`${overview.completedTitles} completed attempts`} />
    </div>
    <GlassPane className="profile-data-card" spotlight={false}>
      <header><p>DATA LOCATION MAP</p><h2>What is durable, shared, or temporary</h2><span>TMDB catalog rows are shared across profiles. A profile owns only its recommendation assignment and interaction signals.</span></header>
      <div className="profile-data-location-list">
        {overview.persistence.map((item) => <article key={item.label}><div><strong>{item.label}</strong><span>{item.location}</span></div><i data-durable={item.durable}>{item.durable ? "Durable" : "Temporary"}</i></article>)}
      </div>
    </GlassPane>
    <GlassPane className="profile-data-card" spotlight={false}>
      <header><p>ACTIVITY INVENTORY</p><h2>Stored records</h2></header>
      <dl className="profile-data-definition-grid">
        {Object.entries(overview.counts).map(([key, value]) => <div key={key}><dt>{words(key)}</dt><dd>{value}</dd></div>)}
      </dl>
    </GlassPane>
  </div>;
}

function History({ data }: { data: AdminProfileData }) {
  return <div className="profile-data-stack">
    <SectionHeading eyebrow="VIEWING ATTEMPTS" title={`${data.history.total} history records`} description="Attempts are the chronological history used by recommendations. Resume states are the latest playback positions shown in Continue Watching." />
    <DataTable headers={["Title", "Episode", "Last watched", "Watched", "Completion", "Outcome"]}>
      {data.history.items.map((item) => <tr key={item.id}>
        <td><MediaTitle media={item.movie} /></td>
        <td>{item.episode ? `S${item.episode.seasonNumber} E${item.episode.episodeNumber} · ${item.episode.title}` : "Movie"}</td>
        <td>{formatDate(item.lastSeenAt)}</td>
        <td>{formatDuration(item.durationWatched)}</td>
        <td>{percent(item.maxCompletion)}</td>
        <td>{item.completedAt ? "Completed" : item.earlyExitRecorded ? "Early exit" : "In progress"}</td>
      </tr>)}
    </DataTable>
    {!data.history.items.length && <Empty label="No viewing history has been recorded for this profile." />}
    <SectionHeading eyebrow="CONTINUE WATCHING" title={`${data.history.resumeStates.length} resume states`} description="These rows hold the most recent playback position for each movie or episode." />
    <DataTable headers={["Title", "Position", "Completion", "Updated", "State"]}>
      {data.history.resumeStates.map((item) => <tr key={`${item.movieId}:${item.episodeId ?? "movie"}`}>
        <td><MediaTitle media={item.movie} fallback={item.movieId} /></td>
        <td>{formatDuration(item.timestamp)}</td>
        <td>{percent(item.completionRate)}</td>
        <td>{formatDate(item.updatedAt)}</td>
        <td>{item.finished ? "Finished" : "Resume available"}</td>
      </tr>)}
    </DataTable>
  </div>;
}

function Watchlist({ data }: { data: AdminProfileData }) {
  return <div className="profile-data-stack">
    <SectionHeading eyebrow="SAVED TITLES" title={`${data.watchlist.total} watchlist entries`} description="Entries remain in server order and are stored in SQLite." />
    <DataTable headers={["Title", "Type", "Availability", "Added"]}>
      {data.watchlist.items.map((item) => <tr key={item.id}>
        <td><MediaTitle media={item.movie} /></td>
        <td>{item.movie?.type ?? "Unknown"}</td>
        <td>{item.movie?.availability ?? "Missing catalog record"}</td>
        <td>{formatDate(item.createdAt)}</td>
      </tr>)}
    </DataTable>
    {!data.watchlist.items.length && <Empty label="This profile has not saved any titles." />}
  </div>;
}

function Recommendations({ data }: { data: AdminProfileData }) {
  const recommendations = data.recommendations;
  return <div className="profile-data-stack">
    <div className="profile-data-metrics">
      <Metric label="Candidate pool" value={String(recommendations.total)} detail={`${recommendations.preferences.length} explicit choices`} />
      <Metric label="Learned tastes" value={String(recommendations.tastes.length)} detail={`${recommendations.onboarding.genres.length} starting genres`} />
      <Metric label="Taste version" value={String(recommendations.refresh?.tasteVersion ?? 0)} detail={recommendations.refresh?.refreshRequested ? "Refresh requested" : "Pool current"} />
      <Metric label="Shadow overlap" value={recommendations.runtimeMetric ? String(recommendations.runtimeMetric.top20Overlap) : "—"} detail={recommendations.runtimeMetric ? `Mean displacement ${recommendations.runtimeMetric.meanDisplacement}` : "No persisted comparison"} />
    </div>
    <GlassPane className="profile-data-card" spotlight={false}>
      <header><p>LEARNED SIGNALS</p><h2>Tastes and starting choices</h2></header>
      <div className="profile-data-signal-grid">
        <section><strong>Cold-start genres</strong><p>{recommendations.onboarding.genres.join(", ") || "None"}</p></section>
        <section><strong>Top learned tastes</strong><div>{recommendations.tastes.slice(0, 12).map((taste) => <span key={`${taste.kind}:${taste.value}`}><b>{taste.value}</b><i>{taste.score.toFixed(2)}</i></span>)}</div></section>
      </div>
    </GlassPane>
    <SectionHeading eyebrow="PERSISTED CANDIDATES" title={`${recommendations.total} ranked titles`} description="This is the durable profile candidate pool. Final feed ordering may be recalculated when the profile opens a page." />
    <DataTable headers={["Title", "Score", "Why", "Source", "Confidence", "Feedback"]}>
      {recommendations.items.map((item, index) => <tr key={`${item.movie?.id ?? "missing"}:${index}`}>
        <td><MediaTitle media={item.movie} /></td>
        <td>{item.score.toFixed(3)}</td>
        <td>{item.reasons.join(" · ") || "No reason stored"}</td>
        <td>{words(item.candidateSource)}</td>
        <td>{percent(item.sourceConfidence)}</td>
        <td>{item.preference ?? "None"}</td>
      </tr>)}
    </DataTable>
  </div>;
}

function Cache({ data }: { data: AdminProfileData }) {
  const cache = data.cache;
  return <div className="profile-data-stack">
    <div className="profile-data-metrics">
      <Metric label="Linked to profile" value={String(cache.total)} detail="Through recommendations or activity" />
      <Metric label="Shared TMDB cache" value={String(cache.sharedCacheTotal)} detail="Server-wide metadata records" />
      <Metric label="Unreferenced here" value={String(cache.unreferencedSharedTotal)} detail="May be used by other profiles" />
      <Metric label="Disk footprint" value={formatBytes(cache.items.reduce((total, item) => total + item.files.totalSizeBytes, 0))} detail="Linked artwork and metadata shown here" />
    </div>
    <GlassPane className="profile-data-card profile-data-cache-note" spotlight={false}>
      <header><p>SHARED OWNERSHIP</p><h2>TMDB cache is not profile-owned</h2><span>Removing one profile must never remove these catalog rows or files. This view explains why each shared title is relevant to the selected profile.</span></header>
    </GlassPane>
    <DataTable headers={["Title", "Profile association", "State", "Poster", "Backdrop", "Metadata", "Disk size"]}>
      {cache.items.map((item) => <tr key={item.movie.id}>
        <td><MediaTitle media={item.movie} /></td>
        <td>{item.associationSources.join(", ")}</td>
        <td>{item.lastError ? `Error: ${item.lastError}` : item.cacheState ?? "Unknown"}</td>
        <td>{item.files.poster.storedOnDisk ? "On disk" : "Remote or missing"}</td>
        <td>{item.files.backdrop.storedOnDisk ? "On disk" : "Remote or missing"}</td>
        <td>{item.files.metadata.storedOnDisk ? "On disk" : "Missing"}</td>
        <td>{formatBytes(item.files.totalSizeBytes)}</td>
      </tr>)}
    </DataTable>
    {!cache.items.length && <Empty label="No shared TMDB cache entries are currently associated with this profile." />}
  </div>;
}

function Activity({ data }: { data: AdminProfileData }) {
  return <div className="profile-data-stack">
    <SectionHeading eyebrow="INTERACTION TELEMETRY" title={`${data.activity.total} stored events`} description="Only accepted server-side interaction signals are shown. Failed browser deliveries do not appear until acknowledged by the server." />
    <DataTable headers={["Event", "Title", "Recorded"]}>
      {data.activity.events.map((event) => <tr key={event.id}><td>{words(event.type)}</td><td><MediaTitle media={event.movie} fallback={event.tmdbId ? `TMDB ${event.tmdbId}` : "No title"} /></td><td>{formatDate(event.timestamp)}</td></tr>)}
    </DataTable>
    <SectionHeading eyebrow="RECOMMENDATION EXPOSURES" title={`${data.activity.exposures.length} recent exposures`} description="Visibility-qualified recommendation impressions retained by the server." />
    <DataTable headers={["Title", "Surface", "Scope", "Category", "Position", "Shown"]}>
      {data.activity.exposures.map((item) => <tr key={item.id}><td><MediaTitle media={item.movie} /></td><td>{item.surface}</td><td>{item.scope}</td><td>{item.category}</td><td>{item.position + 1}</td><td>{formatDate(item.shownAt)}</td></tr>)}
    </DataTable>
    <SectionHeading eyebrow="PLAYBACK RUNS" title={`${data.activity.playbackRuns.length} recent runs`} description="Run lifecycle records support playback integrity and troubleshooting." />
    <DataTable headers={["Title", "State", "Played", "Last seen"]}>
      {data.activity.playbackRuns.map((run) => <tr key={run.id}><td><MediaTitle media={run.movie} /></td><td>{words(run.state)}</td><td>{formatDuration(run.secondsPlayed)}</td><td>{formatDate(run.lastSeenAt)}</td></tr>)}
    </DataTable>
  </div>;
}

function Metric({ label, value, detail }: { label: string; value: string; detail: string }) {
  return <GlassPane className="profile-data-metric" spotlight={false}><p>{label}</p><strong>{value}</strong><span>{detail}</span></GlassPane>;
}

function SectionHeading({ eyebrow, title, description }: { eyebrow: string; title: string; description: string }) {
  return <header className="profile-data-section-heading"><p>{eyebrow}</p><h2>{title}</h2><span>{description}</span></header>;
}

function DataTable({ headers, children }: { headers: string[]; children: React.ReactNode }) {
  return <GlassPane className="profile-data-table-card" spotlight={false}><div className="profile-data-table-scroll"><table><thead><tr>{headers.map((header) => <th key={header}>{header}</th>)}</tr></thead><tbody>{children}</tbody></table></div></GlassPane>;
}

function MediaTitle({ media, fallback = "Missing catalog record" }: { media: AdminMediaBrief | null; fallback?: string }) {
  return <span className="profile-data-media-title"><strong>{media?.title ?? fallback}</strong>{media && <small>{media.releaseYear || "Year unknown"} · {media.type}</small>}</span>;
}

function Empty({ label }: { label: string }) {
  return <GlassPane className="profile-data-empty" spotlight={false}><p>{label}</p></GlassPane>;
}

function formatDate(value: number | string, compact = false) {
  const date = typeof value === "number" ? new Date(value * 1000) : new Date(value);
  if (Number.isNaN(date.getTime())) return "Unknown";
  return new Intl.DateTimeFormat(undefined, compact ? { dateStyle: "medium" } : { dateStyle: "medium", timeStyle: "short" }).format(date);
}

function formatDuration(seconds: number) {
  const value = Math.max(0, Math.round(seconds || 0));
  const hours = Math.floor(value / 3600);
  const minutes = Math.floor((value % 3600) / 60);
  if (hours) return `${hours}h ${minutes}m`;
  if (minutes) return `${minutes}m`;
  return `${value}s`;
}

function formatBytes(bytes: number) {
  if (!bytes) return "0 B";
  const units = ["B", "KB", "MB", "GB"];
  const index = Math.min(units.length - 1, Math.floor(Math.log(bytes) / Math.log(1024)));
  return `${(bytes / 1024 ** index).toFixed(index ? 1 : 0)} ${units[index]}`;
}

function percent(value: number) {
  return `${Math.round(Math.max(0, value || 0) * 100)}%`;
}

function words(value: string) {
  return value.replace(/([a-z])([A-Z])/g, "$1 $2").replace(/_/g, " ");
}
