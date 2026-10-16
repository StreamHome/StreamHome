import React, { createContext, useCallback, useContext, useEffect, useId, useLayoutEffect, useMemo, useRef, useState } from "react";
import { createPortal } from "react-dom";
import { restorePlayerPointerClickFocus, restorePlayerPointerFocus } from "./playerKeyboard";
import { mobilePlayerIcon } from "./PlayerMobileIcon";

export type PlayerIconName =
  | "audio"
  | "back"
  | "captions"
  | "check"
  | "chevron"
  | "close"
  | "exit"
  | "forward"
  | "fullscreen"
  | "fullscreen-exit"
  | "mute"
  | "pause"
  | "pip"
  | "play"
  | "quality"
  | "rewind"
  | "seek-backward"
  | "seek-forward"
  | "speed"
  | "settings"
  | "volume";

interface PlayerIconProps {
  name: PlayerIconName;
  className?: string;
  prominent?: boolean;
  variant?: "default" | "mobile";
}

export function hasSubtitleOptions(options: readonly unknown[]): boolean {
  return options.length > 0;
}

export function PlayerIcon({ name, className = "", prominent = false, variant = "default" }: PlayerIconProps) {
  if (variant === "mobile") {
    const icon = mobilePlayerIcon(name, `player-icon ${className}`.trim());
    if (icon) return icon;
  }
  const common = {
    className: `player-icon ${className}`.trim(),
    viewBox: "0 0 24 24",
    fill: "none",
    stroke: "currentColor",
    strokeWidth: 1.8,
    strokeLinecap: "round" as const,
    strokeLinejoin: "round" as const,
    "aria-hidden": true,
  };

  if (name === "play") return <svg {...common}><path d={prominent ? "M6 3.75 20.5 12 6 20.25V3.75Z" : "m8 5 11 7-11 7V5Z"} fill="currentColor" stroke="none" /></svg>;
  if (name === "pause") return prominent
    ? <svg {...common} fill="currentColor" stroke="none"><rect x="5.5" y="4" width="4.5" height="16" rx=".7" /><rect x="14" y="4" width="4.5" height="16" rx=".7" /></svg>
    : <svg {...common}><path d="M8 5v14M16 5v14" strokeWidth="2.4" /></svg>;
  if (name === "seek-backward") return <svg {...common}><path d="m11 6-6 6 6 6m8-12-6 6 6 6" /></svg>;
  if (name === "seek-forward") return <svg {...common}><path d="m5 6 6 6-6 6m8-12 6 6-6 6" /></svg>;
  if (name === "rewind") return <svg {...common}><path d="M8.5 8H4V3.5" /><path d="M4.3 8a8 8 0 1 1-.1 7.8" /><text x="8" y="15.2" fill="currentColor" stroke="none" fontSize="7" fontWeight="700">10</text></svg>;
  if (name === "forward") return <svg {...common}><path d="M15.5 8H20V3.5" /><path d="M19.7 8a8 8 0 1 0 .1 7.8" /><text x="8" y="15.2" fill="currentColor" stroke="none" fontSize="7" fontWeight="700">10</text></svg>;
  if (name === "volume") return <svg {...common}><path d="M4 10v4h4l5 4V6l-5 4H4Z" /><path d="M16 9a4 4 0 0 1 0 6M18.5 6.5a8 8 0 0 1 0 11" /></svg>;
  if (name === "mute") return <svg {...common}><path d="M4 10v4h4l5 4V6l-5 4H4Z" /><path d="m17 10 4 4m0-4-4 4" /></svg>;
  if (name === "settings") return <svg {...common}><path d="M4 7h16M4 17h16" /><circle cx="9" cy="7" r="2" fill="currentColor" /><circle cx="15" cy="17" r="2" fill="currentColor" /></svg>;
  if (name === "speed") return <svg {...common}><path d="M5 17a8 8 0 1 1 14 0" /><path d="m12 13 4-4" /><path d="M7.5 17h9" /></svg>;
  if (name === "audio") return <svg {...common}><path d="M9 18V6l10-2v12" /><circle cx="6" cy="18" r="3" /><circle cx="16" cy="16" r="3" /></svg>;
  if (name === "captions") return <svg {...common}><rect x="3" y="5" width="18" height="14" rx="2" /><path d="M10 10a2.5 2.5 0 1 0 0 4M18 10a2.5 2.5 0 1 0 0 4" /></svg>;
  if (name === "quality") return <svg {...common}><rect x="3" y="5" width="18" height="14" rx="2" /><path d="m8 15 2.4-3 2.1 2 3.3-4 2.2 5" /></svg>;
  if (name === "pip") return <svg {...common}><rect x="3" y="5" width="18" height="14" rx="2" /><rect x="12" y="11" width="7" height="5" rx="1" fill="currentColor" stroke="none" /></svg>;
  if (name === "fullscreen") return <svg {...common}><path d="M8 4H4v4M16 4h4v4M8 20H4v-4M16 20h4v-4" /></svg>;
  if (name === "fullscreen-exit") return <svg {...common}><path d="M4 8h4V4M20 8h-4V4M4 16h4v4M20 16h-4v4" /></svg>;
  if (name === "exit") return <svg {...common}><path d="M14 5h5v14h-5M10 8l-4 4 4 4M6 12h9" /></svg>;
  if (name === "back") return <svg {...common}><path d="m10 5-7 7 7 7M3 12h18" /></svg>;
  if (name === "close") return <svg {...common}><path d="m6 6 12 12M18 6 6 18" /></svg>;
  if (name === "chevron") return <svg {...common}><path d="m7 9 5 5 5-5" /></svg>;
  return <svg {...common}><path d="m7 12 3 3 7-7" /></svg>;
}

