import React from "react";
import { act, cleanup, fireEvent, render, renderHook, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { MotionProvider } from "../../motion/motionSystem";
import { BillboardStack } from "./BillboardStack";
import { catalogMovie } from "./catalogTestFixtures.nonplayer";
import { ROTATION_INTERVAL, useRotatingFeature } from "./useRotatingFeature";
const movies = [catalogMovie("one"), catalogMovie("two"), catalogMovie("three")];
const wrapper = ({ children }: { children: React.ReactNode }) => <MotionProvider>{children}</MotionProvider>;
beforeEach(() => { Object.defineProperty(window, "matchMedia", { configurable: true, value: () => ({ matches: false, addEventListener() {}, removeEventListener() {} }) }); });
afterEach(() => { cleanup(); vi.useRealTimers(); });
function Hero({ items = movies }: { items?: typeof movies }) { return <MotionProvider><div data-catalog-region tabIndex={-1} role="region" aria-label="Catalog"><BillboardStack items={items} theme="ember" className="ember-billboard" paginationClassName="ember-billboard__pagination" render={(movie, measuring) => <section><h1>{movie.title}</h1>{!measuring && <img src={`/synthetic/${movie.id}.svg`} alt="" />}<button disabled={measuring} onClick={measuring ? undefined : () => {}}>View details {movie.title}</button></section>} /></div></MotionProvider>; }

describe("included stable billboard layers", () => {
  it("reserves the entire bounded roster as inert non-artwork geometry and exposes one active slide", () => {
    const { container } = render(<Hero />);
    const measurements = container.querySelectorAll(".billboard-measurement");
    expect(measurements.length).toBe(3);
    measurements.forEach((layer) => { expect(layer.hasAttribute("inert")).toBe(true); expect(layer.getAttribute("aria-hidden")).toBe("true"); expect(layer.querySelector("img")).toBeNull(); expect(layer.querySelector("button")?.disabled).toBe(true); });
    expect(container.querySelectorAll(".billboard-slide[data-active=true]").length).toBe(1);
    expect(screen.getAllByRole("button", { name: /View details/ }).length).toBe(1);
    fireEvent.click(screen.getByRole("button", { name: "Show three" }));
    container.querySelectorAll(".billboard-slide[data-active=false]").forEach((layer) => { expect(layer.hasAttribute("inert")).toBe(true); expect(layer.getAttribute("aria-hidden")).toBe("true"); });
    expect(screen.getByRole("button", { name: "View details three" })).toBeTruthy();
  });
  it("retires programmatic action focus before manual change and uses the persistent anchor on roster collapse", () => {
    const { rerender } = render(<Hero />); const anchor = screen.getByRole("region", { name: "Catalog" });
    act(() => screen.getByRole("button", { name: "View details one" }).focus());
    fireEvent.click(screen.getByRole("button", { name: "Show two" }));
    expect(document.activeElement).toBe(anchor);
    const pause = screen.getByRole("button", { name: "Pause automatic rotation" });
    act(() => pause.focus()); rerender(<Hero items={[movies[0]]} />);
    expect(document.activeElement).toBe(anchor);
    act(() => screen.getByRole("button", { name: "View details one" }).focus()); rerender(<Hero items={[]} />);
    expect(document.activeElement).toBe(anchor);
    expect(screen.queryByRole("button", { name: "Pause automatic rotation" })).toBeNull();
  });
  it("keeps pagination button identity on countdown reset and rapid changes", () => {
    render(<Hero />); const marker = screen.getByRole("button", { name: "Show one" });
    act(() => marker.focus()); fireEvent.click(screen.getByRole("button", { name: "Show two" })); fireEvent.click(marker); fireEvent.click(screen.getByRole("button", { name: "Show three" }));
    expect(screen.getByRole("button", { name: "Show one" })).toBe(marker);
    expect(document.activeElement).toBe(marker);
    expect(screen.getByRole("button", { name: "Show three" }).getAttribute("aria-current")).toBe("true");
  });
  it("keeps user pause independent from focus, hidden document and manual pagination", () => {
    vi.useFakeTimers();
    const { result } = renderHook(() => useRotatingFeature(movies), { wrapper });
    act(() => vi.advanceTimersByTime(4_000)); act(() => result.current.setUserPaused(true));
    act(() => result.current.setPaused(true)); act(() => result.current.setPaused(false));
    act(() => { Object.defineProperty(document, "hidden", { configurable: true, value: true }); document.dispatchEvent(new Event("visibilitychange")); });
    act(() => { Object.defineProperty(document, "hidden", { configurable: true, value: false }); document.dispatchEvent(new Event("visibilitychange")); });
    expect(result.current.userPaused).toBe(true); expect(result.current.paused).toBe(true);
    act(() => vi.advanceTimersByTime(ROTATION_INTERVAL * 3)); expect(result.current.featured?.id).toBe("one");
    act(() => result.current.setIndex(2)); expect(result.current.userPaused).toBe(true);
    act(() => vi.advanceTimersByTime(ROTATION_INTERVAL * 3)); expect(result.current.featured?.id).toBe("three");
    act(() => result.current.setUserPaused(false)); act(() => vi.advanceTimersByTime(ROTATION_INTERVAL - 1)); expect(result.current.featured?.id).toBe("three");
    act(() => vi.advanceTimersByTime(1)); expect(result.current.featured?.id).toBe("one");
  });
  it("preserves user pause across roster replacement and keeps active-marker selection a no-op", () => {
    vi.useFakeTimers(); const { result, rerender, unmount } = renderHook(({ items }) => useRotatingFeature(items), { wrapper, initialProps: { items: movies } });
    act(() => result.current.setUserPaused(true)); const generation = result.current.countdownGeneration;
    act(() => result.current.setIndex(0)); expect(result.current.countdownGeneration).toBe(generation);
    rerender({ items: [movies[0], catalogMovie("replacement")] });
    expect(result.current.userPaused).toBe(true); expect(result.current.countdownGeneration).toBeGreaterThan(generation);
    act(() => vi.advanceTimersByTime(ROTATION_INTERVAL * 3)); expect(result.current.featured?.id).toBe("one");
    unmount(); expect(vi.getTimerCount()).toBe(0);
  });
  it("checks live focus before an already queued automatic timer retires a slide", () => {
    vi.useFakeTimers(); const beforeRetire = vi.fn(); let safe = true;
    const { result } = renderHook(() => useRotatingFeature(movies, { beforeRetire, canRotate: () => safe }), { wrapper });
    safe = false; act(() => vi.advanceTimersByTime(ROTATION_INTERVAL));
    expect(result.current.featured?.id).toBe("one"); expect(beforeRetire).not.toHaveBeenCalled(); expect(result.current.paused).toBe(true);
  });
});
