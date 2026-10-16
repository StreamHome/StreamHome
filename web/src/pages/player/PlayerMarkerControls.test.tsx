import React from "react";
import { act, cleanup, fireEvent, render } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { PLAYER_SKIP_COUNTDOWN_DELAY_MS, PLAYER_SKIP_COUNTDOWN_DURATION_MS, PlayerCaptionLayer, PlayerSkipControl, PlayerTimelineShell } from "./PlayerMarkerControls";
import type { MarkerRegion } from "./playerMarkers";

const region: MarkerRegion = { id: "credits:80:100:0", kind: "credits", label: "Credits", start: 80, end: 100 };
const ROOT_WIDTH = 1000;
const ROOT_HEIGHT = 600;
const TRACK_LEFT = 40;
const TRACK_TOP = 480;
const TRACK_WIDTH = 920;
const TRACK_HEIGHT = 16;
const BUTTON_WIDTH = 120;
const BUTTON_HEIGHT = 40;
const PREVIEW_WIDTH = 180.625;
const PREVIEW_HEIGHT = 28.375;
const RIGHT_EDGE_PREVIEW = { x: TRACK_WIDTH - 1, time: 95 };

interface Rect { left: number; top: number; width: number; height: number }

function installLayout({ rotated = false, translation = 18, captionTranslation = 0 }: { rotated?: boolean; translation?: number; captionTranslation?: number } = {}) {
  const physicalRect = ({ left, top, width, height }: Rect) => rotated
    ? new DOMRect(100 + ROOT_HEIGHT - top - height, 50 + left, height, width)
    : new DOMRect(100 + left, 50 + top, width, height);

  vi.spyOn(HTMLElement.prototype, "getBoundingClientRect").mockImplementation(function (this: HTMLElement) {
    if (this.dataset.playerRoot === "true") {
      return new DOMRect(100, 50, rotated ? ROOT_HEIGHT : ROOT_WIDTH, rotated ? ROOT_WIDTH : ROOT_HEIGHT);
    }
    if (this.classList.contains("player-timeline-shell")) {
      return physicalRect({ left: TRACK_LEFT, top: TRACK_TOP + translation, width: TRACK_WIDTH, height: TRACK_HEIGHT });
    }
    if (this.classList.contains("player-skip-control")) {
      return physicalRect({
        left: this.style.left ? Number.parseFloat(this.style.left) : ROOT_WIDTH - BUTTON_WIDTH - 24,
        top: this.style.bottom ? ROOT_HEIGHT - Number.parseFloat(this.style.bottom) - BUTTON_HEIGHT : 500,
        width: BUTTON_WIDTH, height: BUTTON_HEIGHT,
      });
    }
    if (this.parentElement?.classList.contains("player-caption-layer")) {
      return physicalRect({ left: 600, top: 410 + captionTranslation, width: 360, height: 58 });
    }
    if (this.classList.contains("player-caption-layer")) {
      return physicalRect({ left: 16, top: 410 + captionTranslation, width: 968, height: 58 });
    }
    if (this.classList.contains("player-timeline-preview")) {
      return physicalRect({
        left: TRACK_LEFT + Number.parseFloat(this.style.left) - PREVIEW_WIDTH / 2,
        // Absolute bottom offsets are measured from the shell's bottom edge.
        top: TRACK_TOP + translation + TRACK_HEIGHT - (Number.parseFloat(this.style.bottom) || 0) - PREVIEW_HEIGHT,
        width: PREVIEW_WIDTH, height: PREVIEW_HEIGHT,
      });
    }
    return new DOMRect();
  });
  vi.spyOn(HTMLElement.prototype, "clientWidth", "get").mockImplementation(function (this: HTMLElement) {
    return this.dataset.playerRoot === "true" ? ROOT_WIDTH : this.classList.contains("player-timeline-shell") ? TRACK_WIDTH : 0;
  });
  vi.spyOn(HTMLElement.prototype, "clientHeight", "get").mockImplementation(function (this: HTMLElement) {
    return this.dataset.playerRoot === "true" ? ROOT_HEIGHT : this.classList.contains("player-timeline-shell") ? TRACK_HEIGHT : 0;
  });
  vi.spyOn(HTMLElement.prototype, "offsetWidth", "get").mockImplementation(function (this: HTMLElement) {
    return this.classList.contains("player-timeline-preview") ? Math.round(PREVIEW_WIDTH) : 0;
  });
  vi.spyOn(HTMLElement.prototype, "offsetHeight", "get").mockImplementation(function (this: HTMLElement) {
    return this.classList.contains("player-timeline-preview") ? Math.round(PREVIEW_HEIGHT) : 0;
  });
  vi.stubGlobal("DOMMatrixReadOnly", class {
    m42: number;
    constructor(value: string) {
      this.m42 = Number(value.slice(value.indexOf("(") + 1, value.lastIndexOf(")")).split(",")[5]);
    }
  });
  const computedStyle = window.getComputedStyle.bind(window);
  vi.spyOn(window, "getComputedStyle").mockImplementation((element) => {
    const style = computedStyle(element);
    if (element.classList.contains("player-caption-layer")) {
      Object.defineProperty(style, "transform", { value: `matrix(1, 0, 0, 1, 0, ${captionTranslation})` });
    }
    return style;
  });
  // Resize events exercise the component's actual fallback when native observers
  // are unavailable, without running any asynchronous browser layout emulation.
  vi.stubGlobal("ResizeObserver", undefined);
}