interface PlayerIconButtonProps extends React.ButtonHTMLAttributes<HTMLButtonElement> {
  icon: PlayerIconName;
  label: string;
  prominentIcon?: boolean;
  iconVariant?: PlayerIconProps["variant"];
}

export function PlayerIconButton({ icon, label, className = "", onClick, prominentIcon = false, iconVariant = "default", ...props }: PlayerIconButtonProps) {
  return (
    <button
      type="button"
      aria-label={label}
      data-tooltip={label}
      className={`player-control-button ${className}`.trim()}
      {...props}
      onClick={(event) => {
        onClick?.(event);
        if (!event.defaultPrevented && !event.currentTarget.hasAttribute("aria-haspopup")) restorePlayerPointerClickFocus(event);
      }}
    >
      <PlayerIcon name={icon} prominent={prominentIcon} variant={iconVariant} />
    </button>
  );
}

interface MenuOwner {
  openId: string | null;
  request: (id: string, open: boolean) => void;
}
const MenuContext = createContext<MenuOwner | null>(null);

export function PlayerMenuProvider({ children, onOpenChange }: { children: React.ReactNode; onOpenChange: (open: boolean) => void }) {
  const [openId, setOpenId] = useState<string | null>(null);
  const request = useCallback((id: string, open: boolean) => {
    setOpenId((current) => open ? id : current === id ? null : current);
  }, []);
  useEffect(() => onOpenChange(openId !== null), [openId, onOpenChange]);
  useEffect(() => {
    if (!openId) return;
    const escape = (event: KeyboardEvent) => {
      if (event.key !== "Escape" || event.defaultPrevented) return;
      event.preventDefault();
      setOpenId(null);
      const trigger = Array.from(document.querySelectorAll<HTMLButtonElement>("button[aria-controls]"))
        .find((button) => button.getAttribute("aria-controls") === openId);
      trigger?.focus();
    };
    document.addEventListener("keydown", escape);
    return () => document.removeEventListener("keydown", escape);
  }, [openId]);
  const value = useMemo(() => ({ openId, request }), [openId, request]);
  return <MenuContext.Provider value={value}>{children}</MenuContext.Provider>;
}

