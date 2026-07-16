import React, { useCallback, useEffect, useRef, useState } from "react";
import { AnimatePresence, motion } from "framer-motion";
import { useLocation, useNavigate } from "react-router-dom";
import { createProfile, deleteProfile, getProfiles, saveProfile } from "../api/profiles";
import { appUrl, parseAppQuery } from "../navigation/queryState";
import { useProfileStore } from "../stores/profileStore";
import { useThemeStore } from "../stores/themeStore";
import type { Profile } from "../types/api";
import type { ThemeId } from "../types/theme";
import { normalizeTheme } from "../utils/media";
import { EmberBackground } from "../themes/ember/EmberBackground";
import { AuroraBackground } from "../themes/aurora/AuroraBackground";
import { CinemaBackground } from "../themes/cinema/CinemaBackground";
import { GeminiBackground } from "../themes/gemini/GeminiBackground";
import { MOTION_EASE, MOTION_TIMINGS, useAppMotion } from "../motion/motionSystem";
import { ScanLines } from "../themes/ember/ScanLines";
import { useHoverIntent } from "../motion/useHoverIntent";
import { BrandLogo } from "../components/brand/BrandLogo";

const THEMES: ThemeId[] = ["ember", "aurora", "cinema", "gemini"];
const THEME_LABELS: Record<ThemeId, string> = { ember: "Ember", aurora: "Aurora", cinema: "Cinema", gemini: "Gemini" };
export const PROFILE_INTENT_DELAY = 180;

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

function ThemeChoices({ value, onChange }: { value: ThemeId; onChange: (theme: ThemeId) => void }) {
  return <fieldset className="profile-theme-picker"><legend>Theme</legend><div>{THEMES.map((item) => <button key={item} type="button" data-active={value === item} onClick={() => onChange(item)}><span className={`profile-preview profile-preview--${item}`} aria-hidden="true"><i /><i /><i /></span><b>{THEME_LABELS[item]}</b></button>)}</div></fieldset>;
}

function ProfileAmbient({ theme }: { theme: ThemeId }) {
  const Background = theme === "aurora" ? AuroraBackground : theme === "cinema" ? CinemaBackground : theme === "gemini" ? GeminiBackground : EmberBackground;
  return <AnimatePresence initial={false} mode="sync"><motion.div className="profile-gallery__ambient" data-ambient-theme={theme} key={theme} initial={{ opacity: 0, scale: 1.035, filter: "blur(18px)" }} animate={{ opacity: 1, scale: 1, filter: "blur(0px)" }} exit={{ opacity: 0, scale: .985, filter: "blur(14px)" }} transition={{ duration: MOTION_TIMINGS.profileMorph, ease: MOTION_EASE }}>{theme === "ember" ? <EmberBackground suspendWhenHidden respectReducedMotion /> : <Background />}{theme === "ember" && <ScanLines />}</motion.div></AnimatePresence>;
}

