import React from "react";
import { Navigate, useLocation, useParams } from "react-router-dom";
import { appUrl } from "./queryState";
import { useProfileStore } from "../stores/profileStore";

export function LegacyWatchRedirect() {
  const { mediaId = "" } = useParams();
  const location = useLocation();
  const profile = useProfileStore((state) => state.activeProfile);
  const profileId = profile?.id ?? localStorage.getItem("streamhome_profile");
  return profileId
    ? <Navigate to={appUrl(profileId, "watch", { media: mediaId })} replace />
    : <Navigate to="/profiles" state={{ from: location }} replace />;
}

export function LegacyAdminRedirect() {
  const location = useLocation();
  const profile = useProfileStore((state) => state.activeProfile);
  const profileId = profile?.id ?? localStorage.getItem("streamhome_profile");
  const rawSection = location.pathname.split("/").filter(Boolean)[1];
  const section = rawSection === "security" || rawSection === "storage" || rawSection === "downloads" ? rawSection : "account";
  return profileId
    ? <Navigate to={appUrl(profileId, "admin", { section })} replace />
    : <Navigate to="/profiles" state={{ from: location }} replace />;
}

export function LegacyAccountSecurityRedirect() {
  const location = useLocation();
  const profile = useProfileStore((state) => state.activeProfile);
  const profileId = profile?.id ?? localStorage.getItem("streamhome_profile");
  return profileId
    ? <Navigate to={appUrl(profileId, "admin", { section: "security" })} replace />
    : <Navigate to="/profiles" state={{ from: location }} replace />;
}

export function AppFallbackRedirect() {
  const profile = useProfileStore((state) => state.activeProfile);
  const profileId = profile?.id ?? localStorage.getItem("streamhome_profile");
  return <Navigate to={profileId ? appUrl(profileId, "home") : "/profiles"} replace />;
}
