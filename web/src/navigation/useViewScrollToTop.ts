import { useLayoutEffect } from "react";
import type { AppView } from "./queryState";

export function useViewScrollToTop(view: AppView): void {
  useLayoutEffect(() => {
    const root = document.getElementById("root");
    if (root) {
      root.scrollTop = 0;
      root.scrollLeft = 0;
    }
    window.scrollTo({ top: 0, left: 0, behavior: "auto" });
  }, [view]);
}
