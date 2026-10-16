import React, { useCallback, useEffect, useRef, useState } from "react";
import { AnimatePresence, motion, useIsPresent, type HTMLMotionProps } from "framer-motion";
import { useLocation, useNavigate } from "react-router-dom";
import { ApiError } from "../api/client";
import { useDialogLifecycle } from "../components/ui/useDialogLifecycle";
import { createProfile, getProfiles, selectProfileAccess, unlockProfile } from "../api/profiles";
import { profileEditUrl } from "../navigation/profileEditing";
import { appUrl, parseAppQuery } from "../navigation/queryState";
import { useProfileStore } from "../stores/profileStore";
import { useThemeStore } from "../stores/themeStore";
import type { Profile } from "../types/api";
import type { ThemeId } from "../types/theme";
import { normalizeTheme } from "../utils/media";
import { AuroraBackground } from "../themes/aurora/AuroraBackground";
import { CinemaBackground } from "../themes/cinema/CinemaBackground";
import { MOTION_EASE, MOTION_TIMINGS, useAppMotion } from "../motion/motionSystem";
import { ScanLines } from "../themes/ember/ScanLines";
import { useHoverIntent } from "../motion/useHoverIntent";
import { BrandLogo } from "../components/brand/BrandLogo";
import { PROFILE_AVATAR_PRESETS } from "../profile/profileAppearance";

const THEMES: ThemeId[] = ["ember", "aurora", "cinema"];
const THEME_LABELS: Record<ThemeId, string> = { ember: "Ember", aurora: "Aurora", cinema: "Cinema" };
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
  return <fieldset className="profile-theme-picker"><legend>Theme</legend><div>{THEMES.map((item) => <button key={item} type="button" data-active={value === item} aria-pressed={value === item} onClick={() => onChange(item)}><span className={`profile-preview profile-preview--${item}`} aria-hidden="true"><i /><i /><i /></span><b>{THEME_LABELS[item]}</b></button>)}</div></fieldset>;
}

function ProfileDialogLayer(props: HTMLMotionProps<"div">) {
  const present = useIsPresent();
  return <motion.div {...props} inert={!present} aria-hidden={!present || undefined} />;
}