function usePlayerPopup(onOpenChange?: (open: boolean) => void, preferredWidth?: number, alignment: "end" | "center" = "end", modal = false) {
  const id = useId();
  const owner = useContext(MenuContext);
  const [localOpen, setLocalOpen] = useState(false);
  const open = owner ? owner.openId === id : localOpen;
  const rootRef = useRef<HTMLDivElement>(null);
  const triggerRef = useRef<HTMLButtonElement>(null);
  const popupRef = useRef<HTMLDivElement>(null);
  const [host, setHost] = useState<HTMLElement | null>(null);
  const [style, setStyle] = useState<React.CSSProperties>({});
  const releaseRef = useRef<(() => void) | null>(null);
  releaseRef.current = open ? () => owner ? owner.request(id, false) : onOpenChange?.(false) : null;
  useEffect(() => () => releaseRef.current?.(), []);
  const updateOpen = useCallback((next: boolean) => {
    if (owner) owner.request(id, next);
    else { setLocalOpen(next); onOpenChange?.(next); }
  }, [id, onOpenChange, owner]);

  useLayoutEffect(() => {
    if (!open) return;
    const anchor = rootRef.current;
    const player = anchor?.closest<HTMLElement>("[data-player-root='true']") ?? null;
    setHost(player ?? document.body);
    if (modal) return;
    const position = () => {
      const popup = popupRef.current;
      if (!anchor || !popup) return;
      const bounds = anchor.getBoundingClientRect();
      const frame = player?.getBoundingClientRect();
      const rotated = player?.dataset.mobileOrientation === "forced-landscape";
      const width = player?.clientWidth || (rotated ? window.innerHeight : window.innerWidth);
      const height = player?.clientHeight || (rotated ? window.innerWidth : window.innerHeight);
      const x = rotated ? bounds.top - (frame?.top ?? 0) : bounds.left - (frame?.left ?? 0);
      const y = rotated ? (frame?.right ?? window.innerWidth) - bounds.right : bounds.top - (frame?.top ?? 0);
      const anchorWidth = rotated ? bounds.height : bounds.width;
      const anchorHeight = rotated ? bounds.width : bounds.height;
      const menuWidth = Math.max(1, Math.min(width - 24, preferredWidth ?? (player?.dataset.mobilePlayer === "true" ? 360 : 288)));
      const roomAbove = Math.max(0, y - 20);
      const roomBelow = Math.max(0, height - y - anchorHeight - 20);
      const above = roomAbove >= Math.min(popup.scrollHeight, 160) || roomAbove >= roomBelow;
      const maxHeight = Math.max(1, Math.min(height - 24, above ? roomAbove : roomBelow));
      const menuHeight = Math.min(popup.scrollHeight, maxHeight);
      setStyle({ position: player ? "absolute" : "fixed", width: menuWidth,
        left: Math.max(12, Math.min(alignment === "center" ? x + (anchorWidth - menuWidth) / 2 : x + anchorWidth - menuWidth, width - menuWidth - 12)),
        top: above ? Math.max(12, y - menuHeight - 8) : Math.min(height - menuHeight - 12, y + anchorHeight + 8),
        right: "auto", bottom: "auto", maxHeight, overflowY: "auto" });
    };
    position();
    window.addEventListener("resize", position);
    const observer = typeof ResizeObserver === "undefined" ? null : new ResizeObserver(position);
    if (player) observer?.observe(player);
    observer?.observe(anchor!);
    if (popupRef.current) observer?.observe(popupRef.current);
    return () => { window.removeEventListener("resize", position); observer?.disconnect(); };
  }, [alignment, host, modal, open, preferredWidth]);

  useEffect(() => {
    if (!open || modal) return;
    const outside = (event: PointerEvent) => {
      if (!rootRef.current?.contains(event.target as Node) && !popupRef.current?.contains(event.target as Node)) updateOpen(false);
    };
    document.addEventListener("pointerdown", outside);
    return () => document.removeEventListener("pointerdown", outside);
  }, [modal, open, updateOpen]);

  const dismiss = () => { updateOpen(false); triggerRef.current?.focus(); };
  const onBlur = (event: React.FocusEvent) => {
    const target = event.relatedTarget as Node | null;
    if (!rootRef.current?.contains(target) && !popupRef.current?.contains(target)) updateOpen(false);
  };
  const render = (content: React.ReactElement) => host ? createPortal(content, host) : content;
  return { id, open, updateOpen, rootRef, triggerRef, popupRef, host, style, render, dismiss, onBlur };
}

