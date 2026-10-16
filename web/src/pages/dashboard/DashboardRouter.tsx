import React, { useEffect, useMemo } from "react";
import { useLocation, useNavigate } from "react-router-dom";
import { appUrl, parseAppQuery } from "../../navigation/queryState";
import { useProfileStore } from "../../stores/profileStore";
import { useThemeStore } from "../../stores/themeStore";
import { getThemeDefinition } from "../../themes/application/themeRegistry";
import { useCatalogController } from "./useCatalogController";

export function DashboardRouter() {
  const location = useLocation();
  const navigate = useNavigate();
  const activeProfile = useProfileStore((state) => state.activeProfile)!;
  const activeTheme = useThemeStore((state) => state.activeTheme);
  const syncFromProfile = useThemeStore((state) => state.syncFromProfile);
  const query = useMemo(() => parseAppQuery(location.search), [location.search]);
  const controller = useCatalogController(activeProfile, query, activeTheme !== "gemini");

  useEffect(() => syncFromProfile(activeProfile), [activeProfile, syncFromProfile]);
  useEffect(() => {
    if (!controller.categoriesReady || !query.genre || (query.view !== "movies" && query.view !== "series")) return;
    const valid = controller.categories.some((category) => category.toLocaleLowerCase() === query.genre?.toLocaleLowerCase());
    if (!valid) navigate(appUrl(activeProfile.id, query.view), { replace: true });
  }, [activeProfile.id, controller.categories, controller.categoriesReady, navigate, query.genre, query.view]);

  const presentation = getThemeDefinition(activeTheme);
  const Application = presentation.Application;
  return <Application query={query} controller={controller} presentation={presentation} />;
}