export function ProfileSelectPage() {
  const navigate = useNavigate();
  const location = useLocation();
  const state = location.state as ProfileLocationState | null;
  const profiles = useProfileStore((store) => store.profiles);
  const activeProfile = useProfileStore((store) => store.activeProfile);
  const setProfiles = useProfileStore((store) => store.setProfiles);
  const selectProfile = useProfileStore((store) => store.selectProfile);
  const updateProfile = useProfileStore((store) => store.updateProfile);
  const removeProfile = useProfileStore((store) => store.removeProfile);
  const setTheme = useThemeStore((store) => store.setTheme);
  const { reduced } = useAppMotion();
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const { value: ambientTheme, schedule: previewTheme, cancel: cancelPreview, setImmediate: setAmbientTheme } = useHoverIntent<ThemeId>("ember", PROFILE_INTENT_DELAY);
  const [enteringProfile, setEnteringProfile] = useState<string | null>(null);
  const [showCreate, setShowCreate] = useState(false);
  const [name, setName] = useState("");
  const [theme, setNewTheme] = useState<ThemeId>("ember");
  const [editing, setEditing] = useState<Profile | null>(null);
  const [editName, setEditName] = useState("");
  const [editTheme, setEditTheme] = useState<ThemeId>("ember");
  const [deleteConfirmation, setDeleteConfirmation] = useState("");
  const [saving, setSaving] = useState(false);
  const [deleting, setDeleting] = useState(false);
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

  const openEdit = (profile: Profile) => {
    setEditing(profile);
    setEditName(profile.name);
    setEditTheme(normalizeTheme(profile.theme));
    setDeleteConfirmation("");
    setDialogError("");
  };

  const submitProfile = async (event: React.FormEvent) => {
    event.preventDefault();
    const cleanName = name.trim();
    if (!cleanName) return;
    setSaving(true);
    setDialogError("");
    try {
      const created = await createProfile({ id: crypto.randomUUID(), name: cleanName, theme, avatarColor: "#ff5f1f", pinEnabled: false });
      setProfiles([...profiles, created]);
      setShowCreate(false);
      setName("");
      setNewTheme("ember");
    } catch (requestError) {
      setDialogError(requestError instanceof Error ? requestError.message : "Profile could not be created.");
    } finally { setSaving(false); }
  };

  const submitEdit = async (event: React.FormEvent) => {
    event.preventDefault();
    if (!editing) return;
    const cleanName = editName.trim();
    if (!cleanName) { setDialogError("Profile name is required."); return; }
    setSaving(true);
    setDialogError("");
    try {
      const saved = await saveProfile({
        id: editing.id,
        name: cleanName,
        avatarColor: editing.avatarColor,
        theme: editTheme,
        pinEnabled: editing.pinEnabled,
        pin: editing.pin,
      });
      updateProfile(saved);
      if (activeProfile?.id === saved.id) setTheme(saved.theme);
      setEditing(null);
    } catch (requestError) {
      setDialogError(requestError instanceof Error ? requestError.message : "Profile changes could not be saved.");
    } finally { setSaving(false); }
  };

  const removeEditingProfile = async () => {
    if (!editing || editing.id === "1" || deleteConfirmation !== editing.name) return;
    setDeleting(true);
    setDialogError("");
    try {
      await deleteProfile(editing.id);
      removeProfile(editing.id);
      setEditing(null);
    } catch (requestError) {
      setDialogError(requestError instanceof Error ? requestError.message : "Profile could not be deleted.");
    } finally { setDeleting(false); }
  };

  return (
    <motion.main className={`profile-gallery profile-gallery--${ambientTheme}`} data-entering={Boolean(enteringProfile)} animate={enteringProfile ? { opacity: 0, scale: 1.025, filter: "blur(10px)" } : { opacity: 1, scale: 1, filter: "blur(0px)" }} transition={{ duration: reduced ? MOTION_TIMINGS.reduced : MOTION_TIMINGS.profileEntry, ease: MOTION_EASE }}>
      <ProfileAmbient theme={ambientTheme} />
      <section className="profile-gallery__content">
        <motion.header className="profile-gallery__header" initial={{ opacity: 0, y: 24 }} animate={{ opacity: 1, y: 0 }} transition={{ duration: reduced ? MOTION_TIMINGS.reduced : MOTION_TIMINGS.profileEntry, ease: MOTION_EASE }}><BrandLogo className="brand-logo--profiles" showWordmark={false} /><p>STREAMHOME / PROFILE MATRIX</p><h1>Who is watching?</h1><span>Select a server profile. Its theme shapes the complete workspace.</span></motion.header>
        {state?.error && <p className="profile-gallery__notice" role="status">{state.error}</p>}
        {loading && <div className="profile-gallery__state">Loading profiles from the server...</div>}
        {error && <div className="profile-gallery__state profile-gallery__state--error"><p>{error}</p><button onClick={() => void loadProfiles()}>Retry</button></div>}
        {!loading && !error && <motion.div className="profile-gallery__grid" initial="hidden" animate="shown" variants={{ hidden: {}, shown: { transition: { staggerChildren: .12, delayChildren: .2 } } }}>
          {profiles.map((profile) => {
            const profileTheme = normalizeTheme(profile.theme);
            return <motion.article key={profile.id} className="profile-tile" data-selected={enteringProfile === profile.id} variants={{ hidden: { opacity: 0, y: 34, scale: .94 }, shown: { opacity: 1, y: 0, scale: 1, transition: { duration: reduced ? MOTION_TIMINGS.reduced : MOTION_TIMINGS.profileEntry, ease: MOTION_EASE } } }} onMouseEnter={() => previewTheme(profileTheme)} onMouseLeave={clearPreview} onFocus={() => previewTheme(profileTheme)} onBlur={(event) => { if (!event.currentTarget.contains(event.relatedTarget)) clearPreview(); }}>
              <button className="profile-tile__select" onClick={() => chooseProfile(profile)}><span className={`profile-preview profile-preview--${profileTheme}`} aria-hidden="true"><i /><i /><i /></span><strong>{profile.name}</strong><small>{THEME_LABELS[profileTheme]}{profile.id === "1" ? " / administrator" : " / profile"}</small></button>
              <button className="profile-tile__edit" onClick={() => openEdit(profile)}>Edit profile</button>
            </motion.article>;
          })}
          <motion.button variants={{ hidden: { opacity: 0, y: 34, scale: .94 }, shown: { opacity: 1, y: 0, scale: 1, transition: { duration: reduced ? MOTION_TIMINGS.reduced : MOTION_TIMINGS.profileEntry, ease: MOTION_EASE } } }} className="profile-tile profile-tile--create" onClick={() => { setShowCreate(true); setDialogError(""); }} onMouseEnter={() => previewTheme("ember")}><span className="profile-create-mark">+</span><strong>Create profile</strong><small>Server profile</small></motion.button>
        </motion.div>}
      </section>

      <AnimatePresence>
        {showCreate && <motion.div key="create" className="profile-dialog" role="dialog" aria-modal="true" aria-label="Create profile" initial={{ opacity: 0, backdropFilter: "blur(0px)" }} animate={{ opacity: 1, backdropFilter: "blur(20px)" }} exit={{ opacity: 0, backdropFilter: "blur(0px)" }} transition={{ duration: reduced ? MOTION_TIMINGS.reduced : MOTION_TIMINGS.dialog, ease: MOTION_EASE }}><motion.form className="profile-dialog__panel" onSubmit={submitProfile} initial={{ opacity: 0, y: 28, scale: .94 }} animate={{ opacity: 1, y: 0, scale: 1 }} exit={{ opacity: 0, y: 18, scale: .97 }} transition={{ duration: reduced ? MOTION_TIMINGS.reduced : MOTION_TIMINGS.dialog, ease: MOTION_EASE }}><p>NEW PROFILE</p><h2>Create a profile</h2><label><span>Name</span><input value={name} onChange={(event) => setName(event.target.value)} minLength={1} maxLength={40} required autoFocus /></label><ThemeChoices value={theme} onChange={setNewTheme} />{dialogError && <p className="profile-dialog__error" role="alert">{dialogError}</p>}<div className="profile-dialog__actions"><button type="submit" disabled={saving}>{saving ? "Creating..." : "Create"}</button><button type="button" onClick={() => setShowCreate(false)}>Cancel</button></div></motion.form></motion.div>}
        {editing && <motion.div key="edit" className="profile-dialog" role="dialog" aria-modal="true" aria-label={`Edit ${editing.name}`} initial={{ opacity: 0, backdropFilter: "blur(0px)" }} animate={{ opacity: 1, backdropFilter: "blur(20px)" }} exit={{ opacity: 0, backdropFilter: "blur(0px)" }} transition={{ duration: reduced ? MOTION_TIMINGS.reduced : MOTION_TIMINGS.dialog, ease: MOTION_EASE }}><motion.form className="profile-dialog__panel profile-dialog__panel--edit" onSubmit={submitEdit} initial={{ opacity: 0, y: 28, scale: .94 }} animate={{ opacity: 1, y: 0, scale: 1 }} exit={{ opacity: 0, y: 18, scale: .97 }} transition={{ duration: reduced ? MOTION_TIMINGS.reduced : MOTION_TIMINGS.dialog, ease: MOTION_EASE }}><p>PROFILE SETTINGS</p><h2>Edit profile</h2><label><span>Name</span><input value={editName} onChange={(event) => setEditName(event.target.value)} minLength={1} maxLength={40} required autoFocus /></label><ThemeChoices value={editTheme} onChange={setEditTheme} />{dialogError && <p className="profile-dialog__error" role="alert">{dialogError}</p>}<div className="profile-dialog__actions"><button type="submit" disabled={saving || deleting}>{saving ? "Saving..." : "Save changes"}</button><button type="button" onClick={() => setEditing(null)} disabled={saving || deleting}>Cancel</button></div>{editing.id === "1" ? <p className="profile-delete-note">The administrator profile cannot be deleted.</p> : <section className="profile-delete-zone"><h3>Delete profile</h3><p>Type <strong>{editing.name}</strong> to permanently delete this profile.</p><input aria-label="Confirm profile name" value={deleteConfirmation} onChange={(event) => setDeleteConfirmation(event.target.value)} /><button type="button" disabled={deleting || saving || deleteConfirmation !== editing.name} onClick={() => void removeEditingProfile()}>{deleting ? "Deleting..." : "Delete profile"}</button></section>}</motion.form></motion.div>}
      </AnimatePresence>
    </motion.main>
  );
}