export function PlayerVolume({ volume, muted, onVolumeChange, onToggleMute, onOpenChange }: {
  volume: number;
  muted: boolean;
  onVolumeChange: (value: number) => void;
  onToggleMute: () => void;
  onOpenChange?: (open: boolean) => void;
}) {
  const popup = usePlayerPopup(onOpenChange, 64, "center");
  const sliderRef = useRef<HTMLInputElement>(null);
  const closeTimerRef = useRef<number | null>(null);
  const hoveringRef = useRef(false);
  const draggingRef = useRef(false);
  const keyboardFocusRef = useRef(false);
  const pointerFocusRef = useRef(false);
  const dismissingRef = useRef(false);
  const restoringPointerFocusRef = useRef(false);
  const level = muted ? 0 : volume;
  const percentage = `${Math.round(level * 100)}%`;
  const clearClose = useCallback(() => {
    if (closeTimerRef.current !== null) window.clearTimeout(closeTimerRef.current);
    closeTimerRef.current = null;
  }, []);
  const restorePointerFocus = useCallback((target: HTMLElement | null) => {
    restoringPointerFocusRef.current = true;
    try {
      restorePlayerPointerFocus(target);
    } finally {
      restoringPointerFocusRef.current = false;
    }
  }, []);
  useEffect(() => clearClose, [clearClose]);
  useEffect(() => {
    if (popup.open) return;
    draggingRef.current = false;
    keyboardFocusRef.current = false;
    pointerFocusRef.current = false;
    clearClose();
  }, [clearClose, popup.open]);
  const contains = (target: EventTarget | null) => target instanceof Node
    && (popup.rootRef.current?.contains(target) || popup.popupRef.current?.contains(target));
  const scheduleClose = () => {
    clearClose();
    closeTimerRef.current = window.setTimeout(() => {
      closeTimerRef.current = null;
      if (!hoveringRef.current && !draggingRef.current && !keyboardFocusRef.current) popup.updateOpen(false);
    }, 140);
  };
  const enter = (event: React.PointerEvent) => {
    if (event.pointerType === "touch") return;
    hoveringRef.current = true;
    clearClose();
    popup.updateOpen(true);
  };
  const leave = (event: React.PointerEvent) => {
    if (contains(event.relatedTarget)) return;
    hoveringRef.current = false;
    scheduleClose();
  };
  useEffect(() => {
    if (!popup.open) return;
    const release = (event: PointerEvent) => {
      pointerFocusRef.current = false;
      if (!draggingRef.current) return;
      draggingRef.current = false;
      hoveringRef.current = [popup.rootRef.current, popup.popupRef.current].some((element) => {
        const bounds = element?.getBoundingClientRect();
        return bounds && event.clientX >= bounds.left && event.clientX < bounds.right
          && event.clientY >= bounds.top && event.clientY < bounds.bottom;
      });
      restorePointerFocus(sliderRef.current);
      if (!hoveringRef.current && !keyboardFocusRef.current) popup.updateOpen(false);
    };
    document.addEventListener("pointerup", release);
    document.addEventListener("pointercancel", release);
    return () => {
      document.removeEventListener("pointerup", release);
      document.removeEventListener("pointercancel", release);
    };
  }, [popup.open, popup.popupRef, popup.rootRef, popup.updateOpen, restorePointerFocus]);
  const dismiss = useCallback(() => {
    clearClose();
    keyboardFocusRef.current = false;
    dismissingRef.current = true;
    popup.updateOpen(false);
    popup.triggerRef.current?.focus();
    dismissingRef.current = false;
  }, [clearClose, popup.triggerRef, popup.updateOpen]);
  useEffect(() => {
    if (!popup.open) return;
    const escape = (event: KeyboardEvent) => {
      if (event.key !== "Escape" || event.defaultPrevented) return;
      event.preventDefault();
      event.stopPropagation();
      dismiss();
    };
    document.addEventListener("keydown", escape, true);
    return () => document.removeEventListener("keydown", escape, true);
  }, [dismiss, popup.open]);
  const openAndFocus = () => {
    clearClose();
    pointerFocusRef.current = false;
    keyboardFocusRef.current = true;
    popup.updateOpen(true);
    window.requestAnimationFrame(() => sliderRef.current?.focus());
  };
  const handleBlur = (event: React.FocusEvent) => {
    if (!contains(event.relatedTarget)) keyboardFocusRef.current = false;
    if (restoringPointerFocusRef.current && hoveringRef.current
      && event.relatedTarget === popup.rootRef.current?.closest("[data-player-root='true']")) return;
    popup.onBlur(event);
  };

  return <div className="player-control-menu" ref={popup.rootRef} data-open={popup.open}
    onPointerEnter={enter} onPointerLeave={leave}
    onPointerDownCapture={(event) => {
      pointerFocusRef.current = true;
      keyboardFocusRef.current = false;
      if (event.pointerType === "touch") popup.updateOpen(true);
    }}
    onFocusCapture={() => {
      if (pointerFocusRef.current || dismissingRef.current) return;
      clearClose();
      keyboardFocusRef.current = true;
      popup.updateOpen(true);
    }}
    onBlur={handleBlur}>
    <button
      ref={popup.triggerRef}
      type="button"
      className="player-control-button player-control-menu__trigger player-volume-trigger"
      aria-label="Volume"
      aria-description={muted ? "Muted" : percentage}
      aria-haspopup="dialog"
      aria-expanded={popup.open}
      aria-controls={popup.id}
      onClick={(event) => {
        onToggleMute();
        if (event.detail > 0) restorePointerFocus(event.currentTarget);
      }}
      onKeyDown={(event) => {
        if (event.key === "ArrowDown" || event.key === "ArrowUp") {
          event.preventDefault();
          openAndFocus();
        } else if (event.key === "Escape" && popup.open) {
          event.preventDefault();
          dismiss();
        }
      }}
    >
      <PlayerIcon name={level === 0 ? "mute" : "volume"} />
    </button>
    {popup.open && popup.render(<div
      ref={popup.popupRef}
      id={popup.id}
      role="dialog"
      aria-label="Volume"
      className="player-control-menu__list player-volume-panel"
      style={popup.style}
      onPointerEnter={enter}
      onPointerLeave={leave}
      onBlur={handleBlur}
      onKeyDown={(event) => {
        if (event.key === "Escape") {
          event.preventDefault();
          event.stopPropagation();
          dismiss();
        }
      }}
    >
      <input ref={sliderRef} aria-label="Volume" aria-orientation="vertical" aria-valuetext={percentage} type="range" min={0} max={1} step={0.01} value={level}
        style={{ "--player-volume-fill": `${level * 100}%` } as React.CSSProperties}
        onPointerDown={() => { draggingRef.current = true; }}
        onChange={(event) => onVolumeChange(Number(event.currentTarget.value))} className="player-volume" />
    </div>)}
  </div>;
}

