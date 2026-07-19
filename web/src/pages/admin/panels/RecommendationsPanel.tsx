import React, { useCallback, useEffect, useState } from "react";
import { clearMediaPreferences, getRecommendationDiagnostics, getRecommendationOnboarding, rebuildRecommendations, saveRecommendationOnboarding } from "../../../api/recommendations";
import { Button } from "../../../components/ui/Button";
import { GlassPane } from "../../../components/ui/GlassPane";
import { useProfileStore } from "../../../stores/profileStore";
import type { RecommendationDiagnostics } from "../../../types/api";

export function RecommendationsPanel() {
  const profile = useProfileStore((state) => state.activeProfile)!;
  const [diagnostics, setDiagnostics] = useState<RecommendationDiagnostics | null>(null);
  const [genres, setGenres] = useState("");
  const [loading, setLoading] = useState(true);
  const [working, setWorking] = useState(false);
  const [error, setError] = useState("");
  const [message, setMessage] = useState("");
  const [confirmName, setConfirmName] = useState("");

  const load = useCallback(async () => {
    setLoading(true); setError("");
    try {
      const [nextDiagnostics, onboarding] = await Promise.all([getRecommendationDiagnostics(profile.id), getRecommendationOnboarding(profile.id)]);
      setDiagnostics(nextDiagnostics);
      setGenres(onboarding.genres.join(", "));
    } catch (requestError) {
      setError(requestError instanceof Error ? requestError.message : "Recommendation diagnostics could not be loaded.");
    } finally { setLoading(false); }
  }, [profile.id]);

  useEffect(() => { void load(); }, [load]);

  const run = async (action: () => Promise<unknown>, success: string) => {
    setWorking(true); setError(""); setMessage("");
    try { await action(); setMessage(success); await load(); }
    catch (requestError) { setError(requestError instanceof Error ? requestError.message : "The recommendation action failed."); }
    finally { setWorking(false); }
  };

  const normalizedGenres = () => Array.from(new Set(genres.split(",").map((genre) => genre.trim()).filter(Boolean))).slice(0, 12);

  return <section className="admin-panel admin-panel--recommendations">
    <header className="admin-panel__header"><p>PROFILE / RECOMMENDATION ENGINE</p><h1>Recommendations</h1><span>Inspect the active profile’s signals, tune its cold-start genres, and rebuild the server candidate pool. Search results are not taste signals; only a selected result contributes a modest signal.</span></header>
    {loading && !diagnostics ? <GlassPane className="admin-state-card" spotlight={false}><p>READING SIGNALS</p><h2>Loading recommendation diagnostics…</h2></GlassPane> : error && !diagnostics ? <GlassPane className="admin-state-card" spotlight={false}><p>DIAGNOSTICS UNAVAILABLE</p><h2>Recommendation data could not be loaded.</h2><span role="alert">{error}</span><Button onClick={() => void load()}>Try again</Button></GlassPane> : diagnostics && <>
      <div className="recommendation-diagnostics-grid" aria-label="Recommendation metrics">
        <Metric label="Exposures" value={diagnostics.exposures} detail={`${diagnostics.periodDays} day window`} />
        <Metric label="Detail opens" value={diagnostics.detailsOpens} detail={`${percent(diagnostics.playRate)} play rate`} />
        <Metric label="Playback starts" value={diagnostics.playbackStarts} detail={`${diagnostics.completions} completions`} />
        <Metric label="Candidate pool" value={diagnostics.candidatePool} detail={`${diagnostics.catalog.available} local / ${diagnostics.catalog.cached} cached`} />
      </div>
      <GlassPane className="admin-settings-card recommendation-admin-card" spotlight={false}>
        <div className="recommendation-admin-columns">
          <section><p>EXPLICIT FEEDBACK</p><h2>{diagnostics.preferences.like} liked · {diagnostics.preferences.love} loved · {diagnostics.preferences.dislike} disliked</h2><span>Explicit choices outweigh inferred viewing behavior. Disliked titles stay searchable and playable but leave personalized feeds.</span></section>
          <section><p>CANDIDATE SOURCES</p><div className="recommendation-signal-list">{Object.entries(diagnostics.candidateSources).length ? Object.entries(diagnostics.candidateSources).map(([source, count]) => <span key={source}><strong>{source.replace(/_/g, " ")}</strong><i>{count}</i></span>) : <span><strong>No generated candidates</strong><i>0</i></span>}</div></section>
          <section><p>TOP TASTE SIGNALS</p><div className="recommendation-signal-list">{diagnostics.topTastes.length ? diagnostics.topTastes.slice(0, 8).map((taste) => <span key={`${taste.kind}:${taste.value}`}><strong>{taste.value}</strong><i>{taste.score.toFixed(2)}</i></span>) : <span><strong>No learned signals yet</strong><i>0</i></span>}</div></section>
        </div>
        <label className="admin-control recommendation-onboarding"><span>Cold-start genres</span><input value={genres} onChange={(event) => setGenres(event.target.value)} placeholder="Action, Science Fiction, Drama" /><small>Comma-separated genres for new or sparse profiles. These can be edited at any time.</small></label>
        <div className="recommendation-admin-actions"><Button disabled={working} onClick={() => void run(() => saveRecommendationOnboarding(profile.id, normalizedGenres()), "Starting preferences saved.")}>Save starting genres</Button><Button variant="secondary" disabled={working} onClick={() => void run(() => rebuildRecommendations(profile.id), "Recommendation pool rebuilt.")}>Rebuild feed</Button></div>
      </GlassPane>
      <GlassPane className="admin-settings-card recommendation-danger-card" spotlight={false}><p>RESET EXPLICIT FEEDBACK</p><h2>Clear Like, Love, and Dislike choices</h2><span>This removes only explicit preference rows. Viewing history, Watch Again order, watchlists, and media remain unchanged.</span><label className="admin-control"><span>Type {profile.name} to confirm</span><input value={confirmName} onChange={(event) => setConfirmName(event.target.value)} /></label><Button variant="secondary" disabled={working || confirmName !== profile.name} onClick={() => void run(() => clearMediaPreferences(profile.id), "Explicit feedback cleared.")}>Clear explicit feedback</Button></GlassPane>
      {(message || error) && <p className={`admin-form-message ${error ? "admin-form-message--error" : "admin-form-message--success"}`} role={error ? "alert" : "status"}>{error || message}</p>}
    </>}
  </section>;
}

function Metric({ label, value, detail }: { label: string; value: number; detail: string }) {
  return <GlassPane className="recommendation-metric" spotlight={false}><p>{label}</p><strong>{value}</strong><span>{detail}</span></GlassPane>;
}

function percent(value: number) { return `${Math.round(value * 100)}%`; }