function Fixture({ caption = false, preview = false, skip = true, rotated = false, translation = 18, mobile = rotated, controlsVisible = true, inset = 5 }: {
  caption?: boolean;
  preview?: boolean;
  skip?: boolean;
  rotated?: boolean;
  translation?: number;
  mobile?: boolean;
  controlsVisible?: boolean;
  inset?: number;
}) {
  return <div data-player-root="true" data-mobile-player={mobile ? "true" : "false"} data-controls-visible={String(controlsVisible)} data-mobile-orientation={rotated ? "forced-landscape" : "native-landscape"}>
    <PlayerCaptionLayer signature={caption ? "long-cue" : ""} mobile={mobile} reduced={false}>
      {caption && <span>A long subtitle cue extends into the contextual action's normal position.</span>}
    </PlayerCaptionLayer>
    {skip && <PlayerSkipControl region={region} captionSignature={caption ? "long-cue" : ""}
      mobile={mobile} reduced={false} controlsVisible={controlsVisible} onSkip={() => undefined} onFocus={() => undefined} onBlur={() => undefined} />}
    <div className={mobile ? "mobile-player-bottom" : "player-controls"} style={{ paddingBottom: inset, transform: `matrix(1, 0, 0, 1, 0, ${translation})` }}>
      <PlayerTimelineShell regions={[region]} duration={100} time={85} preview={preview ? RIGHT_EDGE_PREVIEW : null}
        obstacleKey={`${skip ? region.id : ""}:${caption ? "long-cue" : ""}:${rotated}`}>
        <input aria-label="Playback position" type="range" />
      </PlayerTimelineShell>
    </div>
  </div>;
}

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
  vi.useRealTimers();
});

const intro: MarkerRegion = { id: "intro:0:30:0", kind: "intro", label: "Intro", start: 0, end: 30 };

function CountdownFixture(props: Partial<React.ComponentProps<typeof PlayerSkipControl>>) {
  return <div data-player-root="true"><PlayerSkipControl region={intro} captionSignature="" mobile={false}
    reduced={false} controlsVisible={false} countdownEnabled onSkip={() => undefined}
    onFocus={() => undefined} onBlur={() => undefined} {...props} /></div>;
}