export interface PlayerSettingsSelection {
  value: string;
  options: PlayerMenuOption<string>[];
  onSelect: (value: string) => void;
  status?: string;
  summary?: string;
}

function SettingsSelectionList({ label, selection }: { label: string; selection: PlayerSettingsSelection }) {
  const optionRefs = useRef<Array<HTMLButtonElement | null>>([]);
  const [focusedValue, setFocusedValue] = useState<string | null>(null);
  const selected = selection.options.findIndex((option) => option.value === selection.value && !option.disabled);
  const focused = selection.options.findIndex((option) => option.value === focusedValue && !option.disabled);
  const tabIndex = focused >= 0 ? focused : selected >= 0 ? selected : selection.options.findIndex((option) => !option.disabled);
  const focusOption = (start: number, direction = 1) => {
    const count = selection.options.length;
    for (let step = 0; step < count; step += 1) {
      const index = (start + step * direction + count * 2) % count;
      if (!selection.options[index].disabled) { optionRefs.current[index]?.focus(); return; }
    }
  };
  return <>
    {selection.summary && <p className="player-settings-selection__status" role="status">{selection.summary}</p>}
    <div role="listbox" aria-label={label} className="player-settings-selection" onKeyDown={(event) => {
      const active = optionRefs.current.findIndex((option) => option === document.activeElement);
      if (event.key === "ArrowDown") { event.preventDefault(); focusOption(active + 1); }
      else if (event.key === "ArrowUp") { event.preventDefault(); focusOption(active - 1, -1); }
      else if (event.key === "Home") { event.preventDefault(); focusOption(0); }
      else if (event.key === "End") { event.preventDefault(); focusOption(selection.options.length - 1, -1); }
    }}>
      {selection.options.map((option, index) => <button key={option.value} ref={(element) => { optionRefs.current[index] = element; }}
        type="button" role="option" aria-selected={option.value === selection.value} disabled={option.disabled} tabIndex={index === tabIndex ? 0 : -1}
        onFocus={() => setFocusedValue(option.value)}
        onClick={() => { if (!option.disabled) selection.onSelect(option.value); }}>
        <span>{option.label}{option.status && <small>{option.status}</small>}</span>
        {option.value === selection.value && <PlayerIcon name="check" />}
      </button>)}
    </div>
  </>;
}

