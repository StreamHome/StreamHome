import React, { useCallback, useEffect, useRef, useState } from "react";
import { formatStorageBytes } from "../../../utils/format";
import { useLocation, useNavigate } from "react-router-dom";
import { appUrl, parseAppQuery, type AdminDataCategory } from "../../../navigation/queryState";
import { getAdminProfileData } from "../../../api/adminProfiles";
import { ApiError } from "../../../api/client";
import { Button } from "../../../components/ui/Button";
import { GlassPane } from "../../../components/ui/GlassPane";
import { AdminSelect } from "../../../components/ui/AdminSelect";
import type { AdminMediaBrief, AdminProfileData } from "../../../types/api";
import { AdminHelp } from "../AdminHelp";
import { SudoModal } from "../SudoModal";

type DataTab = AdminDataCategory;

const TABS: Array<{ id: DataTab; label: string }> = [
  { id: "overview", label: "Overview" },
  { id: "history", label: "History" },
  { id: "watchlist", label: "Watchlist" },
  { id: "recommendations", label: "Recommendation data" },
  { id: "cache", label: "TMDB cache" },
  { id: "activity", label: "Activity" },
];

export function ProfileDataPanel({ profileId }: { profileId: string }) {
  // A new inspection subject owns its own reads, dialogs and transient errors.
  return <ProfileDataReader key={profileId} profileId={profileId} />;
}

function ProfileDataReader({ profileId }: { profileId: string }) {
  const [data, setData] = useState<AdminProfileData | null>(null);
  const location = useLocation();
  const navigate = useNavigate();
  const query = parseAppQuery(location.search);
  const tab = query.adminCategory ?? "overview";
  const setTab = (category: DataTab) => navigate(appUrl(query.profile, "admin", { section: "profiles", adminProfile: profileId, adminCategory: category }));
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [reauthorizationRequired, setReauthorizationRequired] = useState(false);
  const [sudoOpen, setSudoOpen] = useState(false);
  const active = useRef(false);
  const requestId = useRef(0);
  const pendingRead = useRef<AbortController | null>(null);

  const load = useCallback(async () => {
    if (!active.current) return;
    pendingRead.current?.abort();
    const controller = new AbortController();
    pendingRead.current = controller;
    const request = ++requestId.current;
    const current = () => active.current && request === requestId.current && !controller.signal.aborted;
    setLoading(true);
    setError("");
    try {
      const next = await getAdminProfileData(profileId, controller.signal);
      if (!current()) return;
      setData(next);
      setReauthorizationRequired(false);
    } catch (requestError) {
      if (!current()) return;
      if (requestError instanceof ApiError && requestError.code === "reauthentication_required") {
        setReauthorizationRequired(true);
        setSudoOpen(true);
        setError("Your recent administrator authorization expired. Confirm your identity to inspect this profile’s records.");
      } else {
        setError(requestError instanceof Error ? requestError.message : "Profile data could not be loaded.");
      }
    } finally {
      if (current()) setLoading(false);
    }
  }, [profileId]);

  useEffect(() => {
    active.current = true;
    void load();
    return () => { active.current = false; requestId.current += 1; pendingRead.current?.abort(); };
  }, [load]);

  const reauthorized = async () => {
    if (!active.current) return;
    setSudoOpen(false);
    // Confirmation only retries this read. It never changes a profile or record.
    await load();
  };

  return <section className="admin-panel admin-panel--profile-data">
    <header className="admin-panel__header">
      <p>PROFILE / DATA EXPLORER</p>
      <h1>Profile data</h1>
      <span>Inspect viewing, saved titles and recommendation activity for the selected profile.</span>
    </header>
    <div className="profile-data-category-select admin-control"><AdminSelect label="Data category" value={tab} onChange={(value) => setTab(value as DataTab)} options={TABS.map((item) => ({ value: item.id, label: item.label }))} /></div>
    <nav className="profile-data-tabs" aria-label="Profile data categories">
      {TABS.map((item) => <button key={item.id} type="button" data-active={tab === item.id} aria-current={tab === item.id ? "page" : undefined} onClick={() => setTab(item.id)}>{item.label}</button>)}
    </nav>
    {loading && !data ? <GlassPane className="admin-state-card" spotlight={false}><p>READING PROFILE DATA</p><h2>Loading profile records…</h2></GlassPane> : error && !data ? <GlassPane className="admin-state-card" spotlight={false}><p>{reauthorizationRequired ? "CONFIRM ACCESS" : "PROFILE DATA UNAVAILABLE"}</p><h2>{reauthorizationRequired ? "Confirm access to profile records" : "The selected profile could not be inspected."}</h2><div><span role="alert">{error}</span></div><Button onClick={() => reauthorizationRequired ? setSudoOpen(true) : void load()}>{reauthorizationRequired ? "Confirm identity" : "Try again"}</Button></GlassPane> : data && <>
      {tab === "overview" && <Overview data={data} />}
      {tab === "history" && <History data={data} />}
      {tab === "watchlist" && <Watchlist data={data} />}
      {tab === "recommendations" && <Recommendations data={data} />}
      {tab === "cache" && <Cache data={data} />}
      {tab === "activity" && <Activity data={data} />}
      {error && <div><p className="admin-form-message admin-form-message--error" role="alert">{error}</p>{reauthorizationRequired && <Button onClick={() => setSudoOpen(true)}>Confirm identity</Button>}</div>}
    </>}
    <SudoModal isOpen={sudoOpen} actionLabel="Inspect profile data" onCancel={() => setSudoOpen(false)} onSuccess={reauthorized} />
  </section>;
}