describe("intro action countdown", () => {
  it("waits for hidden chrome, then fills for four seconds and hides without skipping", () => {
    vi.useFakeTimers();
    const onSkip = vi.fn();
    const view = render(<CountdownFixture controlsVisible onSkip={onSkip} />);
    const control = view.container.querySelector<HTMLElement>(".player-skip-control")!;
    act(() => vi.advanceTimersByTime(10_000));
    expect(control.dataset.countdownStage).toBe("idle");
    expect(view.getByRole("button", { name: "Skip intro" })).toBeTruthy();
    view.rerender(<CountdownFixture onSkip={onSkip} />);
    expect(control.dataset.countdownStage).toBe("waiting");
    act(() => vi.advanceTimersByTime(PLAYER_SKIP_COUNTDOWN_DELAY_MS - 1));
    expect(control.dataset.countdownStage).toBe("waiting");
    expect(control.querySelector(".player-skip-countdown__fill")).toBeNull();
    act(() => vi.advanceTimersByTime(1));
    expect(control.dataset.countdownStage).toBe("filling");
    expect(control.querySelector(".player-skip-countdown")?.getAttribute("data-active")).toBe("true");
    expect(view.queryByRole("progressbar")).toBeNull();
    act(() => vi.advanceTimersByTime(PLAYER_SKIP_COUNTDOWN_DURATION_MS - 1));
    expect(view.getByRole("button", { name: "Skip intro" })).toBeTruthy();
    act(() => vi.advanceTimersByTime(1));
    expect(control.hidden).toBe(true);
    expect(view.queryByRole("button", { name: "Skip intro" })).toBeNull();
    expect(onSkip).not.toHaveBeenCalled();
  });

  it("reappears on chrome reveal and restarts a complete cycle", () => {
    vi.useFakeTimers();
    const view = render(<CountdownFixture />);
    const control = view.container.querySelector<HTMLElement>(".player-skip-control")!;
    act(() => vi.advanceTimersByTime(PLAYER_SKIP_COUNTDOWN_DELAY_MS));
    act(() => vi.advanceTimersByTime(PLAYER_SKIP_COUNTDOWN_DURATION_MS));
    expect(control.hidden).toBe(true);
    view.rerender(<CountdownFixture controlsVisible />);
    expect(control.hidden).toBe(false);
    expect(control.dataset.countdownStage).toBe("idle");
    act(() => vi.advanceTimersByTime(10_000));
    expect(control.hidden).toBe(false);
    view.rerender(<CountdownFixture />);
    act(() => vi.advanceTimersByTime(PLAYER_SKIP_COUNTDOWN_DELAY_MS - 1));
    expect(control.dataset.countdownStage).toBe("waiting");
    act(() => vi.advanceTimersByTime(1));
    expect(control.dataset.countdownStage).toBe("filling");
  });

  it("cancels a filling cycle while paused or buffering and starts fresh when eligible", () => {
    vi.useFakeTimers();
    const view = render(<CountdownFixture />);
    const control = view.container.querySelector<HTMLElement>(".player-skip-control")!;
    act(() => vi.advanceTimersByTime(PLAYER_SKIP_COUNTDOWN_DELAY_MS));
    act(() => vi.advanceTimersByTime(2_000));
    view.rerender(<CountdownFixture countdownEnabled={false} />);
    act(() => vi.advanceTimersByTime(20_000));
    expect(control.dataset.countdownStage).toBe("idle");
    expect(control.hidden).toBe(false);
    view.rerender(<CountdownFixture />);
    act(() => vi.advanceTimersByTime(PLAYER_SKIP_COUNTDOWN_DELAY_MS));
    act(() => vi.advanceTimersByTime(PLAYER_SKIP_COUNTDOWN_DURATION_MS - 1));
    expect(control.hidden).toBe(false);
  });

  it("keeps hover and keyboard focus safe from dismissal", () => {
    vi.useFakeTimers();
    const onFocus = vi.fn();
    const view = render(<CountdownFixture onFocus={onFocus} />);
    const control = view.container.querySelector<HTMLElement>(".player-skip-control")!;
    act(() => vi.advanceTimersByTime(PLAYER_SKIP_COUNTDOWN_DELAY_MS));
    fireEvent.pointerEnter(control, { pointerType: "mouse" });
    act(() => vi.advanceTimersByTime(10_000));
    expect(control.hidden).toBe(false);
    expect(control.dataset.countdownStage).toBe("idle");
    fireEvent.pointerLeave(control, { pointerType: "mouse" });
    const button = view.getByRole("button", { name: "Skip intro" });
    act(() => button.focus());
    act(() => vi.advanceTimersByTime(10_000));
    expect(control.hidden).toBe(false);
    expect(document.activeElement).toBe(button);
    expect(onFocus).toHaveBeenCalled();
  });

  it("cancels when the tab is hidden and resets when it returns", () => {
    vi.useFakeTimers();
    const descriptor = Object.getOwnPropertyDescriptor(document, "visibilityState");
    const view = render(<CountdownFixture />);
    try {
      const control = view.container.querySelector<HTMLElement>(".player-skip-control")!;
      act(() => vi.advanceTimersByTime(PLAYER_SKIP_COUNTDOWN_DELAY_MS));
      Object.defineProperty(document, "visibilityState", { configurable: true, value: "hidden" });
      fireEvent(document, new Event("visibilitychange"));
      act(() => vi.advanceTimersByTime(10_000));
      expect(control.hidden).toBe(false);
      expect(control.dataset.countdownStage).toBe("idle");
      Object.defineProperty(document, "visibilityState", { configurable: true, value: "visible" });
      fireEvent(document, new Event("visibilitychange"));
      expect(control.dataset.countdownStage).toBe("waiting");
    } finally {
      view.unmount();
      if (descriptor) Object.defineProperty(document, "visibilityState", descriptor);
      else Reflect.deleteProperty(document, "visibilityState");
    }
  });

  it("resets for a new intro and keeps credits outside the countdown", () => {
    vi.useFakeTimers();
    const view = render(<CountdownFixture />);
    const control = view.container.querySelector<HTMLElement>(".player-skip-control")!;
    act(() => vi.advanceTimersByTime(PLAYER_SKIP_COUNTDOWN_DELAY_MS));
    view.rerender(<CountdownFixture region={{ ...intro, id: "intro:40:50:0", start: 40, end: 50 }} />);
    expect(control.dataset.countdownStage).toBe("waiting");
    view.rerender(<CountdownFixture region={region} />);
    act(() => vi.advanceTimersByTime(10_000));
    expect(view.getByRole("button", { name: "Skip credits" })).toBeTruthy();
    expect(control.querySelector(".player-skip-countdown")).toBeNull();
  });

  it("keeps reduced-motion dismissal functional and clears timers on unmount", () => {
    vi.useFakeTimers();
    const view = render(<CountdownFixture reduced />);
    const control = view.container.querySelector<HTMLElement>(".player-skip-control")!;
    act(() => vi.advanceTimersByTime(PLAYER_SKIP_COUNTDOWN_DELAY_MS));
    expect(control.dataset.reducedMotion).toBe("true");
    expect(control.querySelector(".player-skip-countdown")?.getAttribute("aria-hidden")).toBe("true");
    expect(vi.getTimerCount()).toBe(1);
    view.unmount();
    expect(vi.getTimerCount()).toBe(0);
  });
});