export function PlayerSettings({ playbackRate, onRateChange, captions, captionScale, onCaptionScaleChange, subtitleOffset, onSubtitleOffsetChange, onOpenChange, compact = false, modal = false, selections, className = "" }: {
  playbackRate: number; onRateChange: (value: number) => void; captions: boolean;
  captionScale: number; onCaptionScaleChange: (value: number) => void;
  subtitleOffset: number; onSubtitleOffsetChange: (value: number) => void;
  onOpenChange?: (open: boolean) => void;
  compact?: boolean;
  modal?: boolean;
  selections?: { audio?: PlayerSettingsSelection; subtitles?: PlayerSettingsSelection; quality?: PlayerSettingsSelection };
  className?: string;
}) {
  const popup = usePlayerPopup(onOpenChange, 384, "end", modal);
  const [group, setGroup] = useState<"root" | "audio" | "subtitles" | "captions" | "quality" | "speed">("root");
  const audio = selections?.audio?.options.length ? selections.audio : undefined;
  const subtitles = selections?.subtitles?.options.length ? selections.subtitles : undefined;
  const quality = selections?.quality?.options.length ? selections.quality : undefined;
  const activeSelection = group === "audio" ? audio : group === "subtitles" ? subtitles : group === "quality" ? quality : undefined;
  const selectionLabel = (selection: PlayerSettingsSelection) => {
    const option = selection.options.find((item) => item.value === selection.value) ?? selection.options[0];
    return option?.shortLabel ?? option?.label;
  };
  const overlayRef = useRef<HTMLDivElement>(null);
  const pointerDismissRef = useRef(false);
  const { open, host, popupRef, triggerRef, updateOpen } = popup;
  const dismiss = useCallback((pointer = false) => {
    pointerDismissRef.current = pointer;
    updateOpen(false);
    if (!modal) {
      if (pointer) restorePlayerPointerFocus(popupRef.current?.contains(document.activeElement) ? popupRef.current : triggerRef.current);
      else triggerRef.current?.focus({ preventScroll: true });
    }
  }, [modal, popupRef, triggerRef, updateOpen]);

  useLayoutEffect(() => {
    if (!open || (group === "subtitles" && !captions && !subtitles) || (group === "captions" && !captions)
      || (group === "audio" && !audio) || (group === "quality" && !quality)) { setGroup("root"); return; }
    popupRef.current?.querySelector<HTMLElement>("[data-settings-initial]")?.focus({ preventScroll: true });
  }, [captions, Boolean(audio), Boolean(subtitles), Boolean(quality), group, host, open, popupRef]);

  useLayoutEffect(() => {
    const overlay = overlayRef.current;
    const dialog = popupRef.current;
    if (!open || !modal || !host || overlay?.parentElement !== host || !dialog) return;
    const player = triggerRef.current?.closest<HTMLElement>("[data-player-root='true']");
    // Keep the portal interactive inside native fullscreen and the rotated player.
    const siblings: Array<{ element: HTMLElement; inert: boolean }> = [];
    for (let child: HTMLElement | null = overlay; child?.parentElement; child = child.parentElement) {
      for (const sibling of Array.from(child.parentElement.children)) {
        if (sibling !== child && sibling instanceof HTMLElement) {
          siblings.push({ element: sibling, inert: sibling.hasAttribute("inert") });
          sibling.setAttribute("inert", "");
        }
      }
      if (child.parentElement === document.body) break;
    }
    const overflow = document.body.style.overflow;
    document.body.style.overflow = "hidden";
    const focusFirst = () => (dialog.querySelector<HTMLElement>("[data-settings-initial]") ?? dialog.querySelector<HTMLElement>("button, input"))?.focus({ preventScroll: true });
    const containFocus = (event: FocusEvent) => {
      if (!dialog.contains(event.target as Node)) focusFirst();
    };
    const keydown = (event: KeyboardEvent) => {
      if (event.key === "Escape") {
        event.preventDefault(); event.stopPropagation(); dismiss();
      } else if (event.key === "Tab") {
        const controls = Array.from(dialog.querySelectorAll<HTMLElement>("button:not(:disabled), input:not(:disabled), [tabindex='0']")).filter((element) => element.tabIndex >= 0);
        const first = controls[0];
        const last = controls[controls.length - 1];
        if (event.shiftKey && (document.activeElement === first || !dialog.contains(document.activeElement))) {
          event.preventDefault(); last?.focus();
        } else if (!event.shiftKey && (document.activeElement === last || !dialog.contains(document.activeElement))) {
          event.preventDefault(); first?.focus();
        }
      }
    };
    document.addEventListener("focusin", containFocus);
    document.addEventListener("keydown", keydown, true);
    return () => {
      document.removeEventListener("focusin", containFocus);
      document.removeEventListener("keydown", keydown, true);
      for (const { element, inert } of siblings) element.toggleAttribute("inert", inert);
      document.body.style.overflow = overflow;
      if (pointerDismissRef.current) (player ?? triggerRef.current)?.focus({ preventScroll: true });
      else triggerRef.current?.focus({ preventScroll: true });
      pointerDismissRef.current = false;
    };
  }, [dismiss, host, modal, open, popupRef, triggerRef]);

  const title = group === "subtitles" ? "Subtitles" : group === "captions" ? "Appearance & timing" : group === "audio" ? "Audio language" : group === "quality" ? "Quality" : group === "speed" ? "Playback speed" : "Settings";
  const panel = <div ref={popupRef} id={popup.id} role="dialog" aria-label="Player settings" aria-modal={modal || undefined}
    data-player-settings-dialog="true" className={`player-settings-panel${modal ? " player-settings-panel--modal" : ""}`}
    style={modal ? undefined : { ...popup.style, overflowY: "hidden" }}
    onBlur={modal ? undefined : popup.onBlur}
    onClick={(event) => event.stopPropagation()} onDoubleClick={(event) => event.stopPropagation()}
    onKeyDown={(event) => { if (event.key === "Escape") { event.preventDefault(); event.stopPropagation(); dismiss(); } }}>
    <header className="player-settings-header">
      {group !== "root" ? <button type="button" aria-label={group === "captions" && subtitles ? "Back to subtitles" : "Back to settings"} className="player-settings-header__button" data-settings-initial
        onClick={() => setGroup(group === "captions" && subtitles ? "subtitles" : "root")}><PlayerIcon name="back" /></button> : <span aria-hidden="true" />}
      <h2>{title}</h2>
      <button type="button" aria-label="Close settings" className="player-settings-header__button" onClick={(event) => dismiss(event.detail > 0)}><PlayerIcon name="close" /></button>
    </header>
    <div className="player-settings-body" key={group}>
      {group === "root" && <div className="player-settings-groups">
        {audio && <button type="button" className="player-settings-group" data-settings-initial onClick={() => setGroup("audio")}>
          <PlayerIcon name="audio" /><span>Audio language</span><span className="player-settings-group__value">{selectionLabel(audio)}</span><PlayerIcon name="chevron" className="player-settings-group__chevron" />
        </button>}
        {(captions || subtitles) && <button type="button" className="player-settings-group" data-settings-initial={!audio || undefined} onClick={() => setGroup("subtitles")}>
          <PlayerIcon name="captions" /><span>Subtitles</span>{subtitles && <span className="player-settings-group__value">{selectionLabel(subtitles)}</span>}<PlayerIcon name="chevron" className="player-settings-group__chevron" />
        </button>}
        {quality && <button type="button" className="player-settings-group" data-settings-initial={!audio && !captions && !subtitles || undefined} aria-description={quality.status} onClick={() => setGroup("quality")}>
          <PlayerIcon name="quality" /><span>Quality</span><span className="player-settings-group__value">{selectionLabel(quality)}</span><PlayerIcon name="chevron" className="player-settings-group__chevron" />
        </button>}
        <button type="button" className="player-settings-group" data-settings-initial={!audio && !captions && !subtitles && !quality || undefined} onClick={() => setGroup("speed")}>
          <PlayerIcon name="speed" /><span>Playback speed</span><span className="player-settings-group__value">{playbackRate}×</span><PlayerIcon name="chevron" className="player-settings-group__chevron" />
        </button>
      </div>}
      {group === "subtitles" && subtitles && captions && <button type="button" className="player-settings-group player-settings-subpage" onClick={() => setGroup("captions")}>
        <PlayerIcon name="settings" /><span>Appearance &amp; timing</span><PlayerIcon name="chevron" className="player-settings-group__chevron" />
      </button>}
      {activeSelection && <SettingsSelectionList label={title} selection={activeSelection} />}
      {group === "speed" && <div className="player-settings-options" role="group" aria-label="Playback speed">{[0.5, 0.75, 1, 1.25, 1.5, 2].map((rate) =>
        <button key={rate} type="button" aria-pressed={rate === playbackRate} onClick={() => onRateChange(rate)}>{rate}×{rate === playbackRate && <PlayerIcon name="check" />}</button>
      )}</div>}
      {(group === "captions" || group === "subtitles" && !subtitles) && captions && <>
        <fieldset><legend>Caption size</legend><div className="player-settings-options player-settings-options--captions">{[[0.8, "Small"], [1, "Medium"], [1.25, "Large"], [1.5, "Extra large"]].map(([scale, label]) =>
          <button key={scale} type="button" aria-pressed={Number(scale) === captionScale} onClick={() => onCaptionScaleChange(Number(scale))}>{label}{Number(scale) === captionScale && <PlayerIcon name="check" />}</button>
        )}</div></fieldset>
        <label className="player-settings-timing"><span>Subtitle timing</span><output>{subtitleOffset === 0 ? "Synced" : `${Math.abs(subtitleOffset)}s ${subtitleOffset < 0 ? "earlier" : "later"}`}</output>
          <input aria-label="Subtitle timing" type="range" min={-5} max={5} step={0.25} value={subtitleOffset} onChange={(event) => onSubtitleOffsetChange(Number(event.target.value))} />
        </label>
        <button type="button" className="player-settings-reset" disabled={subtitleOffset === 0} onClick={() => onSubtitleOffsetChange(0)}>Reset timing</button>
      </>}
    </div>
  </div>;
  return <div className={`player-control-menu ${className}`.trim()} ref={popup.rootRef} data-open={open}>
    <button ref={triggerRef} type="button" className={compact ? "player-control-button player-control-menu__trigger" : "player-control-menu__trigger"} aria-label="Player settings" aria-haspopup="dialog" aria-expanded={open} aria-controls={popup.id}
      onClick={(event) => { if (open) dismiss(event.detail > 0); else updateOpen(true); }}
      onKeyDown={(event) => { if (event.key === "ArrowDown") { event.preventDefault(); updateOpen(true); } else if (event.key === "Escape" && open) { event.preventDefault(); dismiss(); } }}>
      <PlayerIcon name="settings" />{!compact && <span>Settings</span>}
    </button>
    {open && popup.render(modal ? <div ref={overlayRef} className="player-settings-overlay"
      onPointerDown={(event) => { event.stopPropagation(); if (event.target === event.currentTarget) { event.preventDefault(); dismiss(true); } }}
      onClick={(event) => event.stopPropagation()} onDoubleClick={(event) => event.stopPropagation()}>{panel}</div> : panel)}
  </div>;
}

