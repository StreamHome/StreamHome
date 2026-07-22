import React from "react";
import { act, renderHook } from "@testing-library/react";
import { beforeEach, describe, expect, it } from "vitest";
import { MOTION_TIMINGS, MotionProvider, THEME_MOTION, useAppMotion } from "./motionSystem";

function matchMedia(matches: boolean) {
  return {
    matches,
    media: "(prefers-reduced-motion: reduce)",
    onchange: null,
    addListener() {},
    removeListener() {},
    addEventListener() {},
    removeEventListener() {},
    dispatchEvent: () => true,
  } as MediaQueryList;
}

describe("fluid motion system", () => {
  beforeEach(() => {
    window.localStorage.clear();
    delete document.documentElement.dataset.motionPreference;
    Object.defineProperty(window, "matchMedia", { configurable: true, value: () => matchMedia(false) });
  });

  it("keeps every interaction inside a responsive motion budget", () => {
    expect(MOTION_TIMINGS.menu).toBe(.16);
    expect(MOTION_TIMINGS.menuItem).toBe(.14);
    expect(MOTION_TIMINGS.dialog).toBe(.22);
    expect(MOTION_TIMINGS.viewExit).toBe(.14);
    expect(MOTION_TIMINGS.viewEnter).toBe(.24);
    expect(MOTION_TIMINGS.view).toBe(.24);
    expect(MOTION_TIMINGS.viewEnter).toBeLessThanOrEqual(.28);
    expect(MOTION_TIMINGS.menuExit).toBeLessThan(MOTION_TIMINGS.menuEnter);
    expect(MOTION_TIMINGS.dialogExit).toBeLessThan(MOTION_TIMINGS.dialogEnter);
    expect(MOTION_TIMINGS.controlsExit).toBeLessThan(MOTION_TIMINGS.controlsEnter);
    expect(MOTION_TIMINGS.rail).toBe(340);
    expect(MOTION_TIMINGS.billboardExit).toBe(.24);
    expect(MOTION_TIMINGS.billboardEnter).toBe(.38);
    expect(MOTION_TIMINGS.billboard).toBe(.38);
    expect(MOTION_TIMINGS.profileMorph).toBe(.28);
    expect(MOTION_TIMINGS.profileEntry).toBe(.22);
    expect(MOTION_TIMINGS.reduced).toBeLessThanOrEqual(.1);
  });

  it("defines distinct view and billboard choreography for every theme", () => {
    const definitions = Object.values(THEME_MOTION);
    const resolve = (variant: unknown) => typeof variant === "function" ? variant(1) : variant;
    expect(definitions).toHaveLength(4);
    expect(new Set(definitions.map((definition) => JSON.stringify(resolve(definition.view.initial)))).size).toBe(4);
    expect(definitions.every((definition) => resolve(definition.billboard.initial))).toBe(true);
    expect(new Set(definitions.map((definition) => JSON.stringify(definition.billboardTiming))).size).toBe(4);
    expect(definitions.every((definition) => !JSON.stringify(definition).includes("filter"))).toBe(true);
    expect(definitions.every((definition) => definition.billboardTiming.enter <= .42)).toBe(true);
  });

  it("defaults to full motion instead of silently inheriting a false browser reduction", () => {
    Object.defineProperty(window, "matchMedia", { configurable: true, value: () => matchMedia(true) });
    const wrapper = ({ children }: { children: React.ReactNode }) => <MotionProvider>{children}</MotionProvider>;
    const { result } = renderHook(() => useAppMotion(), { wrapper });
    expect(result.current.reduced).toBe(false);
    expect(result.current.preference).toBe("full");
    expect(document.documentElement.dataset.motionPreference).toBe("full");
  });

  it("persists explicit system and reduced motion choices", () => {
    Object.defineProperty(window, "matchMedia", { configurable: true, value: () => matchMedia(true) });
    const wrapper = ({ children }: { children: React.ReactNode }) => <MotionProvider>{children}</MotionProvider>;
    const { result } = renderHook(() => useAppMotion(), { wrapper });
    act(() => result.current.setPreference("system"));
    expect(result.current.reduced).toBe(true);
    expect(window.localStorage.getItem("streamhome.motion-preference")).toBe("system");
    act(() => result.current.setPreference("reduced"));
    expect(result.current.preference).toBe("reduced");
  });
});
