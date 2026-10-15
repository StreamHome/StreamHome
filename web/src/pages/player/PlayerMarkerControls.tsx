import React, { memo, useEffect, useLayoutEffect, useRef, useState } from "react";
import { formatDuration } from "../../utils/format";
import { restorePlayerPointerClickFocus, restorePlayerPointerFocus } from "./playerKeyboard";
import { activeMarkerRegion, type MarkerRegion } from "./playerMarkers";

export const PLAYER_SKIP_COUNTDOWN_DELAY_MS = 1500;
export const PLAYER_SKIP_COUNTDOWN_DURATION_MS = 4000;

type SkipCountdownStage = "idle" | "waiting" | "filling" | "hidden";

const TimelineRegions = memo(function TimelineRegions({ regions, duration, activeId }: {
  regions: readonly MarkerRegion[];
  duration: number;
  activeId?: string;
}) {
  if (!regions.length || duration <= 0) return null;
  return <div className="player-timeline-markers" aria-hidden="true">
    {regions.map((region) => <span
      key={region.id}
      data-marker-kind={region.kind}
      data-marker-id={region.id}
      data-active={region.id === activeId ? "true" : undefined}
      style={{ left: `${region.start / duration * 100}%`, width: `${(region.end - region.start) / duration * 100}%` }}
    />)}
  </div>;
});

export function PlayerTimelineShell({ regions, duration, time, preview, obstacleKey, children }: {
  regions: readonly MarkerRegion[];
  duration: number;
  time: number;
  preview: { x: number; time: number } | null;
  obstacleKey?: string;
  children: React.ReactNode;
}) {
  const active = activeMarkerRegion(regions, time);
  const previewRegion = preview ? activeMarkerRegion(regions, preview.time) : null;
  const previewRef = useRef<HTMLSpanElement>(null);
  useLayoutEffect(() => {
    const tooltip = previewRef.current;
    const shell = tooltip?.parentElement;
    if (!tooltip || !shell || !preview) return;
    const root = shell.closest<HTMLElement>("[data-player-root='true']");
    const rotated = root?.dataset.mobileOrientation === "forced-landscape";
    const size = tooltip.getBoundingClientRect();
    const tooltipHeight = rotated ? size.width : size.height;
    const half = (rotated ? size.height : size.width) / 2;
    const center = Math.max(half, Math.min(preview.x, shell.clientWidth - half));
    tooltip.style.left = `${center}px`;
    const frame = shell.getBoundingClientRect();
    const obstacles = Array.from(root?.querySelectorAll(".player-skip-control:not([hidden]), .player-caption-layer > span") ?? []).map((element) => {
      const rect = element.getBoundingClientRect();
      const left = rotated ? rect.top - frame.top : rect.left - frame.left;
      const top = rotated ? frame.right - rect.right : rect.top - frame.top;
      return { left, top, right: left + (rotated ? rect.height : rect.width), bottom: top + (rotated ? rect.width : rect.height) };
    }).sort((a, b) => b.top - a.top);
    let top = -8 - tooltipHeight;
    for (const obstacle of obstacles) {
      if (obstacle.right > center - half - 8 && obstacle.left < center + half + 8
        && obstacle.bottom > top - 8 && obstacle.top < top + tooltipHeight + 8) {
        top = obstacle.top - tooltipHeight - 8;
      }
    }
    tooltip.style.bottom = `${shell.clientHeight - top - tooltipHeight}px`;
  }, [obstacleKey, preview, previewRegion?.label]);
  return <div className="player-timeline-shell">
    {preview && <span ref={previewRef} className="player-timeline-preview" style={{ left: preview.x }}>
      {formatDuration(preview.time)}{previewRegion && <> <i>·</i> {previewRegion.label}</>}
    </span>}
    {children}
    <TimelineRegions regions={regions} duration={duration} activeId={active?.id} />
  </div>;
}