export interface PlayerMenuOption<T extends string | number> {
  value: T;
  label: string;
  shortLabel?: string;
  disabled?: boolean;
  status?: string;
}

interface PlayerControlMenuProps<T extends string | number> {
  label: string;
  icon: PlayerIconName;
  value: T;
  options: PlayerMenuOption<T>[];
  onSelect: (value: T) => void;
  onOpenChange?: (open: boolean) => void;
  status?: string;
  summary?: string;
  compact?: boolean;
}

export function PlayerControlMenu<T extends string | number>({
  label,
  icon,
  value,
  options,
  onSelect,
  onOpenChange,
  status,
  summary,
  compact = false,
}: PlayerControlMenuProps<T>) {
  const popup = usePlayerPopup(onOpenChange, compact ? 224 : undefined);
  const { open, rootRef, triggerRef, updateOpen } = popup;
  const optionRefs = useRef<Array<HTMLButtonElement | null>>([]);
  const listboxId = popup.id;
  const selectedIndex = Math.max(0, options.findIndex((option) => Object.is(option.value, value)));
  const selected = options[selectedIndex] ?? options[0];

  const focusOption = (index: number, direction = 1) => {
    for (let offset = 0; offset < options.length; offset += 1) {
      const normalized = (index + offset * direction + options.length * 2) % options.length;
      if (!options[normalized]?.disabled) {
        optionRefs.current[normalized]?.focus();
        return;
      }
    }
  };

  const openAndFocus = (index: number) => {
    updateOpen(true);
    window.requestAnimationFrame(() => focusOption(index));
  };

  return (
    <div className="player-control-menu" ref={rootRef} onBlur={popup.onBlur} data-open={open}>
      <button
        ref={triggerRef}
        type="button"
        className="player-control-menu__trigger"
        aria-label={`${label}: ${selected?.label ?? "Unavailable"}`}
        aria-description={status}
        aria-haspopup="listbox"
        aria-expanded={open}
        aria-controls={listboxId}
        onClick={(event) => {
          if (open) { updateOpen(false); restorePlayerPointerClickFocus(event); }
          else openAndFocus(selectedIndex);
        }}
        onKeyDown={(event) => {
          if (event.key === "ArrowDown") {
            event.preventDefault();
            openAndFocus(selectedIndex);
          } else if (event.key === "ArrowUp") {
            event.preventDefault();
            openAndFocus(Math.max(0, selectedIndex - 1));
          } else if (event.key === "Escape" && open) {
            event.preventDefault(); popup.dismiss();
          }
        }}
      >
        <PlayerIcon name={icon} />
        <span>{selected?.shortLabel ?? selected?.label ?? label}</span>
        <PlayerIcon name="chevron" className="player-control-menu__chevron" />
      </button>

      {open && popup.render(
        <div
          ref={popup.popupRef}
          style={popup.style}
          onBlur={popup.onBlur}
          id={listboxId}
          role="listbox"
          aria-label={label}
          className="player-control-menu__list"
          onKeyDown={(event) => {
            const activeIndex = optionRefs.current.findIndex((item) => item === document.activeElement);
            if (event.key === "ArrowDown") {
              event.preventDefault();
              focusOption(activeIndex + 1);
            } else if (event.key === "ArrowUp") {
              event.preventDefault();
              focusOption(activeIndex - 1, -1);
            } else if (event.key === "Home") {
              event.preventDefault();
              focusOption(0);
            } else if (event.key === "End") {
              event.preventDefault();
              focusOption(options.length - 1, -1);
            } else if (event.key === "Escape") {
              event.preventDefault();
              updateOpen(false);
              triggerRef.current?.focus();
            }
          }}
        >
          <span className="player-control-menu__label">{label}</span>
          {summary && <div className="player-control-menu__summary" role="status">{summary}</div>}
          {options.map((option, index) => {
            const isSelected = Object.is(option.value, value);
            return (
              <button
                key={`${String(option.value)}-${index}`}
                ref={(element) => { optionRefs.current[index] = element; }}
                type="button"
                role="option"
                aria-selected={isSelected}
                aria-disabled={option.disabled || undefined}
                disabled={option.disabled}
                tabIndex={!option.disabled && index === selectedIndex ? 0 : -1}
                onClick={(event) => {
                  if (option.disabled) return;
                  onSelect(option.value);
                  updateOpen(false);
                  if (event.detail > 0) restorePlayerPointerClickFocus(event);
                  else triggerRef.current?.focus();
                }}
              >
                <span>{option.label}{option.status && <small>{option.status}</small>}</span>
                {isSelected && <PlayerIcon name="check" />}
              </button>
            );
          })}
        </div>
      )}
    </div>
  );
}
