import { useLayoutEffect, useRef, useState } from "react";
import type { AppQueryState } from "../../navigation/queryState";
import type { Movie } from "../../types/api";
import type { CatalogController } from "./useCatalogController";

/** Arrival may offer discovery, but only an explicit action retires a painted catalog. */
export function useCatalogDiscovery(query: AppQueryState, controller: CatalogController) {
  const key = `${query.profile}:${query.view}:${query.genre?.toLocaleLowerCase() ?? "recommended"}`;
  const region = useRef<HTMLDivElement>(null);
  const hasFeed = Boolean(controller.recommendation && (controller.recommendation.items.length || controller.recommendation.watchAgain.length || controller.recommendation.vibeRails?.some((rail) => rail.items.length)));
  const [choice, setChoice] = useState<{ key: string; mode: "awaiting" | "catalog" | "discovery"; items: Movie[] }>({ key, mode: "awaiting", items: [] });
  const current = choice.key === key ? choice : { key, mode: "awaiting" as const, items: [] };
  const mode = current.mode === "awaiting" && hasFeed ? "discovery" : current.mode;
  useLayoutEffect(() => {
    if (choice.key !== key || choice.mode === "awaiting") {
      if (hasFeed) setChoice({ key, mode: "discovery", items: [] });
      else if (!controller.loading) setChoice({ key, mode: "catalog", items: controller.catalogItems });
      else if (choice.key !== key) setChoice({ key, mode: "awaiting", items: [] });
    }
  }, [choice.key, choice.mode, controller.catalogItems, controller.loading, hasFeed, key]);
  const showRecommendations = () => {
    setChoice({ key, mode: "discovery", items: [] });
    region.current?.focus({ preventScroll: true });
  };
  return { region, discovery: mode === "discovery", fallbackItems: current.items, hasFeed, showRecommendations };
}
