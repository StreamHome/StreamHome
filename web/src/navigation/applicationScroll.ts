/** Reset the active page without touching exiting catalogs or horizontal rails. */
export function resetApplicationScroll(): void {
  const panel = document.querySelector<HTMLElement>('.catalog-layout[data-catalog-present="true"] > [data-catalog-scroll]');
  if (panel) {
    panel.scrollTop = 0;
    panel.scrollLeft = 0;
  }
  const root = document.getElementById("root");
  if (root) {
    root.scrollTop = 0;
    root.scrollLeft = 0;
  }
  window.scrollTo({ top: 0, left: 0, behavior: "auto" });
}