describe("contextual player marker layout", () => {
  it.each([false, true])("lowers mobile overlays together without losing their caption gap (rotated: %s)", (rotated) => {
    installLayout({ rotated, translation: 6, captionTranslation: 70 });
    const view = render(<Fixture caption mobile rotated={rotated} translation={6} />);
    const caption = view.container.querySelector<HTMLElement>(".player-caption-layer")!;
    const skip = view.container.querySelector<HTMLElement>(".player-skip-control")!;
    expect(caption.style.getPropertyValue("--player-caption-hidden-offset")).toBe("116px");
    expect(skip.style.getPropertyValue("--player-skip-hidden-offset")).toBe("116px");
    const restingBottom = skip.style.bottom;
    const restingTop = ROOT_HEIGHT - Number.parseFloat(restingBottom) - BUTTON_HEIGHT;
    expect(410 - restingTop - BUTTON_HEIGHT).toBe(12);
    view.rerender(<Fixture caption mobile rotated={rotated} translation={6} controlsVisible={false} />);
    fireEvent.resize(window);
    view.rerender(<Fixture caption mobile rotated={rotated} translation={6} />);
    expect(skip.style.bottom).toBe(restingBottom);
    expect(skip.style.getPropertyValue("--player-skip-hidden-offset")).toBe("116px");
    expect(caption.style.getPropertyValue("--player-caption-hidden-offset")).toBe("116px");
  });

  it("lowers a caption-free mobile Skip only as far as the safe bottom inset", () => {
    installLayout({ translation: 0 });
    const view = render(<Fixture mobile translation={0} inset={34} />);
    const skip = view.container.querySelector<HTMLElement>(".player-skip-control")!;
    const shownBottom = Number.parseFloat(skip.style.bottom);
    const offset = Number.parseFloat(skip.style.getPropertyValue("--player-skip-hidden-offset"));
    expect(shownBottom - offset).toBe(34);
  });

  it("anchors Skip 12px above the resting timeline during an 18px chrome entrance", () => {
    installLayout();
    const view = render(<Fixture />);
    const button = view.container.querySelector<HTMLElement>(".player-skip-control")!;
    const buttonBottom = ROOT_HEIGHT - Number.parseFloat(button.style.bottom);
    expect(TRACK_TOP - buttonBottom).toBe(12);
    expect(Number.parseFloat(button.style.left) + BUTTON_WIDTH).toBe(TRACK_LEFT + TRACK_WIDTH);

    fireEvent.resize(window);
    expect(ROOT_HEIGHT - Number.parseFloat(button.style.bottom)).toBe(buttonBottom);
  });

  it("moves Skip above a newly rendered long caption and restores its anchor when the cue clears", () => {
    installLayout();
    const view = render(<Fixture />);
    const button = view.container.querySelector<HTMLElement>(".player-skip-control")!;
    const restingBottom = button.style.bottom;

    view.rerender(<Fixture caption />);
    const cue = view.container.querySelector<HTMLElement>(".player-caption-layer > span")!;
    expect(cue.getBoundingClientRect().top - button.getBoundingClientRect().bottom).toBe(12);
    expect(button.style.bottom).not.toBe(restingBottom);

    view.rerender(<Fixture />);
    expect(button.style.bottom).toBe(restingBottom);
  });

  it("clamps a right-edge preview inside the timeline and raises it above Skip", () => {
    installLayout();
    const view = render(<Fixture preview />);
    const timeline = view.container.querySelector<HTMLElement>(".player-timeline-shell")!.getBoundingClientRect();
    const preview = view.container.querySelector<HTMLElement>(".player-timeline-preview")!;
    const skip = view.container.querySelector<HTMLElement>(".player-skip-control")!.getBoundingClientRect();
    const tooltip = preview.getBoundingClientRect();

    expect(preview.textContent).toContain("Credits");
    expect(tooltip.right).toBe(timeline.right);
    expect(tooltip.left).toBeGreaterThanOrEqual(timeline.left);
    expect(skip.top - tooltip.bottom).toBe(8);
  });

  it("repositions an unchanged preview when Skip returns after a pending seek", () => {
    installLayout();
    const view = render(<Fixture preview skip={false} />);
    const preview = view.container.querySelector<HTMLElement>(".player-timeline-preview")!;
    const timeline = view.container.querySelector<HTMLElement>(".player-timeline-shell")!.getBoundingClientRect();
    expect(view.container.querySelector(".player-skip-control")).toBeNull();
    expect(timeline.top - preview.getBoundingClientRect().bottom).toBe(8);
    const unobstructedBottom = preview.getBoundingClientRect().bottom;

    // The same preview object and DOM node survive confirmation; the changed
    // obstacle key must account for the contextual action reappearing alone.
    view.rerender(<Fixture preview />);
    expect(view.container.querySelector(".player-timeline-preview")).toBe(preview);
    const skip = view.container.querySelector<HTMLElement>(".player-skip-control")!.getBoundingClientRect();
    expect(preview.getBoundingClientRect().bottom).toBeLessThan(unobstructedBottom);
    expect(skip.top - preview.getBoundingClientRect().bottom).toBe(8);

    view.rerender(<Fixture preview skip={false} />);
    expect(timeline.top - preview.getBoundingClientRect().bottom).toBe(8);
  });

  it("uses logical landscape geometry for Skip and preview after a 90-degree rotation", () => {
    installLayout({ rotated: true, translation: 0 });
    const view = render(<Fixture rotated translation={0} preview />);
    const skip = view.container.querySelector<HTMLElement>(".player-skip-control")!;
    const timeline = view.container.querySelector<HTMLElement>(".player-timeline-shell")!.getBoundingClientRect();
    const preview = view.container.querySelector<HTMLElement>(".player-timeline-preview")!.getBoundingClientRect();
    expect(TRACK_TOP - (ROOT_HEIGHT - Number.parseFloat(skip.style.bottom))).toBe(12);
    expect(Number.parseFloat(skip.style.left) + BUTTON_WIDTH).toBe(TRACK_LEFT + TRACK_WIDTH);
    expect(preview.bottom).toBe(timeline.bottom);
    expect(preview.left - skip.getBoundingClientRect().right).toBe(8);
  });
});