function Overview({ data }: { data: AdminProfileData }) {
  const { overview } = data;
  return <div className="profile-data-stack">
    <div className="profile-data-metrics" aria-label="Profile data summary">
      <Metric label="Watch time" value={formatDuration(overview.watchSeconds)} detail={countLabel(overview.counts.history, "viewing attempt", "viewing attempts")} />
      <Metric label="Watchlist" value={String(overview.counts.watchlist)} detail="Saved titles" />
      <Metric label="Recommendations" value={String(overview.counts.recommendations)} detail={countLabel(overview.counts.preferences, "explicit choice", "explicit choices")} />
      <Metric label="Last activity" value={overview.lastActivityAt ? formatDate(overview.lastActivityAt, true) : "Never"} detail={countLabel(overview.completedTitles, "completed attempt", "completed attempts")} />
    </div>
    <AdminHelp label="Stored records and data locations" title="Profile records and data locations" variant="details" description="See which records belong to this profile and where its data is stored.">
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
    </AdminHelp>
  </div>;
}

function History({ data }: { data: AdminProfileData }) {
  return <div className="profile-data-stack">
    <SectionHeading eyebrow="VIEWING ATTEMPTS" title={countLabel(data.history.total, "history record", "history records")} description="Attempts are the chronological history used by recommendations. Resume states are the latest playback positions shown in Continue Watching." />
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
    <SectionHeading eyebrow="CONTINUE WATCHING" title={countLabel(data.history.resumeStates.length, "resume state", "resume states")} description="These rows hold the most recent playback position for each movie or episode." />
    <DataTable headers={["Title", "Position", "Completion", "Updated", "State"]}>
      {data.history.resumeStates.map((item) => <tr key={`${item.movieId}:${item.episodeId ?? "movie"}`}>
        <td><MediaTitle media={item.movie} fallback={item.movieId} /></td>
        <td>{formatDuration(item.timestamp)}</td>
        <td>{percent(item.completionRate)}</td>
        <td>{formatDate(item.updatedAt)}</td>
        <td>{item.finished ? "Finished" : "Resume available"}</td>
      </tr>)}
    </DataTable>
    {!data.history.resumeStates.length && <Empty label="No playback positions have been recorded. Resume states appear after playback starts." />}
  </div>;
}

