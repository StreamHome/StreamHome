import React, { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { AnimatePresence, motion } from "framer-motion";
import { useLocation, useNavigate, useParams } from "react-router-dom";
import { deleteProfile, getProfiles, saveProfile } from "../api/profiles";
import { BrandLogo } from "../components/brand/BrandLogo";
import { CONTENT_REVEAL, CONTENT_STAGGER, MOTION_EASE, MOTION_TIMINGS, useAppMotion } from "../motion/motionSystem";
import { profileEditReturnTarget, type ProfileEditLocationState } from "../navigation/profileEditing";
import { PROFILE_AVATAR_PRESETS, avatarPresetBackground } from "../profile/profileAppearance";
import { useProfileStore } from "../stores/profileStore";
import { useThemeStore } from "../stores/themeStore";
import { getThemeDefinition } from "../themes/application/themeRegistry";
import type { Profile } from "../types/api";
import type { ThemeId } from "../types/theme";
import { normalizeTheme } from "../utils/media";

const THEMES: ThemeId[] = ["ember", "aurora", "cinema", "gemini"];
const THEME_LABELS: Record<ThemeId, string> = { ember: "Ember", aurora: "Aurora", cinema: "Cinema", gemini: "Gemini" };

function ThemePreview({ theme }: { theme: ThemeId }) {
  return <span className={`profile-preview profile-preview--${theme}`} aria-hidden="true"><i /><i /><i /></span>;
}

export function ProfileEditPage() {
  const { profileId = "" } = useParams();
  const location = useLocation();
  const navigate = useNavigate();
  const profiles = useProfileStore((state) => state.profiles);
  const activeProfile = useProfileStore((state) => state.activeProfile);
  const setProfiles = useProfileStore((state) => state.setProfiles);
  const updateProfile = useProfileStore((state) => state.updateProfile);
  const removeProfile = useProfileStore((state) => state.removeProfile);
  const activeTheme = useThemeStore((state) => state.activeTheme);
  const setTheme = useThemeStore((state) => state.setTheme);
  const { reduced } = useAppMotion();
  const decodedProfileId = useMemo(() => { try { return decodeURIComponent(profileId); } catch { return profileId; } }, [profileId]);
  const profile = profiles.find((item) => item.id === decodedProfileId) ?? null;
  const returnTarget = profileEditReturnTarget(location.state as ProfileEditLocationState | null);
  const initializedProfile = useRef("");
  const [loadAttempted, setLoadAttempted] = useState(false);
  const [loading, setLoading] = useState(!profile);
  const [loadError, setLoadError] = useState("");
  const [name, setName] = useState("");
  const [draftTheme, setDraftTheme] = useState<ThemeId>(activeTheme);
  const [avatarColor, setAvatarColor] = useState(PROFILE_AVATAR_PRESETS[0].background);
  const [pinEnabled, setPinEnabled] = useState(false);
  const [pin, setPin] = useState("");
  const [pinConfirmation, setPinConfirmation] = useState("");
  const [confirmation, setConfirmation] = useState("");
  const [saving, setSaving] = useState(false);
  const [deleting, setDeleting] = useState(false);
  const [formError, setFormError] = useState("");

  const loadProfiles = useCallback(async () => {
    setLoading(true);
    setLoadError("");
    try { setProfiles(await getProfiles()); }
    catch (requestError) { setLoadError(requestError instanceof Error ? requestError.message : "Profiles could not be loaded."); }
    finally { setLoadAttempted(true); setLoading(false); }
  }, [setProfiles]);

  useEffect(() => {
    if (profile) { setLoading(false); return; }
    if (!loadAttempted) void loadProfiles();
  }, [loadAttempted, loadProfiles, profile]);

  useEffect(() => {
    if (!profile || initializedProfile.current === profile.id) return;
    initializedProfile.current = profile.id;
    setName(profile.name);
    setDraftTheme(normalizeTheme(profile.theme));
    setAvatarColor(avatarPresetBackground(profile));
    setPinEnabled(profile.pinEnabled);
    setPin("");
    setPinConfirmation("");
  }, [profile]);

  const cleanName = name.trim();
  const pinChanged = pin.length > 0;
  const pinValid = !pinEnabled || (!pinChanged && profile?.pinEnabled) || (/^[0-9]{4,8}$/.test(pin) && pin === pinConfirmation);
  const dirty = Boolean(profile && (cleanName !== profile.name || draftTheme !== normalizeTheme(profile.theme) || avatarColor !== avatarPresetBackground(profile) || pinEnabled !== profile.pinEnabled || pinChanged));
  const valid = cleanName.length > 0 && cleanName.length <= 40 && Boolean(pinValid);
  const presentation = getThemeDefinition(profile ? draftTheme : activeTheme);
  const Background = presentation.Background;

  useEffect(() => {
    if (!dirty || saving || deleting) return;
    const warn = (event: BeforeUnloadEvent) => { event.preventDefault(); };
    window.addEventListener("beforeunload", warn);
    return () => window.removeEventListener("beforeunload", warn);
  }, [deleting, dirty, saving]);

  const leave = () => {
    if (dirty && !window.confirm("Discard your unsaved profile changes?")) return;
    navigate(returnTarget, { replace: true });
  };

  const submit = async (event: React.FormEvent) => {
    event.preventDefault();
    if (!profile || !dirty || !valid) return;
    setSaving(true);
    setFormError("");
    try {
      const saved = await saveProfile({
        ...profile,
        name: cleanName,
        theme: draftTheme,
        avatarColor,
        pinEnabled,
        ...(pinChanged ? { pin } : {}),
      });
      updateProfile(saved);
      if (activeProfile?.id === saved.id) setTheme(saved.theme);
      navigate(returnTarget, { replace: true });
    } catch (requestError) {
      setFormError(requestError instanceof Error ? requestError.message : "Profile changes could not be saved.");
    } finally { setSaving(false); }
  };

  const remove = async () => {
    if (!profile || profile.id === "1" || confirmation !== profile.name) return;
    setDeleting(true);
    setFormError("");
    try {
      await deleteProfile(profile.id);
      removeProfile(profile.id);
      navigate(returnTarget, { replace: true, state: { message: `${profile.name} was deleted.` } });
    } catch (requestError) {
      setFormError(requestError instanceof Error ? requestError.message : "Profile could not be deleted.");
    } finally { setDeleting(false); }
  };

  const stateTitle = loading ? "Loading profile" : loadError ? "Profile unavailable" : "Profile not found";
  if (loading || loadError || !profile) return <main className={`theme-app profile-editor profile-editor--state ${presentation.shellClass}`} data-theme={presentation.id} data-interaction={presentation.interaction.id}><Background /><motion.section className="profile-editor__state" initial={{ opacity: 0 }} animate={{ opacity: 1 }}><BrandLogo showWordmark={false} /><p>PROFILE CONTROL</p><h1>{stateTitle}</h1><span>{loading ? "Reading the profile from the server." : loadError || "That profile does not exist on the server."}</span><div>{loadError && <button onClick={() => void loadProfiles()}>Retry</button>}<button onClick={() => navigate("/profiles", { replace: true })}>Back to profiles</button></div></motion.section></main>;

  return <motion.main className={`theme-app profile-editor ${presentation.shellClass}`} data-theme={draftTheme} data-interaction={presentation.interaction.id} variants={CONTENT_STAGGER} initial="hidden" animate="shown">
    <AnimatePresence mode="sync"><motion.div className="profile-editor__ambient" key={draftTheme} initial={{ opacity: 0 }} animate={{ opacity: 1 }} exit={{ opacity: 0 }} transition={{ duration: reduced ? MOTION_TIMINGS.reduced : MOTION_TIMINGS.profileMorph, ease: MOTION_EASE }}><Background /></motion.div></AnimatePresence>
    <motion.header variants={CONTENT_REVEAL} className="profile-editor__header"><button type="button" onClick={leave}>← Back</button><div><p>STREAMHOME / PROFILE CONTROL</p><h1>Edit profile</h1></div><BrandLogo className="brand-logo--profile-editor" showWordmark={false} /></motion.header>
    <motion.form variants={CONTENT_STAGGER} className="profile-editor__layout" onSubmit={submit}>
      <motion.aside variants={CONTENT_REVEAL} className="profile-editor__summary"><motion.div className="profile-editor__avatar" layout style={{ background: avatarColor }}><span>{cleanName.slice(0, 1).toUpperCase() || "?"}</span></motion.div><h2>{cleanName || "Unnamed profile"}</h2><p>{THEME_LABELS[draftTheme]} presentation</p><dl><div><dt>Profile type</dt><dd>{profile.id === "1" ? "Administrator" : "Viewer"}</dd></div><div><dt>Profile ID</dt><dd>{profile.id}</dd></div></dl></motion.aside>
      <div className="profile-editor__sections">
        <motion.section variants={CONTENT_REVEAL} className="profile-editor__panel"><header><p>01 / IDENTITY</p><h2>Profile identity</h2><span>Choose the name shown throughout StreamHome.</span></header><label><span>Profile name</span><input aria-label="Profile name" value={name} onChange={(event) => setName(event.target.value)} minLength={1} maxLength={40} required autoFocus /><small>{cleanName.length}/40 characters</small></label>{(cleanName.length === 0 || cleanName.length > 40) && name.length > 0 && <p className="profile-editor__error">Profile name must contain between 1 and 40 characters.</p>}</motion.section>
        <motion.section variants={CONTENT_REVEAL} className="profile-editor__panel"><header><p>02 / APPEARANCE</p><h2>Theme and avatar</h2><span>Preview changes here before saving them to the profile.</span></header><fieldset className="profile-editor__themes"><legend>Interface theme</legend><div>{THEMES.map((theme) => <button key={theme} type="button" data-active={draftTheme === theme} aria-pressed={draftTheme === theme} onClick={() => setDraftTheme(theme)}><ThemePreview theme={theme} /><strong>{THEME_LABELS[theme]}</strong></button>)}</div></fieldset><fieldset className="profile-editor__avatars"><legend>Avatar preset</legend><div>{PROFILE_AVATAR_PRESETS.map((preset) => <button key={preset.id} type="button" data-active={avatarColor === preset.background} aria-pressed={avatarColor === preset.background} aria-label={preset.label} onClick={() => setAvatarColor(preset.background)}><i style={{ background: preset.background }} /><span>{preset.label}</span></button>)}</div></fieldset></motion.section>
        <motion.section variants={CONTENT_REVEAL} className="profile-editor__panel"><header><p>03 / SECURITY</p><h2>Profile PIN</h2><span>Require a server-verified PIN before this profile can be selected.</span></header><label><span><input aria-label="Require profile PIN" type="checkbox" checked={pinEnabled} onChange={(event) => { setPinEnabled(event.target.checked); setPin(""); setPinConfirmation(""); }} /> Require a PIN</span></label>{pinEnabled && <><label><span>{profile.pinEnabled ? "New PIN (leave blank to keep current)" : "New PIN"}</span><input aria-label="Profile PIN" type="password" inputMode="numeric" autoComplete="new-password" value={pin} onChange={(event) => setPin(event.target.value.replace(/\D/g, "").slice(0, 8))} minLength={4} maxLength={8} required={!profile.pinEnabled} /></label>{pinChanged && <label><span>Confirm PIN</span><input aria-label="Confirm profile PIN" type="password" inputMode="numeric" autoComplete="new-password" value={pinConfirmation} onChange={(event) => setPinConfirmation(event.target.value.replace(/\D/g, "").slice(0, 8))} minLength={4} maxLength={8} required /></label>}{!pinValid && <p className="profile-editor__error">Use 4 to 8 matching digits.</p>}</>}</motion.section>
        <motion.section variants={CONTENT_REVEAL} className="profile-editor__panel profile-editor__danger"><header><p>04 / DANGER ZONE</p><h2>Delete profile</h2></header>{profile.id === "1" ? <p>The administrator profile is permanent and cannot be deleted.</p> : <><p>Type <strong>{profile.name}</strong> to permanently remove this profile and its local selection.</p><label><span>Confirm profile name</span><input aria-label="Confirm profile name" value={confirmation} onChange={(event) => setConfirmation(event.target.value)} /></label><button type="button" disabled={saving || deleting || confirmation !== profile.name} onClick={() => void remove()}>{deleting ? "Deleting…" : "Delete profile"}</button></>}</motion.section>
        <AnimatePresence>{formError && <motion.p className="profile-editor__notice" role="alert" initial={{ opacity: 0, y: -6 }} animate={{ opacity: 1, y: 0 }} exit={{ opacity: 0 }}>{formError}</motion.p>}</AnimatePresence>
        <motion.footer variants={CONTENT_REVEAL} className="profile-editor__actions"><span>{dirty ? "Unsaved changes" : "Profile is up to date"}</span><div><button type="button" onClick={leave} disabled={saving || deleting}>Cancel</button><button type="submit" disabled={!dirty || !valid || saving || deleting}>{saving ? "Saving…" : "Save changes"}</button></div></motion.footer>
      </div>
    </motion.form>
  </motion.main>;
}
