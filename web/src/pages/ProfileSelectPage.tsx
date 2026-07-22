import React, { useCallback, useEffect, useRef, useState } from "react";
import { AnimatePresence, motion } from "framer-motion";
import { useLocation, useNavigate } from "react-router-dom";
import { createProfile, getProfiles } from "../api/profiles";
import { profileEditUrl } from "../navigation/profileEditing";
import { appUrl, parseAppQuery } from "../navigation/queryState";
import { useProfileStore } from "../stores/profileStore";
import { useThemeStore } from "../stores/themeStore";
import type { Profile } from "../types/api";
import type { ThemeId } from "../types/theme";
import { normalizeTheme } from "../utils/media";
import { AuroraBackground } from "../themes/aurora/AuroraBackground";
import { CinemaBackground } from "../themes/cinema/CinemaBackground";
import { GeminiBackground } from "../themes/gemini/GeminiBackground";
import { MOTION_EASE, MOTION_TIMINGS, useAppMotion } from "../motion/motionSystem";
import { ScanLines } from "../themes/ember/ScanLines";
import { useHoverIntent } from "../motion/useHoverIntent";
import { BrandLogo } from "../components/brand/BrandLogo";
import { PROFILE_AVATAR_PRESETS } from "../profile/profileAppearance";

const THEMES: ThemeId[] = ["ember", "aurora", "cinema", "gemini"];
const THEME_LABELS: Record<ThemeId, string> = { ember: "Ember", aurora: "Aurora", cinema: "Cinema", gemini: "Gemini" };
export const PROFILE_INTENT_DELAY = 180;

