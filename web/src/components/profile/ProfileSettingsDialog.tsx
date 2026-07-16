import React, { useEffect, useState } from "react";
import { deleteProfile, saveProfile } from "../../api/profiles";
import { useProfileStore } from "../../stores/profileStore";
import { useThemeStore } from "../../stores/themeStore";
import type { Profile } from "../../types/api";
import type { ThemeId } from "../../types/theme";
import { normalizeTheme } from "../../utils/media";

const THEMES: ThemeId[] = ["ember", "aurora", "cinema", "gemini"];
const THEME_LABELS: Record<ThemeId, string> = { ember: "Ember", aurora: "Aurora", cinema: "Cinema", gemini: "Gemini" };

function ThemeChoices({ value, onChange }: { value: ThemeId; onChange: (theme: ThemeId) => void }) {
  return <fieldset className="profile-theme-picker"><legend>Theme</legend><div>{THEMES.map((item) => <button key={item} type="button" data-active={value === item} onClick={() => onChange(item)}><span className={`profile-preview profile-preview--${item}`} aria-hidden="true"><i /><i /><i /></span><b>{THEME_LABELS[item]}</b></button>)}</div></fieldset>;
}

export function ProfileSettingsDialog({ profile, onClose, onDeleted }: { profile: Profile; onClose: () => void; onDeleted: () => void }) {
  const updateProfile = useProfileStore((state) => state.updateProfile);
  const removeProfile = useProfileStore((state) => state.removeProfile);
  const setTheme = useThemeStore((state) => state.setTheme);
  const activeTheme = useThemeStore((state) => state.activeTheme);
  const [name, setName] = useState(profile.name);
  const [theme, setSelectedTheme] = useState<ThemeId>(normalizeTheme(profile.theme));
  const [confirmation, setConfirmation] = useState("");
  const [saving, setSaving] = useState(false);
  const [deleting, setDeleting] = useState(false);
  const [error, setError] = useState("");

  useEffect(() => {
    const closeOnEscape = (event: KeyboardEvent) => { if (event.key === "Escape" && !saving && !deleting) onClose(); };
    window.addEventListener("keydown", closeOnEscape);
    return () => window.removeEventListener("keydown", closeOnEscape);
  }, [deleting, onClose, saving]);

  const submit = async (event: React.FormEvent) => {
    event.preventDefault();
    const cleanName = name.trim();
    if (!cleanName) { setError("Profile name is required."); return; }
    setSaving(true);
    setError("");
    try {
      const saved = await saveProfile({ ...profile, name: cleanName, theme });
      updateProfile(saved);
      setTheme(saved.theme);
      onClose();
    } catch (requestError) {
      setError(requestError instanceof Error ? requestError.message : "Profile changes could not be saved.");
    } finally { setSaving(false); }
  };

  const remove = async () => {
    if (profile.id === "1" || confirmation !== profile.name) return;
    setDeleting(true);
    setError("");
    try {
      await deleteProfile(profile.id);
      removeProfile(profile.id);
      onDeleted();
    } catch (requestError) {
      setError(requestError instanceof Error ? requestError.message : "Profile could not be deleted.");
    } finally { setDeleting(false); }
  };

  return <div className="profile-dialog app-profile-dialog" data-theme={activeTheme} role="dialog" aria-modal="true" aria-label={`Edit ${profile.name}`} onMouseDown={(event) => { if (event.target === event.currentTarget && !saving && !deleting) onClose(); }}><form className="profile-dialog__panel profile-dialog__panel--edit" onSubmit={submit}><p>PROFILE SETTINGS</p><h2>Edit profile</h2><label><span>Name</span><input value={name} onChange={(event) => setName(event.target.value)} minLength={1} maxLength={40} required autoFocus /></label><ThemeChoices value={theme} onChange={setSelectedTheme} />{error && <p className="profile-dialog__error" role="alert">{error}</p>}<div className="profile-dialog__actions"><button type="submit" disabled={saving || deleting}>{saving ? "Saving..." : "Save changes"}</button><button type="button" onClick={onClose} disabled={saving || deleting}>Cancel</button></div>{profile.id === "1" ? <p className="profile-delete-note">The administrator profile cannot be deleted.</p> : <section className="profile-delete-zone"><h3>Delete profile</h3><p>Type <strong>{profile.name}</strong> to permanently delete this profile.</p><input aria-label="Confirm profile name" value={confirmation} onChange={(event) => setConfirmation(event.target.value)} /><button type="button" disabled={deleting || saving || confirmation !== profile.name} onClick={() => void remove()}>{deleting ? "Deleting..." : "Delete profile"}</button></section>}</form></div>;
}
