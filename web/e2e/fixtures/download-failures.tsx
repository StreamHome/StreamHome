import React from "react";
import { createRoot } from "react-dom/client";
import { useAuthStore } from "../../src/stores/authStore";
import { useThemeStore } from "../../src/stores/themeStore";
import { ServerDownloads } from "../../src/pages/dashboard/ServerDownloads";
import { EmberDownloads } from "../../src/pages/dashboard/ember/EmberDownloads";
import { DownloadsPanel } from "../../src/pages/admin/panels/DownloadsPanel";
import "../../src/index.css";
import "../../src/themes";

const query = new URLSearchParams(location.search);
const theme = query.get("theme") === "aurora" ? "aurora" : query.get("theme") === "cinema" ? "cinema" : "ember";
const admin = query.get("admin") === "1";
useAuthStore.setState({ isAuthenticated: true, isHydrated: true });
useThemeStore.setState({ activeTheme: theme });
const View = admin ? DownloadsPanel : theme === "ember" ? EmberDownloads : ServerDownloads;
createRoot(document.getElementById("root")!).render(<div data-theme={theme} className="theme-app">
  <div className={admin ? "admin-content" : ""}><View /></div>
</div>);