interface ProfileLocationState {
  from?: { pathname?: string; search?: string };
  error?: string;
  message?: string;
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

function ThemeChoices({ value, onChange }: { value: ThemeId; onChange: (theme: ThemeId) => void }) {
  return <fieldset className="profile-theme-picker"><legend>Theme</legend><div>{THEMES.map((item) => <button key={item} type="button" data-active={value === item} onClick={() => onChange(item)}><span className={`profile-preview profile-preview--${item}`} aria-hidden="true"><i /><i /><i /></span><b>{THEME_LABELS[item]}</b></button>)}</div></fieldset>;
}

function ProfileAmbient({ theme }: { theme: ThemeId }) {
  const Background = theme === "aurora" ? AuroraBackground : theme === "cinema" ? CinemaBackground : GeminiBackground;
  const { reduced } = useAppMotion();
  return <AnimatePresence initial={false} mode="sync"><motion.div className="profile-gallery__ambient" data-ambient-theme={theme} key={theme} initial={{ opacity: 0 }} animate={{ opacity: 1 }} exit={{ opacity: 0 }} transition={{ duration: reduced ? MOTION_TIMINGS.reduced : MOTION_TIMINGS.profileMorph, ease: MOTION_EASE }}>{theme === "ember" ? <ScanLines /> : <Background />}</motion.div></AnimatePresence>;
}

export function ProfileSelectPage() {
  const navigate = useNavigate();
  const location = useLocation();
  const state = location.state as ProfileLocationState | null;
  const profiles = useProfileStore((store) => store.profiles);
  const setProfiles = useProfileStore((store) => store.setProfiles);
  const selectProfile = useProfileStore((store) => store.selectProfile);
  const setTheme = useThemeStore((store) => store.setTheme);
  const { reduced } = useAppMotion();
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const { value: ambientTheme, schedule: previewTheme, cancel: cancelPreview, setImmediate: setAmbientTheme } = useHoverIntent<ThemeId>("ember", PROFILE_INTENT_DELAY);
  const [enteringProfile, setEnteringProfile] = useState<string | null>(null);
  const [showCreate, setShowCreate] = useState(false);
  const [name, setName] = useState("");
  const [theme, setNewTheme] = useState<ThemeId>("ember");
  const [saving, setSaving] = useState(false);
  const [dialogError, setDialogError] = useState("");
  const navigationTimer = useRef<number | null>(null);

  const loadProfiles = useCallback(async () => {
    setLoading(true);
    setError("");
    try { setProfiles(await getProfiles()); }
    catch (requestError) { setError(requestError instanceof Error ? requestError.message : "Profiles could not be loaded."); }
    finally { setLoading(false); }
  }, [setProfiles]);

  useEffect(() => { void loadProfiles(); }, [loadProfiles]);

  useEffect(() => () => {
    if (navigationTimer.current !== null) window.clearTimeout(navigationTimer.current);
  }, []);

  const clearPreview = () => {
    if (enteringProfile) cancelPreview();
    else cancelPreview("ember");
  };

  const chooseProfile = (profile: Profile) => {
    if (enteringProfile) return;
    selectProfile(profile);
    setTheme(profile.theme);
    setAmbientTheme(normalizeTheme(profile.theme));
    setEnteringProfile(profile.id);
    navigationTimer.current = window.setTimeout(() => navigate(destinationFor(profile, state?.from)), reduced ? 180 : MOTION_TIMINGS.profileEntry * 1000);
  };

  const submitProfile = async (event: React.FormEvent) => {
    event.preventDefault();
    const cleanName = name.trim();
    if (!cleanName) return;
    setSaving(true);
    setDialogError("");
    try {
      const created = await createProfile({ id: crypto.randomUUID(), name: cleanName, theme, avatarColor: PROFILE_AVATAR_PRESETS[0].background, pinEnabled: false });
      setProfiles([...profiles, created]);
      setShowCreate(false);
      setName("");
      setNewTheme("ember");
    } catch (requestError) {
      setDialogError(requestError instanceof Error ? requestError.message : "Profile could not be created.");
    } finally { setSaving(false); }
  };

  return (
    <motion.main className={`profile-gallery profile-gallery--${ambientTheme}`} data-entering={Boolean(enteringProfile)} animate={enteringProfile ? { opacity: 0, scale: 1.008 } : { opacity: 1, scale: 1 }} transition={{ duration: reduced ? MOTION_TIMINGS.reduced : MOTION_TIMINGS.profileEntry, ease: MOTION_EASE }}>
      <ProfileAmbient theme={ambientTheme} />
      <section className="profile-gallery__content">
        <motion.header className="profile-gallery__header" initial={{ opacity: 0, y: 24 }} animate={{ opacity: 1, y: 0 }} transition={{ duration: reduced ? MOTION_TIMINGS.reduced : MOTION_TIMINGS.profileEntry, ease: MOTION_EASE }}><BrandLogo className="brand-logo--profiles" showWordmark={false} /><p>STREAMHOME / PROFILE MATRIX</p><h1>Who is watching?</h1><span>Select a server profile. Its theme shapes the complete workspace.</span></motion.header>
        {state?.error && <p className="profile-gallery__notice" role="status">{state.error}</p>}
        {state?.message && <p className="profile-gallery__notice" role="status">{state.message}</p>}
        {loading && <div className="profile-gallery__state">Loading profiles from the server...</div>}
        {error && <div className="profile-gallery__state profile-gallery__state--error"><p>{error}</p><button onClick={() => void loadProfiles()}>Retry</button></div>}
        {!loading && !error && <motion.div className="profile-gallery__grid" initial="hidden" animate="shown" variants={{ hidden: {}, shown: { transition: { staggerChildren: .025, delayChildren: .02 } } }}>
          {profiles.map((profile) => {
            const profileTheme = normalizeTheme(profile.theme);
            return <motion.article key={profile.id} className="profile-tile" data-selected={enteringProfile === profile.id} variants={{ hidden: { opacity: 0, y: 10 }, shown: { opacity: 1, y: 0, transition: { duration: reduced ? MOTION_TIMINGS.reduced : MOTION_TIMINGS.profileEntry, ease: MOTION_EASE } } }} onMouseEnter={() => previewTheme(profileTheme)} onMouseLeave={clearPreview} onFocus={() => previewTheme(profileTheme)} onBlur={(event) => { if (!event.currentTarget.contains(event.relatedTarget)) clearPreview(); }}>
              <button className="profile-tile__select" onClick={() => chooseProfile(profile)}><span className={`profile-preview profile-preview--${profileTheme}`} aria-hidden="true"><i /><i /><i /></span><strong>{profile.name}</strong><small>{THEME_LABELS[profileTheme]}{profile.id === "1" ? " / administrator" : " / profile"}</small></button>
              <button className="profile-tile__edit" onClick={() => navigate(profileEditUrl(profile.id), { state: { returnTo: "/profiles" } })}>Edit profile</button>
            </motion.article>;
          })}
          <motion.button variants={{ hidden: { opacity: 0, y: 34, scale: .94 }, shown: { opacity: 1, y: 0, scale: 1, transition: { duration: reduced ? MOTION_TIMINGS.reduced : MOTION_TIMINGS.profileEntry, ease: MOTION_EASE } } }} className="profile-tile profile-tile--create" onClick={() => { setShowCreate(true); setDialogError(""); }} onMouseEnter={() => previewTheme("ember")}><span className="profile-create-mark">+</span><strong>Create profile</strong><small>Server profile</small></motion.button>
        </motion.div>}
      </section>

      <AnimatePresence>
        {showCreate && <motion.div key="create" className="profile-dialog" role="dialog" aria-modal="true" aria-label="Create profile" initial={{ opacity: 0, backdropFilter: "blur(0px)" }} animate={{ opacity: 1, backdropFilter: "blur(20px)" }} exit={{ opacity: 0, backdropFilter: "blur(0px)" }} transition={{ duration: reduced ? MOTION_TIMINGS.reduced : MOTION_TIMINGS.dialog, ease: MOTION_EASE }}><motion.form className="profile-dialog__panel" onSubmit={submitProfile} initial={{ opacity: 0, y: 28, scale: .94 }} animate={{ opacity: 1, y: 0, scale: 1 }} exit={{ opacity: 0, y: 18, scale: .97 }} transition={{ duration: reduced ? MOTION_TIMINGS.reduced : MOTION_TIMINGS.dialog, ease: MOTION_EASE }}><p>NEW PROFILE</p><h2>Create a profile</h2><label><span>Name</span><input value={name} onChange={(event) => setName(event.target.value)} minLength={1} maxLength={40} required autoFocus /></label><ThemeChoices value={theme} onChange={setNewTheme} />{dialogError && <p className="profile-dialog__error" role="alert">{dialogError}</p>}<div className="profile-dialog__actions"><button type="submit" disabled={saving}>{saving ? "Creating..." : "Create"}</button><button type="button" onClick={() => setShowCreate(false)}>Cancel</button></div></motion.form></motion.div>}
      </AnimatePresence>
    </motion.main>
  );
}
