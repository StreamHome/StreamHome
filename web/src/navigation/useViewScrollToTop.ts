import { useLayoutEffect } from "react";
import type { AdminSection, AppView } from "./queryState";
import { resetApplicationScroll } from "./applicationScroll";

export function useViewScrollToTop(view: AppView | AdminSection): void {
  useLayoutEffect(() => {
    resetApplicationScroll();
  }, [view]);
}
