import { useEffect, useLayoutEffect, useMemo, useRef, useState } from "react";
import type { AppQueryState, CatalogView } from "../../navigation/queryState";
import { buildCatalogLanding } from "./catalogLanding";
import type { CatalogController } from "./useCatalogController";

/** Automatic handoff waits only for a current focused/pressed control to finish. */
export function useCatalogLanding(query: AppQueryState, controller: CatalogController) {
  const region = useRef<HTMLDivElement>(null);
  const gesture = useRef(false);
  const [interaction, setInteraction] = useState(0);
  const requestKey = `${query.profile}:${query.view}:${query.genre?.trim().toLocaleLowerCase() ?? "recommended"}`;
  const next = useMemo(() => buildCatalogLanding({ feed: controller.recommendation, catalog: controller.catalogItems, continueWatching: controller.continueWatching, view: query.view as CatalogView, category: query.genre, preferences: controller.preferences }), [controller.recommendation, controller.catalogItems, controller.continueWatching, controller.preferences, query.view, query.genre]);
  const [painted, setPainted] = useState({ key: requestKey, model: next });
  const sameRequest = painted.key === requestKey;
  const model = sameRequest ? painted.model : next;
  useLayoutEffect(() => {
    const focused = document.activeElement;
    const usingControl = focused instanceof HTMLElement && focused !== region.current && region.current?.contains(focused) && focused.matches("button,a,input,select,textarea,[role=slider]") && !focused.closest(".catalog-landing-notice");
    if (sameRequest && (gesture.current || usingControl)) return;
    if (!sameRequest) gesture.current = false;
    if (painted.model !== next || !sameRequest) setPainted({ key: requestKey, model: next });
  }, [next, painted.model, requestKey, sameRequest, interaction]);
  useEffect(() => {
    const element = region.current;
    let timer: ReturnType<typeof setTimeout> | undefined;
    const finish = () => {
      clearTimeout(timer);
      // Let click/blur dispatch finish against the record that received pointerdown.
      timer = setTimeout(() => { gesture.current = false; setInteraction((value) => value + 1); }, 0);
    };
    const start = () => { gesture.current = true; };
    element?.addEventListener("pointerdown", start);
    element?.addEventListener("focusout", finish);
    document.addEventListener("pointerup", finish);
    document.addEventListener("pointercancel", finish);
    window.addEventListener("blur", finish);
    return () => { clearTimeout(timer); element?.removeEventListener("pointerdown", start); element?.removeEventListener("focusout", finish); document.removeEventListener("pointerup", finish); document.removeEventListener("pointercancel", finish); window.removeEventListener("blur", finish); };
  }, []);
  const hasContent = Boolean(model.billboardItems.length || model.sections.length || model.gridItems.length);
  const authoritativeEmpty = model.source === "recommendations" && !controller.recommendationLoading;
  const pending = !hasContent && !authoritativeEmpty && (controller.loading || controller.recommendationLoading);
  const error = !hasContent && !pending && !authoritativeEmpty ? controller.catalogError || controller.recommendationError : "";
  return { region, model, hasContent, pending, error };
}
