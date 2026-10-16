import React from "react";
import { render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { describe, expect, it, vi } from "vitest";
import { AuthenticatedApp } from "./AuthenticatedApp";

vi.mock("./player/PlayerPage", () => ({ PlayerPage: () => <div>Loaded player</div> }));

describe("watch route entry", () => {
  it("uses a landscape fallback before lazy loading without transforming its fixed-position ancestor", async () => {
    const width = Object.getOwnPropertyDescriptor(window, "innerWidth")!;
    const height = Object.getOwnPropertyDescriptor(window, "innerHeight")!;
    const touch = Object.getOwnPropertyDescriptor(navigator, "maxTouchPoints");
    Object.defineProperty(window, "innerWidth", { configurable: true, value: 390 });
    Object.defineProperty(window, "innerHeight", { configurable: true, value: 844 });
    Object.defineProperty(navigator, "maxTouchPoints", { configurable: true, value: 1 });
    const scroll = vi.spyOn(window, "scrollTo").mockImplementation(() => undefined);
    const view = render(<MemoryRouter initialEntries={["/?profile=entry&view=watch&media=m_entry"]}><AuthenticatedApp /></MemoryRouter>);
    try {
      const loading = screen.getByRole("status", { name: "Loading player" });
      expect(loading.dataset.mobileOrientation).toBe("forced-landscape");
      expect(loading.style.getPropertyValue("--mobile-player-width")).toBe("844px");
      expect(loading.style.getPropertyValue("--mobile-player-height")).toBe("390px");
      const ancestor = loading.closest<HTMLElement>(".standalone-motion-view")!;
      expect(ancestor.style.transform).toBe("");
      await screen.findByText("Loaded player");
      expect(ancestor.style.transform).toBe("");
    } finally {
      view.unmount();
      scroll.mockRestore();
      Object.defineProperty(window, "innerWidth", width);
      Object.defineProperty(window, "innerHeight", height);
      if (touch) Object.defineProperty(navigator, "maxTouchPoints", touch);
      else Reflect.deleteProperty(navigator, "maxTouchPoints");
    }
  });
});
