import React, { useCallback, useEffect, useRef, useState } from "react";
import { clearMediaPreferences, getRecommendationDiagnostics, getRecommendationOnboarding, rebuildRecommendations, saveRecommendationOnboarding } from "../../../api/recommendations";
import { Button } from "../../../components/ui/Button";
import { GlassPane } from "../../../components/ui/GlassPane";
import { useProfileStore } from "../../../stores/profileStore";
import type { Profile, RecommendationDiagnostics } from "../../../types/api";
import { AdminHelp } from "../AdminHelp";

type DraftProps = { draft?: string; onDraftChange?: (draft: string | undefined) => void };
export function RecommendationsPanel({ profileId, ...draftProps }: { profileId?: string } & DraftProps = {}) {
  const activeProfile = useProfileStore((state) => state.activeProfile)!;
  const profiles = useProfileStore((state) => state.profiles);
  const profile = profiles.find((item) => item.id === profileId) ?? activeProfile;
  return <ProfileRecommendationsPanel key={profile.id} profile={profile} {...draftProps} />;
}

function ProfileRecommendationsPanel({ profile, draft, onDraftChange }: { profile: Profile } & DraftProps) {
  const retainedDraft = useRef(draft);
  const [diagnostics, setDiagnostics] = useState<RecommendationDiagnostics | null>(null);
  const [genres, setGenres] = useState("");
  const [savedGenres, setSavedGenres] = useState<string[]>([]);
  const [loading, setLoading] = useState(true);
  const [working, setWorking] = useState(false);
  const [error, setError] = useState("");
  const [message, setMessage] = useState("");
  const [confirmName, setConfirmName] = useState("");
  const active = useRef(true);
  const loadRequest = useRef(0);
  useEffect(() => {
    active.current = true;
    return () => { active.current = false; loadRequest.current += 1; };
  }, []);

  const load = useCallback(async (preserveDraft = false) => {
    const request = ++loadRequest.current;
    setLoading(true); setError("");
    try {
      const [nextDiagnostics, onboarding] = await Promise.all([getRecommendationDiagnostics(profile.id), getRecommendationOnboarding(profile.id)]);
      if (!active.current || request !== loadRequest.current) return;
      setDiagnostics(nextDiagnostics);
      setSavedGenres(onboarding.genres);
      if (!preserveDraft) setGenres(retainedDraft.current ?? onboarding.genres.join(", "));
    } catch (requestError) {
      if (active.current && request === loadRequest.current) setError(requestError instanceof Error ? requestError.message : "Recommendation diagnostics could not be loaded.");
    } finally { if (active.current && request === loadRequest.current) setLoading(false); }
  }, [profile.id]);

  useEffect(() => { void load(); }, [load]);

  const run = async (action: () => Promise<unknown>, success: string, preserveDraft = true) => {
    setWorking(true); setError(""); setMessage("");
    try { await action(); if (!active.current) return; if (!preserveDraft) { retainedDraft.current = undefined; onDraftChange?.(undefined); } setMessage(success); await load(preserveDraft); }
    catch (requestError) { if (active.current) setError(requestError instanceof Error ? requestError.message : "The recommendation action failed."); }
    finally { if (active.current) setWorking(false); }
  };

  const normalizedGenres = Array.from(new Set(genres.split(",").map((genre) => genre.trim()).filter(Boolean)));
  const genresDirty = JSON.stringify(normalizedGenres) !== JSON.stringify(savedGenres);
  const tooManyGenres = normalizedGenres.length > 12;
  const editGenres = (value: string) => { setGenres(value); retainedDraft.current = value; onDraftChange?.(value); setMessage(""); };

  return <section className="admin-panel admin-panel--recommendations">
    <header className="admin-panel__header"><p>PROFILE / RECOMMENDATION ENGINE</p><h1>Recommendations</h1><span>Manage {profile.name}’s starting genres and recommendation feed.</span></header>
    {loading && !diagnostics ? <GlassPane className="admin-state-card" spotlight={false}><p>READING SIGNALS</p><h2>Loading recommendation diagnostics…</h2></GlassPane> : error && !diagnostics ? <GlassPane className="admin-state-card" spotlight={false}><p>DIAGNOSTICS UNAVAILABLE</p><h2>Recommendation data could not be loaded.</h2><span role="alert">{error}</span><Button onClick={() => void load()}>Try again</Button></GlassPane> : diagnostics && <>
      <GlassPane className="admin-settings-card recommendation-admin-card" spotlight={false}>
        <h2 className="recommendation-editor-title">Starting genres</h2>
        <label className="admin-control recommendation-onboarding"><span>Starting genres</span><input aria-label="Starting genres" disabled={loading || working} value={genres} aria-invalid={tooManyGenres || undefined} aria-describedby="starting-genres-help" onChange={(event) => editGenres(event.target.value)} placeholder="Action, Science Fiction, Drama" /><small id="starting-genres-help">Use comma-separated names, up to 12 distinct genres. {normalizedGenres.length}/12 selected. {genresDirty ? "Unsaved changes." : "No unsaved changes."}</small></label>
        {genresDirty && <p className="admin-draft-help">This profile’s draft is kept while you browse admin sections. Leaving Admin Center discards it.</p>}
        {tooManyGenres && <p className="admin-form-message admin-form-message--error" role="alert">Remove {normalizedGenres.length - 12} genre{normalizedGenres.length - 12 === 1 ? "" : "s"} before saving. No entries will be discarded automatically.</p>}
        {normalizedGenres.length > 0 && <ul className="recommendation-genre-preview" aria-label="Selected starting genres">{normalizedGenres.map((genre) => <li key={genre}><span>{genre}</span><button type="button" aria-label={`Remove ${genre}`} disabled={working || loading} onClick={() => editGenres(normalizedGenres.filter((item) => item !== genre).join(", "))}>×</button></li>)}</ul>}
        {genresDirty && <Button variant="ghost" disabled={working || loading} onClick={() => { setGenres(savedGenres.join(", ")); retainedDraft.current = undefined; onDraftChange?.(undefined); }}>Discard genre changes</Button>}
        <p className="admin-action-subject">Editing profile: <strong>{profile.name}</strong></p>
        <div className="recommendation-admin-actions"><Button disabled={working || loading || !genresDirty || tooManyGenres} onClick={() => void run(() => saveRecommendationOnboarding(profile.id, normalizedGenres), "Starting preferences saved.", false)}>Save starting genres</Button><div className="recommendation-rebuild-action"><Button variant="secondary" disabled={working || loading} aria-describedby="rebuild-draft-help" onClick={() => void run(() => rebuildRecommendations(profile.id), "Recommendation pool rebuilt.")}>Rebuild feed</Button><small id="rebuild-draft-help">{genresDirty ? "Uses saved genres. Your unsaved draft will not be saved." : "Uses saved genres and signals."}</small></div></div>
        <p className="recommendation-rebuild-help">Rebuild refreshes this profile’s candidate pool using its saved preferences and signals; it does not save the genre draft above or change media, history or watchlists.{diagnostics.catalog.available + diagnostics.catalog.cached === 0 ? " No local or cached titles are currently available, so the pool may remain empty." : ""}</p>
        <AdminHelp label="Recommendation signals and diagnostics" variant="details" description="Inspect this profile’s explicit choices, candidate sources and learned tastes."><div className="recommendation-admin-columns">
          <section><p>EXPLICIT FEEDBACK</p><h2>{diagnostics.preferences.like} liked · {diagnostics.preferences.love} loved · {diagnostics.preferences.dislike} disliked</h2><span>Explicit choices outweigh inferred viewing behavior. Disliked titles stay searchable and playable but leave personalized feeds.</span></section>
          <section><p>CANDIDATE SOURCES</p><div className="recommendation-signal-list">{Object.entries(diagnostics.candidateSources).length ? Object.entries(diagnostics.candidateSources).map(([source, count]) => <span key={source}><strong>{source.replace(/_/g, " ")}</strong><i>{count}</i></span>) : <span><strong>No generated candidates</strong><i>0</i></span>}</div></section>
          <section><p>TOP TASTE SIGNALS</p><div className="recommendation-signal-list">{diagnostics.topTastes.length ? diagnostics.topTastes.slice(0, 8).map((taste) => <span key={`${taste.kind}:${taste.value}`}><strong>{taste.value}</strong><i>{taste.score.toFixed(2)}</i></span>) : <span><strong>No learned signals yet</strong><i>0</i></span>}</div></section>
        </div>
        </AdminHelp>
      </GlassPane>
      <div className="recommendation-diagnostics-grid" aria-label="Recommendation metrics">
        <Metric label="Exposures" value={diagnostics.exposures} detail={`${diagnostics.periodDays} day window`} />
        <Metric label="Detail opens" value={diagnostics.detailsOpens} detail={`${diagnostics.periodDays} day window`} />
        <Metric label="Playback starts" value={diagnostics.playbackStarts} detail={`${percent(diagnostics.playRate)} play rate · ${diagnostics.completions} completions`} />
        <Metric label="Candidate pool" value={diagnostics.candidatePool} detail={`${diagnostics.catalog.available} local / ${diagnostics.catalog.cached} cached`} />
      </div>
      <GlassPane className="admin-settings-card recommendation-danger-card" spotlight={false}><p>RESET EXPLICIT FEEDBACK</p><h2>Clear Like, Love, and Dislike choices</h2><span>This removes only explicit preference rows. Viewing history, Watch Again order, watchlists, and media remain unchanged.</span><p className="admin-action-subject">Editing profile: <strong>{profile.name}</strong></p><label className="admin-control"><span>Type {profile.name} to confirm</span><input disabled={loading || working} value={confirmName} onChange={(event) => setConfirmName(event.target.value)} /></label><Button variant="secondary" disabled={working || loading || confirmName !== profile.name} onClick={() => void run(() => clearMediaPreferences(profile.id), "Explicit feedback cleared.")}>Clear explicit feedback</Button></GlassPane>
      {(message || error) && <p className={`admin-form-message ${error ? "admin-form-message--error" : "admin-form-message--success"}`} role={error ? "alert" : "status"}>{error || message}</p>}
    </>}
  </section>;
}

function Metric({ label, value, detail }: { label: string; value: number; detail: string }) {
  return <GlassPane className="recommendation-metric" spotlight={false}><p>{label}</p><strong>{value}</strong><span>{detail}</span></GlassPane>;
}

function percent(value: number) { return `${Math.round(value * 100)}%`; }