function Watchlist({ data }: { data: AdminProfileData }) {
  return <div className="profile-data-stack">
    <SectionHeading eyebrow="SAVED TITLES" title={countLabel(data.watchlist.total, "watchlist entry", "watchlist entries")} description="Titles saved by this profile, in the order returned by the server." />
    <DataTable headers={["Title", "Type", "Availability", "Added"]}>
      {data.watchlist.items.map((item) => <tr key={item.id}>
        <td><MediaTitle media={item.movie} /></td>
        <td>{displayLabel(item.movie?.type ?? "Unknown")}</td>
        <td>{displayLabel(item.movie?.availability ?? "Missing catalog record")}</td>
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
      <Metric label="Candidate pool" value={String(recommendations.total)} detail={countLabel(recommendations.preferences.length, "explicit choice", "explicit choices")} />
      <Metric label="Learned tastes" value={String(recommendations.tastes.length)} detail={countLabel(recommendations.onboarding.genres.length, "starting genre", "starting genres")} />
      <Metric label="Preference version" value={recommendations.refresh ? String(recommendations.refresh.tasteVersion) : "—"} detail={!recommendations.refresh ? "No refresh status recorded" : recommendations.refresh.refreshRequested ? "Refresh requested" : "Pool current"} />
      <Metric label="Ranking overlap" value={recommendations.runtimeMetric ? String(recommendations.runtimeMetric.top20Overlap) : "—"} detail={recommendations.runtimeMetric ? `Average position change: ${recommendations.runtimeMetric.meanDisplacement}` : "No comparison recorded"} />
    </div>
    <GlassPane className="profile-data-card" spotlight={false}>
      <header><p>LEARNED SIGNALS</p><h2>Tastes and starting choices</h2></header>
      <div className="profile-data-signal-grid">
        <section><strong>Starting genres</strong><p>{recommendations.onboarding.genres.map(displayLabel).join(", ") || "None"}</p></section>
        <section><strong>Top learned tastes</strong>{!recommendations.tastes.length && <p>No learned tastes yet. Viewing and explicit choices contribute signals.</p>}<div>{recommendations.tastes.slice(0, 12).map((taste) => <span key={`${taste.kind}:${taste.value}`}><b>{taste.value}</b><i>{taste.score.toFixed(2)}</i></span>)}</div></section>
      </div>
    </GlassPane>
    <SectionHeading eyebrow="RECOMMENDATION TITLES" title={countLabel(recommendations.total, "ranked title", "ranked titles")} description="Saved recommendation choices for this profile. Their order can change when the profile opens a page. Score is a ranking weight, not a percentage; confidence describes the source." />
    <DataTable headers={["Title", "Score", "Why", "Source", "Confidence", "Feedback"]}>
      {recommendations.items.map((item, index) => <tr key={`${item.movie?.id ?? "missing"}:${index}`}>
        <td><MediaTitle media={item.movie} /></td>
        <td>{item.score.toFixed(3)}</td>
        <td>{item.reasons.join(" · ") || "No reason stored"}</td>
        <td>{displayLabel(item.candidateSource)}</td>
        <td>{percent(item.sourceConfidence)}</td>
        <td>{displayLabel(item.preference ?? "None")}</td>
      </tr>)}
    </DataTable>
    {!recommendations.items.length && <Empty label="No recommendation candidates have been generated for this profile yet." />}
  </div>;
}

function Cache({ data }: { data: AdminProfileData }) {
  const cache = data.cache;
  return <div className="profile-data-stack">
    <div className="profile-data-metrics">
      <Metric label="Linked to profile" value={String(cache.total)} detail="Through recommendations or activity" />
      <Metric label="Shared TMDB cache" value={String(cache.sharedCacheTotal)} detail="Server-wide metadata records" />
      <Metric label="Unreferenced here" value={String(cache.unreferencedSharedTotal)} detail="May be used by other profiles" />
      <Metric label="Disk footprint" value={formatStorageBytes(cache.items.reduce((total, item) => total + item.files.totalSizeBytes, 0))} detail="Linked artwork and metadata shown here" />
    </div>
    <GlassPane className="profile-data-card profile-data-cache-note" spotlight={false}>
      <header><p>SHARED OWNERSHIP</p><h2>Shared catalog artwork and metadata</h2><span>Other profiles can use these records too. Removing a profile keeps this shared artwork and metadata. This view shows why each title is linked to the selected profile.</span></header>
    </GlassPane>
    <DataTable headers={["Title", "Profile association", "State", "Poster", "Backdrop", "Metadata", "Disk size"]}>
      {cache.items.map((item) => <tr key={item.movie.id}>
        <td><MediaTitle media={item.movie} /></td>
        <td>{item.associationSources.map(displayLabel).join(", ")}</td>
        <td>{item.lastError ? `Error: ${item.lastError}` : displayLabel(item.cacheState ?? "Unknown")}</td>
        <td>{item.files.poster.storedOnDisk ? "On disk" : "Remote or missing"}</td>
        <td>{item.files.backdrop.storedOnDisk ? "On disk" : "Remote or missing"}</td>
        <td>{item.files.metadata.storedOnDisk ? "On disk" : "Missing"}</td>
        <td>{formatStorageBytes(item.files.totalSizeBytes)}</td>
      </tr>)}
    </DataTable>
    {!cache.items.length && <Empty label="No shared TMDB cache entries are currently associated with this profile." />}
  </div>;
}

function Activity({ data }: { data: AdminProfileData }) {
  return <div className="profile-data-stack">
    <SectionHeading eyebrow="INTERACTION TELEMETRY" title={countLabel(data.activity.total, "stored event", "stored events")} description="Activity received by the server for this profile. Events that could not be delivered are not shown." />
    <DataTable headers={["Event", "Title", "Recorded"]}>
      {data.activity.events.map((event) => <tr key={event.id}><td>{displayLabel(event.type)}</td><td><MediaTitle media={event.movie} fallback={event.tmdbId ? `TMDB ${event.tmdbId}` : "No title"} /></td><td>{formatDate(event.timestamp)}</td></tr>)}
    </DataTable>
    {!data.activity.events.length && <Empty label="No interaction events have been recorded for this profile." />}
    <SectionHeading eyebrow="RECOMMENDATION EXPOSURES" title={countLabel(data.activity.exposures.length, "recent exposure", "recent exposures")} description="Visibility-qualified recommendation impressions retained by the server." />
    <DataTable headers={["Title", "Surface", "Scope", "Category", "Position", "Shown"]}>
      {data.activity.exposures.map((item) => <tr key={item.id}><td><MediaTitle media={item.movie} /></td><td>{displayLabel(item.surface)}</td><td>{displayLabel(item.scope)}</td><td>{displayLabel(item.category)}</td><td>{item.position + 1}</td><td>{formatDate(item.shownAt)}</td></tr>)}
    </DataTable>
    {!data.activity.exposures.length && <Empty label="No recommendation exposures have been recorded yet." />}
    <SectionHeading eyebrow="PLAYBACK RUNS" title={countLabel(data.activity.playbackRuns.length, "recent run", "recent runs")} description="Run lifecycle records support playback integrity and troubleshooting." />
    <DataTable headers={["Title", "State", "Played", "Last seen"]}>
      {data.activity.playbackRuns.map((run) => <tr key={run.id}><td><MediaTitle media={run.movie} /></td><td>{displayLabel(run.state)}</td><td>{formatDuration(run.secondsPlayed)}</td><td>{formatDate(run.lastSeenAt)}</td></tr>)}
    </DataTable>
    {!data.activity.playbackRuns.length && <Empty label="No playback runs have been recorded for this profile." />}
  </div>;
}

function Metric({ label, value, detail }: { label: string; value: string; detail: string }) {
  return <GlassPane className="profile-data-metric" spotlight={false}><p>{label}</p><strong>{value}</strong><span>{detail}</span></GlassPane>;
}

function SectionHeading({ eyebrow, title, description }: { eyebrow: string; title: string; description: string }) {
  return <header className="profile-data-section-heading"><p>{eyebrow}</p><h2>{title}</h2><span>{description}</span></header>;
}

function DataTable({ headers, children }: { headers: string[]; children: React.ReactNode }) {
  const rows = React.Children.toArray(children).filter(React.isValidElement);
  if (!rows.length) return null;
  const labeledRows = rows.map((row) => {
    const element = row as React.ReactElement<{ children: React.ReactNode; role?: string }>;
    return React.cloneElement(element, { role: "row" }, React.Children.map(element.props.children, (cell, index) => {
      if (!React.isValidElement(cell)) return cell;
      const dataCell = cell as React.ReactElement<{ children: React.ReactNode; "data-label"?: string; role?: string }>;
      return React.cloneElement(dataCell, { "data-label": headers[index], role: "cell" }, <><span className="profile-data-cell-label" aria-hidden="true">{headers[index]}</span><div className="profile-data-cell-value">{dataCell.props.children}</div></>);
    }));
  });
  return <GlassPane className="profile-data-table-card" spotlight={false}><div className="profile-data-table-scroll" tabIndex={0} role="region" aria-label={`${headers.join(", ")} table`}><table role="table"><thead role="rowgroup"><tr role="row">{headers.map((header) => <th role="columnheader" scope="col" key={header}>{header}</th>)}</tr></thead><tbody role="rowgroup">{labeledRows}</tbody></table></div></GlassPane>;
}

function MediaTitle({ media, fallback = "Missing catalog record" }: { media: AdminMediaBrief | null; fallback?: string }) {
  return <span className="profile-data-media-title"><strong>{media?.title ?? fallback}</strong>{media && <small>{media.releaseYear || "Year unknown"} · {displayLabel(media.type)}</small>}</span>;
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

function percent(value: number) {
  return `${Math.round(Math.max(0, value || 0) * 100)}%`;
}

function words(value: string) {
  return value.replace(/([a-z])([A-Z])/g, "$1 $2").replace(/_/g, " ").replace(/\btmdb\b/gi, "TMDB");
}

function countLabel(count: number, singular: string, plural: string) { return `${count} ${count === 1 ? singular : plural}`; }

function displayLabel(value: string) { const label = words(value); return label ? label[0].toUpperCase() + label.slice(1) : "Unknown"; }