export function PlayerSkipControl({ region, captionSignature, mobile, reduced, controlsVisible = true,
  countdownEnabled = false, onVisibilityChange, onSkip, onFocus, onBlur }: {
  region: MarkerRegion;
  captionSignature: string;
  mobile: boolean;
  reduced: boolean;
  controlsVisible?: boolean;
  countdownEnabled?: boolean;
  onVisibilityChange?: (visible: boolean) => void;
  onSkip: () => void;
  onFocus: () => void;
  onBlur: () => void;
}) {
  const controlRef = useRef<HTMLDivElement>(null);
  const [countdownStage, setCountdownStage] = useState<SkipCountdownStage>("idle");
  const [hovered, setHovered] = useState(false);
  const [focused, setFocused] = useState(false);
  const [documentVisible, setDocumentVisible] = useState(() => document.visibilityState !== "hidden");
  const intro = region.kind.toLowerCase() === "intro";
  const countdownActive = intro && countdownEnabled && !controlsVisible && !hovered && !focused && documentVisible;
  const hidden = countdownActive && countdownStage === "hidden";
  const stage = countdownActive ? countdownStage : "idle";

  useEffect(() => {
    const updateVisibility = () => setDocumentVisible(document.visibilityState !== "hidden");
    document.addEventListener("visibilitychange", updateVisibility);
    return () => document.removeEventListener("visibilitychange", updateVisibility);
  }, []);

  useLayoutEffect(() => {
    setCountdownStage(countdownActive ? "waiting" : "idle");
    if (!countdownActive) return;
    let hideTimer: number | undefined;
    const delayTimer = window.setTimeout(() => {
      setCountdownStage("filling");
      hideTimer = window.setTimeout(() => setCountdownStage("hidden"), PLAYER_SKIP_COUNTDOWN_DURATION_MS);
    }, PLAYER_SKIP_COUNTDOWN_DELAY_MS);
    return () => {
      window.clearTimeout(delayTimer);
      if (hideTimer !== undefined) window.clearTimeout(hideTimer);
    };
  }, [countdownActive, region.id]);

  useLayoutEffect(() => { onVisibilityChange?.(!hidden); }, [hidden, onVisibilityChange]);

  useLayoutEffect(() => {
    const control = controlRef.current;
    const root = control?.closest<HTMLElement>("[data-player-root='true']");
    if (!root || !control || hidden) return;
    const timeline = root.querySelector<HTMLElement>(".player-timeline-shell");
    const captions = root.querySelector<HTMLElement>(".player-caption-layer");
    const position = () => {
      const frame = root.getBoundingClientRect();
      const rotated = root.dataset.mobileOrientation === "forced-landscape";
      const bounds = (element: Element) => {
        const rect = element.getBoundingClientRect();
        const left = rotated ? rect.top - frame.top : rect.left - frame.left;
        const top = rotated ? frame.right - rect.right : rect.top - frame.top;
        const width = rotated ? rect.height : rect.width;
        const height = rotated ? rect.width : rect.height;
        return { left, top, right: left + width, bottom: top + height, width, height };
      };
      const width = root.clientWidth;
      const height = root.clientHeight;
      if (!width || !height) return;
      const button = bounds(control);
      const track = timeline ? bounds(timeline) : null;
      const chrome = timeline?.closest(".player-controls");
      // The contextual action stays anchored to resting chrome geometry while
      // Framer translates that chrome during its entrance and idle animations.
      if (track && chrome && typeof DOMMatrixReadOnly !== "undefined") {
        const transform = getComputedStyle(chrome).transform;
        if (transform !== "none") track.top -= new DOMMatrixReadOnly(transform).m42;
      }
      const left = Math.max(16, Math.min(width - button.width - 16, (track?.right ?? width - 24) - button.width));
      let top = (track?.top ?? height - 84) - button.height - 12;
      const lines = Array.from(captions?.children ?? []).map(bounds).sort((a, b) => b.top - a.top);
      for (const line of lines) {
        if (line.right > left - 12 && line.left < left + button.width + 12
          && line.bottom > top - 12 && line.top < top + button.height + 12) {
          top = line.top - button.height - 12;
        }
      }
      control.style.left = `${left}px`;
      control.style.bottom = `${Math.max(16, height - Math.max(16, top) - button.height)}px`;
      control.style.right = "auto";
    };
    position();
    const observer = typeof ResizeObserver === "undefined" ? null : new ResizeObserver(position);
    [root, control, timeline, captions].forEach((element) => { if (element) observer?.observe(element); });
    window.addEventListener("resize", position);
    return () => { observer?.disconnect(); window.removeEventListener("resize", position); };
  }, [captionSignature, hidden, mobile, region.id]);

  useLayoutEffect(() => {
    const control = controlRef.current;
    return () => {
      // A disappearing contextual action must not leave keyboard focus on body.
      if (control?.contains(control.ownerDocument.activeElement)) restorePlayerPointerFocus(control);
    };
  }, []);

  return <div ref={controlRef} className="player-skip-control" hidden={hidden}
    data-countdown-stage={stage} data-reduced-motion={reduced ? "true" : undefined}
    style={{ "--player-skip-countdown-duration": `${PLAYER_SKIP_COUNTDOWN_DURATION_MS}ms` } as React.CSSProperties}
    onPointerEnter={(event) => {
      if (event.pointerType === "touch") return;
      setHovered(true);
      onFocus();
    }}
    onPointerLeave={(event) => {
      if (event.pointerType === "touch") return;
      setHovered(false);
      onBlur();
    }}
    onFocusCapture={() => { setFocused(true); onFocus(); }}
    onBlurCapture={(event) => {
      if (event.currentTarget.contains(event.relatedTarget)) return;
      setFocused(false);
      onBlur();
    }}>
    <button type="button" className="player-control-button player-skip-button" data-reduced-motion={reduced ? "true" : undefined}
      onClick={(event) => { onSkip(); restorePlayerPointerClickFocus(event); }}>
      <span>Skip {region.label.toLowerCase()}</span>
      <svg className="player-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
        <path d="m5 5 10 7-10 7V5Z" /><path d="M19 5v14" />
      </svg>
    </button>
    {intro && <span className="player-skip-countdown" aria-hidden="true" data-active={stage === "filling" ? "true" : undefined}>
      {stage === "filling" && <span className="player-skip-countdown__fill" />}
    </span>}
  </div>;
}
