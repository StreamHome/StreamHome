import React, { useEffect } from "react";
import { useProfileStore } from "../../stores/profileStore";
import { useThemeStore } from "../../stores/themeStore";
import { DashboardShell } from "./DashboardShell";

export function DashboardRouter() {
  const activeProfile = useProfileStore((state) => state.activeProfile);
  const syncFromProfile = useThemeStore((state) => state.syncFromProfile);
  useEffect(() => syncFromProfile(activeProfile), [activeProfile, syncFromProfile]);
  return <DashboardShell />;
}
