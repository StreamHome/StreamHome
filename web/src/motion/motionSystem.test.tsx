import React from "react";
import { renderHook } from "@testing-library/react";
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

describe("cinematic motion system", () => {
  beforeEach(() => Object.defineProperty(window, "matchMedia", { configurable: true, value: () => matchMedia(false) }));

  it("keeps interactions responsive and cinematic transitions deliberate", () => {
    expect(MOTION_TIMINGS.menu).toBe(.26);
    expect(MOTION_TIMINGS.menuItem).toBe(.22);
    expect(MOTION_TIMINGS.dialog).toBe(.42);
    expect(MOTION_TIMINGS.viewExit).toBe(.28);
    expect(MOTION_TIMINGS.viewEnter).toBe(.52);
    expect(MOTION_TIMINGS.view).toBe(.8);
    expect(MOTION_TIMINGS.viewExit + MOTION_TIMINGS.viewEnter).toBe(MOTION_TIMINGS.view);
    expect(MOTION_TIMINGS.menuExit).toBeLessThan(MOTION_TIMINGS.menuEnter);
    expect(MOTION_TIMINGS.dialogExit).toBeLessThan(MOTION_TIMINGS.dialogEnter);
    expect(MOTION_TIMINGS.controlsEnter).toBeLessThan(MOTION_TIMINGS.controlsExit);
    expect(MOTION_TIMINGS.rail).toBe(1080);
    expect(MOTION_TIMINGS.billboardExit).toBe(.88);
    expect(MOTION_TIMINGS.billboardEnter).toBe(1.12);
    expect(MOTION_TIMINGS.billboardExit + MOTION_TIMINGS.billboardEnter).toBe(MOTION_TIMINGS.billboard);
    expect(MOTION_TIMINGS.profileMorph).toBe(.95);
    expect(MOTION_TIMINGS.profileEntry).toBe(.62);
    expect(MOTION_TIMINGS.reduced).toBeGreaterThanOrEqual(.16);
  });

  it("defines distinct view and billboard choreography for every theme", () => {
    const definitions = Object.values(THEME_MOTION);
    const resolve = (variant: unknown) => typeof variant === "function" ? variant(1) : variant;
    expect(definitions).toHaveLength(4);
    expect(new Set(definitions.map((definition) => JSON.stringify(resolve(definition.view.initial)))).size).toBe(4);
    expect(definitions.every((definition) => resolve(definition.billboard.initial))).toBe(true);
    expect(new Set(definitions.map((definition) => JSON.stringify(definition.billboardTiming))).size).toBe(4);
  });

  it("exposes normal and reduced preferences through one provider", () => {
    const wrapper = ({ children }: { children: React.ReactNode }) => <MotionProvider>{children}</MotionProvider>;
    const { result } = renderHook(() => useAppMotion(), { wrapper });
    expect(result.current.reduced).toBe(false);
    Object.defineProperty(window, "matchMedia", { configurable: true, value: () => matchMedia(true) });
    const reducedWrapper = ({ children }: { children: React.ReactNode }) => <MotionProvider>{children}</MotionProvider>;
    const reduced = renderHook(() => useAppMotion(), { wrapper: reducedWrapper });
    expect(reduced.result.current.reduced).toBe(true);
  });
});
