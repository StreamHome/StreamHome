import React from "react";
import { act, cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { AccountSectionNavigation } from "./AccountSectionNavigation";

const ids = ["account-authenticator", "account-credentials", "account-sessions", "account-activity"];
const originalScrollIntoView = Object.getOwnPropertyDescriptor(HTMLElement.prototype, "scrollIntoView");
let positions: number[];
let heights: number[];
let navHeight: number;
let observed: Array<{ callback: ResizeObserverCallback; disconnect: ReturnType<typeof vi.fn> }>;

function renderNavigation(rootScroll = false) {
  const view = render(<div id={rootScroll ? "root" : undefined} style={rootScroll ? { overflowY: "auto" } : undefined}>
    <div className="admin-shell" style={{ "--admin-nav-height": "140px" } as React.CSSProperties}>
      <header className="admin-nav" />
      <div className="account-workspace"><AccountSectionNavigation /><main>{ids.map((id) => <section key={id} id={id} tabIndex={-1}>{id}</section>)}</main></div>
    </div>
  </div>);
  const root = rootScroll ? document.getElementById("root")! : null;
  if (root) {
    Object.defineProperties(root, { scrollHeight: { configurable: true, value: 3200 }, clientHeight: { configurable: true, value: 640 } });
    root.scrollTop = 0;
  }
  return { ...view, root, nav: screen.getByRole("navigation", { name: "Account tasks" }) };
}

function currentSection(nav: HTMLElement) {
  return within(nav).getAllByRole("link").filter((link) => link.getAttribute("aria-current") === "location").map((link) => link.getAttribute("href"));
}

describe("AccountSectionNavigation", () => {
  beforeEach(() => {
    positions = [400, 1000, 1800, 3000];
    heights = [500, 700, 800, 150];
    navHeight = 112;
    observed = [];
    window.history.replaceState(null, "", "/?profile=1&view=admin&section=account");
    Object.defineProperty(window, "scrollY", { configurable: true, value: 0, writable: true });
    Object.defineProperty(window, "innerHeight", { configurable: true, value: 800, writable: true });
    Object.defineProperty(document.documentElement, "scrollHeight", { configurable: true, value: 3200 });
    document.documentElement.scrollTop = 0;
    vi.stubGlobal("ResizeObserver", class {
      disconnect = vi.fn();
      observe = vi.fn();
      constructor(callback: ResizeObserverCallback) { observed.push({ callback, disconnect: this.disconnect }); }
    });
    vi.spyOn(HTMLElement.prototype, "getBoundingClientRect").mockImplementation(function (this: HTMLElement) {
      const index = ids.indexOf(this.id);
      const root = document.getElementById("root");
      const scroll = root?.scrollTop ?? window.scrollY;
      const top = root ? 70 : 0;
      if (index >= 0) return new DOMRect(0, positions[index] + top - scroll, 800, heights[index]);
      if (this.id === "root") return new DOMRect(0, 70, 1000, 640);
      if (this.classList.contains("account-section-navigation")) return new DOMRect(0, 140, 224, navHeight);
      return new DOMRect(0, 0, 1000, 400);
    });
  });

  afterEach(() => {
    cleanup();
    vi.restoreAllMocks();
    vi.unstubAllGlobals();
    if (originalScrollIntoView) Object.defineProperty(HTMLElement.prototype, "scrollIntoView", originalScrollIntoView);
    else delete (HTMLElement.prototype as Partial<HTMLElement>).scrollIntoView;
    window.history.replaceState(null, "", "/");
    Object.defineProperty(window, "scrollY", { configurable: true, value: 0, writable: true });
    document.documentElement.scrollTop = 0;
  });

  it("tracks sections in document order using the measured header, including a short final section", async () => {
    const { nav } = renderNavigation();
    expect(within(nav).getAllByRole("link").map((link) => link.getAttribute("href"))).toEqual(ids.map((id) => `#${id}`));
    expect(currentSection(nav)).toEqual(["#account-authenticator"]);
    window.scrollY = 900;
    fireEvent.scroll(window);
    await waitFor(() => expect(currentSection(nav)).toEqual(["#account-credentials"]));
    window.scrollY = 1700;
    fireEvent.scroll(window);
    await waitFor(() => expect(currentSection(nav)).toEqual(["#account-sessions"]));
    // At maximum scroll, the short activity section still starts below the header.
    window.scrollY = 2400;
    fireEvent.scroll(window);
    await waitFor(() => expect(currentSection(nav)).toEqual(["#account-activity"]));
  });

  it("keeps an anchored section current when native scroll rounding leaves its top slightly below the activation line", async () => {
    positions[1] = 747.171875;
    const { nav } = renderNavigation();
    nav.closest<HTMLElement>(".admin-shell")!.style.setProperty("--admin-nav-height", "104px");
    window.scrollY = 631;
    fireEvent.scroll(window);
    // The activation line is 116px, but the settled anchor starts at 116.171875px.
    await waitFor(() => expect(currentSection(nav)).toEqual(["#account-credentials"]));
    window.scrollY = 629;
    fireEvent.scroll(window);
    await waitFor(() => expect(currentSection(nav)).toEqual(["#account-authenticator"]));
    window.scrollY = 631;
    fireEvent.scroll(window);
    await waitFor(() => expect(currentSection(nav)).toEqual(["#account-credentials"]));
  });

  it("uses an overflowing application root instead of document scroll coordinates", async () => {
    const { nav, root } = renderNavigation(true);
    root!.scrollTop = 1700;
    fireEvent.scroll(root!);
    await waitFor(() => expect(currentSection(nav)).toEqual(["#account-sessions"]));
    expect(window.scrollY).toBe(0);
    root!.scrollTop = 2560;
    fireEvent.scroll(root!);
    await waitFor(() => expect(currentSection(nav)).toEqual(["#account-activity"]));
    root!.scrollTop = 0;
    fireEvent.scroll(root!);
    await waitFor(() => expect(currentSection(nav)).toEqual(["#account-authenticator"]));
  });

  it("recalculates after changing section heights and includes the compact sticky menu offset", async () => {
    heights[0] = 580;
    const { nav } = renderNavigation();
    window.scrollY = 800;
    fireEvent.scroll(window);
    await waitFor(() => expect(currentSection(nav)).toEqual(["#account-authenticator"]));
    nav.style.setProperty("--account-navigation-compact", "1");
    fireEvent(window, new Event("resize"));
    await waitFor(() => expect(currentSection(nav)).toEqual(["#account-credentials"]));
    // A QR/recovery panel above credentials can change its position without scrolling.
    positions[1] = 1500;
    heights[0] = 1080;
    act(() => observed[0].callback([], {} as ResizeObserver));
    await waitFor(() => expect(currentSection(nav)).toEqual(["#account-authenticator"]));
    expect(nav.closest<HTMLElement>(".account-workspace")!.style.getPropertyValue("--account-task-nav-height")).toBe("112px");
  });

  it("keeps credentials current across a compact-to-desktop resize when the reading line lands in the card gap", async () => {
    window.scrollY = 666;
    positions[0] = 666 + 71.828;
    heights[0] = 127.078;
    positions[1] = 666 + 218.906;
    heights[1] = 785.359 - 218.906;
    navHeight = 99;
    const { nav } = renderNavigation();
    const shell = nav.closest<HTMLElement>(".admin-shell")!;
    shell.style.setProperty("--admin-nav-height", "108px");
    nav.style.setProperty("--account-navigation-compact", "1");
    fireEvent(window, new Event("resize"));
    await waitFor(() => expect(currentSection(nav)).toEqual(["#account-credentials"]));

    // Measured at 1025px after a 1024px anchor jump: the document keeps scrollY666.
    const url = `${window.location.pathname}${window.location.search}#account-credentials`;
    window.history.replaceState(null, "", url);
    const historyLength = window.history.length;
    const scrollTo = vi.spyOn(window, "scrollTo");
    const scrollIntoView = vi.fn();
    Object.defineProperty(HTMLElement.prototype, "scrollIntoView", { configurable: true, value: scrollIntoView });
    positions[0] = 666 + 28.844;
    positions[1] = 666 + 175.922;
    heights[1] = 764.063 - 175.922;
    navHeight = 247;
    shell.style.setProperty("--admin-nav-height", "155px");
    nav.style.setProperty("--account-navigation-compact", "0");
    await act(async () => {
      fireEvent(window, new Event("resize"));
      observed[0].callback([], {} as ResizeObserver);
      await new Promise<void>((resolve) => window.requestAnimationFrame(() => resolve()));
    });
    expect(nav.closest<HTMLElement>(".account-workspace")!.style.getPropertyValue("--account-task-nav-height")).toBe("247px");
    // Authenticator ends at 155.922px; line 167 is in its 20px gap to Credentials.
    expect(currentSection(nav)).toEqual(["#account-credentials"]);
    expect(window.scrollY).toBe(666);
    expect(`${window.location.pathname}${window.location.search}${window.location.hash}`).toBe(url);
    expect(window.history.length).toBe(historyLength);
    expect(scrollTo).not.toHaveBeenCalled();
    expect(scrollIntoView).not.toHaveBeenCalled();
  });

  it("keeps intervening recovery content under the authenticator location instead of treating it as a card gap", async () => {
    positions[1] = 1150;
    const { nav } = renderNavigation();
    const recovery = document.createElement("section");
    recovery.textContent = "Save your recovery codes now";
    document.getElementById("account-authenticator")!.after(recovery);
    window.scrollY = 940;
    await act(async () => {
      fireEvent.scroll(window);
      await new Promise<void>((resolve) => window.requestAnimationFrame(() => resolve()));
    });
    expect(currentSection(nav)).toEqual(["#account-authenticator"]);
    window.scrollY = 1150;
    fireEvent.scroll(window);
    await waitFor(() => expect(currentSection(nav)).toEqual(["#account-credentials"]));
  });

  it("honors anchors mounted after the gate and subsequent hash history without changing query state", async () => {
    const scrollIntoView = vi.fn(function (this: HTMLElement) {
      window.scrollY = Math.min(2400, positions[ids.indexOf(this.id)] - 152);
    });
    Object.defineProperty(HTMLElement.prototype, "scrollIntoView", { configurable: true, value: scrollIntoView });
    window.history.replaceState(null, "", "/?profile=1&view=admin&section=account#account-activity");
    const { nav } = renderNavigation();
    await waitFor(() => expect(currentSection(nav)).toEqual(["#account-activity"]));
    expect(scrollIntoView).toHaveBeenCalledWith({ block: "start", behavior: "auto" });
    window.history.pushState(null, "", "#account-credentials");
    fireEvent(window, new HashChangeEvent("hashchange"));
    await waitFor(() => expect(currentSection(nav)).toEqual(["#account-credentials"]));
    window.history.replaceState(null, "", "#account-activity");
    fireEvent(window, new PopStateEvent("popstate"));
    fireEvent(window, new HashChangeEvent("hashchange"));
    await waitFor(() => expect(currentSection(nav)).toEqual(["#account-activity"]));
    expect(window.location.search).toBe("?profile=1&view=admin&section=account");
    delete (HTMLElement.prototype as Partial<HTMLElement>).scrollIntoView;
  });

  it("disconnects observers, removes listeners and cancels pending measurement on unmount", () => {
    const removeListener = vi.spyOn(window, "removeEventListener");
    const cancelFrame = vi.spyOn(window, "cancelAnimationFrame");
    const { nav, unmount } = renderNavigation();
    const workspace = nav.closest<HTMLElement>(".account-workspace")!;
    fireEvent(window, new Event("resize"));
    unmount();
    expect(observed[0].disconnect).toHaveBeenCalledOnce();
    expect(removeListener.mock.calls.some(([name, , capture]) => name === "scroll" && capture === true)).toBe(true);
    expect(removeListener.mock.calls.some(([name]) => name === "hashchange")).toBe(true);
    expect(cancelFrame).toHaveBeenCalled();
    expect(workspace.style.getPropertyValue("--account-task-nav-height")).toBe("");
  });
});
