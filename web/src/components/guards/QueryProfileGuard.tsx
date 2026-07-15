import React, { useCallback, useEffect, useMemo, useState } from "react";
import { Navigate, useLocation } from "react-router-dom";
import { getProfiles } from "../../api/profiles";
import { appSearch, appUrl, parseAppQuery } from "../../navigation/queryState";
import { useProfileStore } from "../../stores/profileStore";
import { useThemeStore } from "../../stores/themeStore";

export function QueryProfileGuard({ children }: { children: React.ReactNode }) {
  const location = useLocation();
  const profiles = useProfileStore((state) => state.profiles);
  const activeProfile = useProfileStore((state) => state.activeProfile);
  const setProfiles = useProfileStore((state) => state.setProfiles);
  const selectProfile = useProfileStore((state) => state.selectProfile);
  const setTheme = useThemeStore((state) => state.setTheme);
  const [loading, setLoading] = useState(profiles.length === 0);
  const [error, setError] = useState("");
  const query = useMemo(() => parseAppQuery(location.search), [location.search]);

  const load = useCallback(async () => {
    if (profiles.length) return;
    setLoading(true);
    setError("");
    try { setProfiles(await getProfiles()); }
    catch (requestError) { setError(requestError instanceof Error ? requestError.message : "Profiles could not be loaded."); }
    finally { setLoading(false); }
  }, [profiles.length, setProfiles]);

  useEffect(() => { void load(); }, [load]);

  const requestedProfile = profiles.find((profile) => profile.id === query.profile) ?? null;
  useEffect(() => {
    if (!requestedProfile || activeProfile?.id === requestedProfile.id) return;
    selectProfile(requestedProfile);
    setTheme(requestedProfile.theme);
  }, [activeProfile?.id, requestedProfile, selectProfile, setTheme]);

  if (!query.profile) return <Navigate to="/profiles" state={{ from: location }} replace />;
  if (loading) return <div className="app-guard-state" aria-label="Loading profile">Loading profile from server...</div>;
  if (error) return <div className="app-guard-state"><p>{error}</p><button onClick={() => void load()}>Retry</button></div>;
  if (!requestedProfile) return <Navigate to="/profiles" state={{ error: "That profile is not available on the server." }} replace />;
  if (query.view === "admin" && requestedProfile.id !== "1") return <Navigate to={appUrl(requestedProfile.id, "home")} replace />;

  const canonicalSearch = appSearch(query);
  if (location.pathname !== "/" || location.search !== canonicalSearch) return <Navigate to={`/${canonicalSearch}`} replace />;
  if (activeProfile?.id !== requestedProfile.id) return <div className="app-guard-state" aria-label="Applying profile">Applying profile...</div>;
  return children;
}
