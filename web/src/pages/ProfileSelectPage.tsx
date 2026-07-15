import React, { useCallback, useEffect, useState } from "react";
import { useLocation, useNavigate } from "react-router-dom";
import { createProfile, getProfiles } from "../api/profiles";
import { appUrl, parseAppQuery } from "../navigation/queryState";
import { useProfileStore } from "../stores/profileStore";
import { useThemeStore } from "../stores/themeStore";
import type { Profile } from "../types/api";
import type { ThemeId } from "../types/theme";
import { normalizeTheme } from "../utils/media";

const THEMES: ThemeId[] = ["ember", "aurora", "cinema", "gemini"];

interface ProfileLocationState {
  from?: { pathname?: string; search?: string };
  error?: string;
}

function destinationFor(profile: Profile, from?: ProfileLocationState["from"]): string {
  if (from?.pathname === "/") {
    const requested = parseAppQuery(from.search ?? "");
    const { view, media, genre, season, q, section } = requested;
    return appUrl(profile.id, view, { media, genre, season, q, section });
  }
  if (from?.pathname?.startsWith("/watch/")) {
    const media = decodeURIComponent(from.pathname.slice("/watch/".length));
    return appUrl(profile.id, "watch", { media });
  }
  if (from?.pathname?.startsWith("/admin")) {
    const section = from.pathname.split("/").filter(Boolean)[1];
    return appUrl(profile.id, "admin", { section: section === "storage" || section === "downloads" ? section : "account" });
  }
  return appUrl(profile.id, "home");
}

export function ProfileSelectPage() {
  const navigate = useNavigate();
  const location = useLocation();
  const state = location.state as ProfileLocationState | null;
  const profiles = useProfileStore((store) => store.profiles);
  const setProfiles = useProfileStore((store) => store.setProfiles);
  const selectProfile = useProfileStore((store) => store.selectProfile);
  const setTheme = useThemeStore((store) => store.setTheme);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [showCreate, setShowCreate] = useState(false);
  const [name, setName] = useState("");
  const [theme, setNewTheme] = useState<ThemeId>("ember");
  const [saving, setSaving] = useState(false);

  const loadProfiles = useCallback(async () => {
    setLoading(true);
    setError("");
    try { setProfiles(await getProfiles()); }
    catch (requestError) { setError(requestError instanceof Error ? requestError.message : "Profiles could not be loaded."); }
    finally { setLoading(false); }
  }, [setProfiles]);

  useEffect(() => { void loadProfiles(); }, [loadProfiles]);

  const chooseProfile = (profile: Profile) => {
    selectProfile(profile);
    setTheme(profile.theme);
    navigate(destinationFor(profile, state?.from));
  };

  const submitProfile = async (event: React.FormEvent) => {
    event.preventDefault();
    if (!name.trim()) return;
    setSaving(true);
    setError("");
    try {
      const created = await createProfile({
        id: crypto.randomUUID(), name: name.trim(), theme, avatarColor: "#ff5f1f", pinEnabled: false,
      });
      setProfiles([...profiles, created]);
      setShowCreate(false);
      setName("");
    } catch (requestError) {
      setError(requestError instanceof Error ? requestError.message : "Profile could not be created.");
    } finally { setSaving(false); }
  };

  return (
    <main className="profile-gallery">
      <section className="profile-gallery__content">
        <header className="profile-gallery__header">
          <p>STREAMHOME / PROFILE MATRIX</p>
          <h1>Who is watching?</h1>
          <span>Select a server profile. Its theme will shape the complete workspace.</span>
        </header>
        {state?.error && <p className="profile-gallery__notice" role="status">{state.error}</p>}

        {loading && <div className="profile-gallery__state">Loading profiles from the server...</div>}
        {error && <div className="profile-gallery__state profile-gallery__state--error"><p>{error}</p><button onClick={() => void loadProfiles()}>Retry</button></div>}

        {!loading && !error && (
          <div className="profile-gallery__grid">
            {profiles.map((profile) => {
              const profileTheme = normalizeTheme(profile.theme);
              return (
                <button key={profile.id} className="profile-tile" onClick={() => chooseProfile(profile)}>
                  <span className={`profile-preview profile-preview--${profileTheme}`} aria-hidden="true"><i /><i /><i /></span>
                  <strong>{profile.name}</strong>
                  <small>{profileTheme}{profile.id === "1" ? " / administrator" : " / profile"}</small>
                </button>
              );
            })}
            <button className="profile-tile profile-tile--create" onClick={() => setShowCreate(true)}>
              <span className="profile-create-mark">+</span><strong>Create profile</strong><small>Server profile</small>
            </button>
          </div>
        )}
      </section>

      {showCreate && (
        <div className="profile-dialog" role="dialog" aria-modal="true" aria-label="Create profile">
          <form className="profile-dialog__panel" onSubmit={submitProfile}>
            <p>NEW PROFILE</p><h2>Create a profile</h2>
            <label><span>Name</span><input value={name} onChange={(event) => setName(event.target.value)} maxLength={40} required autoFocus /></label>
            <label><span>Theme</span><select value={theme} onChange={(event) => setNewTheme(event.target.value as ThemeId)}>{THEMES.map((item) => <option key={item} value={item}>{item}</option>)}</select></label>
            <div className="profile-dialog__actions"><button type="submit" disabled={saving}>{saving ? "Creating..." : "Create"}</button><button type="button" onClick={() => setShowCreate(false)}>Cancel</button></div>
          </form>
        </div>
      )}
    </main>
  );
}
