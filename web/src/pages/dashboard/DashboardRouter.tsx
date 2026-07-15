import React, { useEffect, useMemo } from "react";
import { useLocation, useNavigate } from "react-router-dom";
import { appUrl, parseAppQuery } from "../../navigation/queryState";
import { useProfileStore } from "../../stores/profileStore";
import { useThemeStore } from "../../stores/themeStore";
import { ThemeDashboard } from "./ThemeDashboard";
import { useCatalogController } from "./useCatalogController";

export function DashboardRouter() {
  const location = useLocation();
  const navigate = useNavigate();
  const activeProfile = useProfileStore((state) => state.activeProfile)!;
  const syncFromProfile = useThemeStore((state) => state.syncFromProfile);
  const query = useMemo(() => parseAppQuery(location.search), [location.search]);
  const controller = useCatalogController(activeProfile, query);

  useEffect(() => syncFromProfile(activeProfile), [activeProfile, syncFromProfile]);
  useEffect(() => {
    if (controller.loading || !query.genre || (query.view !== "movies" && query.view !== "series")) return;
    const valid = controller.genres.some((genre) => genre.toLocaleLowerCase() === query.genre?.toLocaleLowerCase());
    if (!valid) navigate(appUrl(activeProfile.id, query.view), { replace: true });
  }, [activeProfile.id, controller.genres, controller.loading, navigate, query.genre, query.view]);

  return <ThemeDashboard query={query} controller={controller} />;
}