function ProfileAmbient({ theme }: { theme: ThemeId }) {
  const background = theme === "ember" ? <ScanLines /> : theme === "aurora" ? <AuroraBackground /> : <CinemaBackground />;
  const { reduced } = useAppMotion();
  return <AnimatePresence initial={false} mode="sync"><motion.div className="profile-gallery__ambient" data-ambient-theme={theme} key={theme} initial={{ opacity: 0 }} animate={{ opacity: 1 }} exit={{ opacity: 0 }} transition={{ duration: reduced ? MOTION_TIMINGS.reduced : MOTION_TIMINGS.profileMorph, ease: MOTION_EASE }}>{background}</motion.div></AnimatePresence>;
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
  const [selectingProfile, setSelectingProfile] = useState<string | null>(null);
  const [showCreate, setShowCreate] = useState(false);
  const [name, setName] = useState("");
  const [theme, setNewTheme] = useState<ThemeId>("ember");
  const [saving, setSaving] = useState(false);
  const [dialogError, setDialogError] = useState("");
  const [pinProfile, setPinProfile] = useState<Profile | null>(null);
  const [profilePin, setProfilePin] = useState("");
  const [pinBusy, setPinBusy] = useState(false);
  const [pinError, setPinError] = useState("");
  const contentRef = useRef<HTMLElement>(null);
  const dialogRef = useRef<HTMLFormElement>(null);
  const invokerRef = useRef<HTMLElement | null>(null);
  const mounted = useRef(true);
  const dialogGeneration = useRef(0);
  const pendingSelection = useRef(false);
  const selectionGeneration = useRef(0);
  const entryInProgress = useRef(false);
  const pendingDialog = useRef(false);
  const uncertainCreateRef = useRef<string | null>(null);
  const [uncertainCreate, setUncertainCreate] = useState<string | null>(null);
  const closeDialog = () => {
    if (pendingDialog.current) return;
    dialogGeneration.current += 1;
    setShowCreate(false); setPinProfile(null); setProfilePin(""); setPinError("");
  };
  useDialogLifecycle({ open: showCreate || Boolean(pinProfile), dialogRef, invokerRef, inertRef: contentRef, onClose: closeDialog });
  const navigationTimer = useRef<number | null>(null);

  const profileRead = useRef(0);
  const loadProfiles = useCallback(async () => {
    const request = ++profileRead.current;
    setLoading(true);
    setError("");
    try { const nextProfiles = await getProfiles(); if (mounted.current && request === profileRead.current) setProfiles(nextProfiles); }
    catch (requestError) { if (mounted.current && request === profileRead.current) setError(requestError instanceof Error ? requestError.message : "Profiles could not be loaded."); }
    finally { if (mounted.current && request === profileRead.current) setLoading(false); }
  }, [setProfiles]);

  useEffect(() => { void loadProfiles(); }, [loadProfiles]);

  useEffect(() => { mounted.current = true; return () => {
    mounted.current = false; dialogGeneration.current += 1; selectionGeneration.current += 1;
    if (navigationTimer.current !== null) window.clearTimeout(navigationTimer.current);
  }; }, []);

  const clearPreview = () => {
    if (enteringProfile) cancelPreview();
    else cancelPreview("ember");
  };

  const enterProfile = (profile: Profile) => {
    if (entryInProgress.current) return;
    entryInProgress.current = true;
    selectProfile(profile);
    setTheme(profile.theme);
    setAmbientTheme(normalizeTheme(profile.theme));
    setEnteringProfile(profile.id);
    navigationTimer.current = window.setTimeout(() => navigate(destinationFor(profile, state?.from)), reduced ? 180 : MOTION_TIMINGS.profileEntry * 1000);
  };

  const chooseProfile = async (profile: Profile, invoker: HTMLElement) => {
    if (pendingDialog.current || pendingSelection.current || entryInProgress.current) return;
    if (profile.pinEnabled) {
      invokerRef.current = invoker; dialogGeneration.current += 1;
      setPinProfile(profile);
      setProfilePin("");
      setPinError("");
      return;
    }
    const generation = ++selectionGeneration.current;
    pendingSelection.current = true; setSelectingProfile(profile.id);
    setError("");
    try {
      const result = await selectProfileAccess(profile.id);
      if (!mounted.current || generation !== selectionGeneration.current) return;
      if (result.selected !== true) throw new Error("The server did not confirm profile selection.");
      enterProfile(profile);
    } catch (requestError) {
      if (mounted.current && generation === selectionGeneration.current) setError(requestError instanceof Error ? requestError.message : "The profile could not be selected.");
    } finally {
      if (mounted.current && generation === selectionGeneration.current) { pendingSelection.current = false; setSelectingProfile(null); }
    }
  };

  const submitProfilePin = async (event: React.FormEvent) => {
    event.preventDefault();
    if (pendingDialog.current || !pinProfile || !/^[0-9]{4,8}$/.test(profilePin)) return;
    const generation = dialogGeneration.current;
    const verifiedProfile = pinProfile;
    pendingDialog.current = true; setPinBusy(true); setPinError("");
    try {
      const result = await unlockProfile(verifiedProfile.id, profilePin);
      if (!mounted.current || generation !== dialogGeneration.current) return;
      if (result.verified !== true) throw new Error("The server did not confirm profile access.");
      setPinProfile(null); setProfilePin(""); enterProfile(verifiedProfile);
    } catch (requestError) {
      if (mounted.current && generation === dialogGeneration.current) {
        setPinError(requestError instanceof Error ? requestError.message : "The profile PIN was not accepted.");
        dialogRef.current?.querySelector("input")?.focus();
      }
    } finally {
      if (mounted.current && generation === dialogGeneration.current) { pendingDialog.current = false; setPinBusy(false); }
    }
  };

  const reconcileCreate = async (profileId: string, generation: number) => {
    const latest = await getProfiles();
    if (!mounted.current || generation !== dialogGeneration.current) return;
    setProfiles(latest);
    if (latest.some((item) => item.id === profileId)) { uncertainCreateRef.current = null; setUncertainCreate(null); setShowCreate(false); setName(""); setNewTheme("ember"); }
    else setDialogError("This creation is not listed yet. Its outcome remains unconfirmed; check again before starting another creation.");
  };

  const checkCreate = async () => {
    if (!uncertainCreate || pendingDialog.current) return;
    const generation = dialogGeneration.current;
    pendingDialog.current = true; setSaving(true);
    try { await reconcileCreate(uncertainCreate, generation); }
    catch { if (mounted.current && generation === dialogGeneration.current) setDialogError("Creation is still unconfirmed. Check the server profile list again before retrying."); }
    finally { if (mounted.current && generation === dialogGeneration.current) { pendingDialog.current = false; setSaving(false); } }
  };

  const submitProfile = async (event: React.FormEvent) => {
    event.preventDefault();
    const cleanName = name.trim();
    if (!cleanName || pendingDialog.current || uncertainCreateRef.current) return;
    const generation = dialogGeneration.current;
    const profileId = crypto.randomUUID();
    pendingDialog.current = true; setSaving(true); setDialogError("");
    try {
      const created = await createProfile({ id: profileId, name: cleanName, theme, avatarColor: PROFILE_AVATAR_PRESETS[0].background, pinEnabled: false });
      if (!mounted.current || generation !== dialogGeneration.current) return;
      setProfiles([...useProfileStore.getState().profiles.filter((item) => item.id !== created.id), created]);
      setShowCreate(false); setName(""); setNewTheme("ember");
    } catch (requestError) {
      if (!mounted.current || generation !== dialogGeneration.current) return;
      if (requestError instanceof ApiError && requestError.status === 0) {
        uncertainCreateRef.current = profileId; setUncertainCreate(profileId);
        setDialogError("The creation outcome is unconfirmed. Check the server profile list before trying again.");
      } else setDialogError(requestError instanceof Error ? requestError.message : "Profile could not be created.");
      dialogRef.current?.querySelector("input")?.focus();
    } finally { if (mounted.current && generation === dialogGeneration.current) { pendingDialog.current = false; setSaving(false); } }
  };

  return (
    <motion.main className={`profile-gallery profile-gallery--${ambientTheme}`} data-entering={Boolean(enteringProfile)} animate={enteringProfile ? { opacity: 0, scale: 1.008 } : { opacity: 1, scale: 1 }} transition={{ duration: reduced ? MOTION_TIMINGS.reduced : MOTION_TIMINGS.profileEntry, ease: MOTION_EASE }}>
      <ProfileAmbient theme={ambientTheme} />
      <section ref={contentRef} className="profile-gallery__content">
        <motion.header className="profile-gallery__header" initial={{ opacity: 0, y: 24 }} animate={{ opacity: 1, y: 0 }} transition={{ duration: reduced ? MOTION_TIMINGS.reduced : MOTION_TIMINGS.profileEntry, ease: MOTION_EASE }}><BrandLogo className="brand-logo--profiles" showWordmark={false} /><p>STREAMHOME / PROFILE MATRIX</p><h1>Who is watching?</h1><span>Select a server profile. Its theme shapes the complete workspace.</span></motion.header>
        {state?.error && <p className="profile-gallery__notice" role="status">{state.error}</p>}
        {state?.message && <p className="profile-gallery__notice" role="status">{state.message}</p>}
        {loading && <div className="profile-gallery__state">Loading profiles from the server...</div>}
        {error && <div className="profile-gallery__state profile-gallery__state--error"><p>{error}</p><button onClick={() => void loadProfiles()}>Retry</button></div>}
        {!loading && !error && <motion.div className="profile-gallery__grid" initial="hidden" animate="shown" variants={{ hidden: {}, shown: { transition: { staggerChildren: .025, delayChildren: .02 } } }}>
          {profiles.map((profile) => {
            const profileTheme = normalizeTheme(profile.theme);
            return <motion.article key={profile.id} className="profile-tile" data-selected={enteringProfile === profile.id} variants={{ hidden: { opacity: 0, y: 10 }, shown: { opacity: 1, y: 0, transition: { duration: reduced ? MOTION_TIMINGS.reduced : MOTION_TIMINGS.profileEntry, ease: MOTION_EASE } } }} onMouseEnter={() => previewTheme(profileTheme)} onMouseLeave={clearPreview} onFocus={() => previewTheme(profileTheme)} onBlur={(event) => { if (!event.currentTarget.contains(event.relatedTarget)) clearPreview(); }}>
              <button className="profile-tile__select" disabled={selectingProfile !== null || Boolean(enteringProfile)} onClick={(event) => void chooseProfile(profile, event.currentTarget)}><span className={`profile-preview profile-preview--${profileTheme}`} aria-hidden="true"><i /><i /><i /></span><strong>{profile.name}</strong><small>{selectingProfile === profile.id ? "Selecting..." : `${THEME_LABELS[profileTheme]}${profile.id === "1" ? " / administrator" : " / profile"}`}</small></button>
              <button className="profile-tile__edit" disabled={selectingProfile !== null || Boolean(enteringProfile)} onClick={() => { if (!pendingSelection.current && !entryInProgress.current) navigate(profileEditUrl(profile.id), { state: { returnTo: "/profiles" } }); }}>Edit profile</button>
            </motion.article>;
          })}
          <motion.button variants={{ hidden: { opacity: 0, y: 34, scale: .94 }, shown: { opacity: 1, y: 0, scale: 1, transition: { duration: reduced ? MOTION_TIMINGS.reduced : MOTION_TIMINGS.profileEntry, ease: MOTION_EASE } } }} className="profile-tile profile-tile--create" disabled={selectingProfile !== null || Boolean(enteringProfile)} onClick={(event) => { if (pendingSelection.current || entryInProgress.current || pendingDialog.current) return; invokerRef.current = event.currentTarget; dialogGeneration.current += 1; setShowCreate(true); setDialogError(""); }} onMouseEnter={() => previewTheme("ember")}><span className="profile-create-mark">+</span><strong>Create profile</strong><small>Server profile</small></motion.button>
        </motion.div>}
      </section>

      <AnimatePresence>
        {showCreate && <ProfileDialogLayer key="create" className="profile-dialog" role="dialog" aria-modal="true" aria-label="Create profile" initial={{ opacity: 0, backdropFilter: "blur(0px)" }} animate={{ opacity: 1, backdropFilter: "blur(20px)" }} exit={{ opacity: 0, backdropFilter: "blur(0px)" }} transition={{ duration: reduced ? MOTION_TIMINGS.reduced : MOTION_TIMINGS.dialog, ease: MOTION_EASE }}><motion.form ref={dialogRef} tabIndex={-1} className="profile-dialog__panel" onSubmit={submitProfile} initial={{ opacity: 0, y: 28, scale: .94 }} animate={{ opacity: 1, y: 0, scale: 1 }} exit={{ opacity: 0, y: 18, scale: .97 }} transition={{ duration: reduced ? MOTION_TIMINGS.reduced : MOTION_TIMINGS.dialog, ease: MOTION_EASE }}><p>NEW PROFILE</p><h2>Create a profile</h2><label><span>Name</span><input value={name} onChange={(event) => setName(event.target.value)} minLength={1} maxLength={40} required /></label><ThemeChoices value={theme} onChange={setNewTheme} />{dialogError && <p className="profile-dialog__error" role="alert">{dialogError}</p>}{uncertainCreate && <button type="button" disabled={saving} onClick={() => void checkCreate()}>Check creation status</button>}{saving && <p role="status">Creating or checking profile…</p>}<div className="profile-dialog__actions"><button type="submit" disabled={saving || Boolean(uncertainCreate)}>{saving ? "Creating..." : "Create"}</button><button type="button" disabled={saving} onClick={closeDialog}>Cancel</button></div></motion.form></ProfileDialogLayer>}
        {pinProfile && <ProfileDialogLayer key="pin" className="profile-dialog" role="dialog" aria-modal="true" aria-label={`Unlock ${pinProfile.name}`} initial={{ opacity: 0, backdropFilter: "blur(0px)" }} animate={{ opacity: 1, backdropFilter: "blur(20px)" }} exit={{ opacity: 0, backdropFilter: "blur(0px)" }} transition={{ duration: reduced ? MOTION_TIMINGS.reduced : MOTION_TIMINGS.dialog, ease: MOTION_EASE }}><motion.form ref={dialogRef} tabIndex={-1} className="profile-dialog__panel" onSubmit={submitProfilePin} initial={{ opacity: 0, y: 28, scale: .94 }} animate={{ opacity: 1, y: 0, scale: 1 }} exit={{ opacity: 0, y: 18, scale: .97 }} transition={{ duration: reduced ? MOTION_TIMINGS.reduced : MOTION_TIMINGS.dialog, ease: MOTION_EASE }}><p>PROTECTED PROFILE</p><h2>Unlock {pinProfile.name}</h2><label><span>Profile PIN</span><input value={profilePin} onChange={(event) => { setProfilePin(event.target.value.replace(/\D/g, "").slice(0, 8)); setPinError(""); }} inputMode="numeric" autoComplete="off" minLength={4} maxLength={8} required type="password" /></label>{pinBusy && <p role="status">Verifying profile PIN…</p>}{pinError && <p className="profile-dialog__error" role="alert">{pinError}</p>}<div className="profile-dialog__actions"><button type="submit" disabled={pinBusy || !/^[0-9]{4,8}$/.test(profilePin)}>{pinBusy ? "Verifying..." : "Unlock"}</button><button type="button" disabled={pinBusy} onClick={closeDialog}>Cancel</button></div></motion.form></ProfileDialogLayer>}
      </AnimatePresence>
    </motion.main>
  );
}
