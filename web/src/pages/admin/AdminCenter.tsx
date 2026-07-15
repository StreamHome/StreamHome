import React, { useMemo } from "react";
import { useLocation, useNavigate } from "react-router-dom";
import { appUrl, parseAppQuery, type AdminSection } from "../../navigation/queryState";
import { useProfileStore } from "../../stores/profileStore";
import { useThemeStore } from "../../stores/themeStore";
import { getThemeDefinition } from "../../themes/application/themeRegistry";
import { AccountPanel } from "./panels/AccountPanel";
import { DownloadsPanel } from "./panels/DownloadsPanel";
import { StoragePanel } from "./panels/StoragePanel";

const PANELS: Array<{ id: AdminSection; label: string }> = [
  { id: "account", label: "Account & TOTP" }, { id: "storage", label: "Storage & HEVC" }, { id: "downloads", label: "Downloads" },
];

export function AdminCenter() {
  const navigate = useNavigate();
  const location = useLocation();
  const profile = useProfileStore((state) => state.activeProfile)!;
  const theme = useThemeStore((state) => state.activeTheme);
  const definition = getThemeDefinition(theme);
  const query = useMemo(() => parseAppQuery(location.search), [location.search]);
  const section = query.section ?? "account";
  const Background = definition.Background;
  const select = (next: AdminSection) => navigate(appUrl(profile.id, "admin", { section: next }));

  return <div className={`theme-app admin-shell ${definition.shellClass}`} data-theme={theme}><Background /><header className="admin-nav"><div><p>STREAMHOME / CONTROL PLANE</p><h1>Admin center</h1></div><nav aria-label="Admin sections">{PANELS.map((panel) => <button key={panel.id} data-active={section === panel.id} onClick={() => select(panel.id)}>{panel.label}</button>)}</nav><div className="admin-nav__profile"><span>{profile.name}</span><button onClick={() => navigate(appUrl(profile.id, "home"))}>Exit admin</button></div></header><main className="admin-content">{section === "account" && <AccountPanel />}{section === "storage" && <StoragePanel />}{section === "downloads" && <DownloadsPanel />}</main></div>;
}
