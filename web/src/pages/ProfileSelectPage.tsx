import React, { useCallback, useEffect, useState } from "react";
import { useLocation, useNavigate } from "react-router-dom";
import { createProfile, getProfiles } from "../api/profiles";
import { Button } from "../components/ui/Button";
import { GlassPane } from "../components/ui/GlassPane";
import { useProfileStore } from "../stores/profileStore";
import { useThemeStore } from "../stores/themeStore";
import type { Profile } from "../types/api";
import type { ThemeId } from "../types/theme";
import { avatarBackground, normalizeTheme } from "../utils/media";

const THEMES: ThemeId[] = ["ember", "aurora", "cinema", "gemini"];

export function ProfileSelectPage() {
  const navigate = useNavigate();
  const location = useLocation();
  const profiles = useProfileStore((state) => state.profiles);
  const restoreProfile = useProfileStore((state) => state.restoreProfile);
  const selectProfile = useProfileStore((state) => state.selectProfile);
  const setTheme = useThemeStore((state) => state.setTheme);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [showCreate, setShowCreate] = useState(false);
  const [name, setName] = useState("");
  const [theme, setNewTheme] = useState<ThemeId>("ember");
  const [saving, setSaving] = useState(false);

  const loadProfiles = useCallback(async () => {
    setLoading(true);
    setError("");
    try {
      const data = await getProfiles();
      const restored = restoreProfile(data);
      const redirectedFromGuard = Boolean((location.state as { from?: unknown } | null)?.from);
      if (restored && redirectedFromGuard) {
        setTheme(restored.theme);
        navigate("/", { replace: true });
      }
    } catch (requestError) {
      setError(requestError instanceof Error ? requestError.message : "Profiles could not be loaded.");
    } finally {
      setLoading(false);
    }
  }, [location.state, navigate, restoreProfile, setTheme]);

  useEffect(() => { void loadProfiles(); }, [loadProfiles]);

  const chooseProfile = (profile: Profile) => {
    selectProfile(profile);
    setTheme(profile.theme);
    navigate("/");
  };

  const submitProfile = async (event: React.FormEvent) => {
    event.preventDefault();
    if (!name.trim()) return;
    setSaving(true);
    setError("");
    try {
      const created = await createProfile({
        id: crypto.randomUUID(),
        name: name.trim(),
        theme,
        avatarColor: "#2563eb",
        pinEnabled: false,
      });
      const next = [...profiles, created];
      restoreProfile(next);
      setShowCreate(false);
      setName("");
    } catch (requestError) {
      setError(requestError instanceof Error ? requestError.message : "Profile could not be created.");
    } finally {
      setSaving(false);
    }
  };

  return (
    <main className="min-h-screen bg-[var(--bg-body)] text-[var(--text-primary)] px-6 py-16" data-theme="ember">
      <div className="mx-auto flex max-w-6xl flex-col items-center gap-10">
        <div className="text-center">
          <h1 className="font-[family-name:var(--font-headline)] text-4xl">Choose a profile</h1>
          <p className="mt-3 text-[var(--text-muted)]">Profiles and preferences are loaded from the server.</p>
        </div>

        {loading && <p className="font-[family-name:var(--font-mono)] text-sm">Loading profiles…</p>}
        {error && (
          <div className="text-center text-[var(--text-error)]">
            <p>{error}</p>
            <Button className="mt-4" variant="secondary" onClick={() => void loadProfiles()}>Retry</Button>
          </div>
        )}

        {!loading && !error && (
          <div className="flex flex-wrap justify-center gap-6">
            {profiles.map((profile) => (
              <button key={profile.id} className="group w-48 text-left" onClick={() => chooseProfile(profile)}>
                <GlassPane className="p-5" spotlight={false}>
                  <div className="aspect-square rounded-[var(--radius)]" style={{ background: avatarBackground(profile) }} />
                  <div className="mt-4 font-semibold">{profile.name}</div>
                  <div className="mt-1 text-xs uppercase tracking-wider text-[var(--text-muted)]">
                    {normalizeTheme(profile.theme)}{profile.id === "1" ? " · Admin" : ""}
                  </div>
                </GlassPane>
              </button>
            ))}
            <button className="w-48" onClick={() => setShowCreate(true)}>
              <GlassPane className="grid aspect-[4/5] place-items-center border-dashed p-5" spotlight={false}>
                <span className="text-center text-[var(--text-muted)]">+ Create profile</span>
              </GlassPane>
            </button>
          </div>
        )}

        {showCreate && (
          <div className="fixed inset-0 z-50 grid place-items-center bg-black/70 p-6">
            <GlassPane className="w-full max-w-md p-8" spotlight={false}>
              <form className="flex flex-col gap-5" onSubmit={submitProfile}>
                <h2 className="text-2xl font-semibold">Create profile</h2>
                <label className="flex flex-col gap-2">
                  <span className="text-sm text-[var(--text-muted)]">Name</span>
                  <input className="rounded border border-[var(--glass-border)] bg-black/20 px-4 py-3" value={name} onChange={(event) => setName(event.target.value)} maxLength={40} required />
                </label>
                <label className="flex flex-col gap-2">
                  <span className="text-sm text-[var(--text-muted)]">Theme</span>
                  <select className="rounded border border-[var(--glass-border)] bg-[#1e100b] px-4 py-3" value={theme} onChange={(event) => setNewTheme(event.target.value as ThemeId)}>
                    {THEMES.map((item) => <option key={item} value={item}>{item}</option>)}
                  </select>
                </label>
                <div className="flex gap-3">
                  <Button type="submit" disabled={saving}>{saving ? "Creating…" : "Create"}</Button>
                  <Button type="button" variant="ghost" onClick={() => setShowCreate(false)}>Cancel</Button>
                </div>
              </form>
            </GlassPane>
          </div>
        )}
      </div>
    </main>
  );
}
