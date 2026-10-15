import React, { createContext, useCallback, useContext, useEffect, useId, useLayoutEffect, useMemo, useRef, useState } from "react";
import { createPortal } from "react-dom";

export type PlayerIconName =
  | "audio"
  | "captions"
  | "check"
  | "chevron"
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
  | "speed"
  | "settings"
  | "volume";

interface PlayerIconProps {
  name: PlayerIconName;
  className?: string;
}

export function hasSubtitleOptions(options: readonly unknown[]): boolean {
  return options.length > 0;
}

export function PlayerIcon({ name, className = "" }: PlayerIconProps) {
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

  if (name === "play") return <svg {...common}><path d="m8 5 11 7-11 7V5Z" fill="currentColor" stroke="none" /></svg>;
  if (name === "pause") return <svg {...common}><path d="M8 5v14M16 5v14" strokeWidth="2.4" /></svg>;
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
  if (name === "chevron") return <svg {...common}><path d="m7 9 5 5 5-5" /></svg>;
  return <svg {...common}><path d="m7 12 3 3 7-7" /></svg>;
}

interface PlayerIconButtonProps extends React.ButtonHTMLAttributes<HTMLButtonElement> {
  icon: PlayerIconName;
  label: string;
}

export function PlayerIconButton({ icon, label, className = "", ...props }: PlayerIconButtonProps) {
  return (
    <button
      type="button"
      aria-label={label}
      data-tooltip={label}
      className={`player-control-button ${className}`.trim()}
      {...props}
    >
      <PlayerIcon name={icon} />
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

function usePlayerPopup(onOpenChange?: (open: boolean) => void) {
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
      const menuWidth = Math.max(1, Math.min(width - 24, player?.dataset.mobilePlayer === "true" ? 360 : 288));
      const roomAbove = Math.max(0, y - 20);
      const roomBelow = Math.max(0, height - y - anchorHeight - 20);
      const above = roomAbove >= Math.min(popup.scrollHeight, 160) || roomAbove >= roomBelow;
      const maxHeight = Math.max(1, Math.min(height - 24, above ? roomAbove : roomBelow));
      const menuHeight = Math.min(popup.scrollHeight, maxHeight);
      setStyle({ position: player ? "absolute" : "fixed", width: menuWidth,
        left: Math.max(12, Math.min(x + anchorWidth - menuWidth, width - menuWidth - 12)),
        top: above ? Math.max(12, y - menuHeight - 8) : Math.min(height - menuHeight - 12, y + anchorHeight + 8),
        right: "auto", bottom: "auto", maxHeight, overflowY: "auto" });
    };
    position();
    window.addEventListener("resize", position);
    const observer = typeof ResizeObserver === "undefined" ? null : new ResizeObserver(position);
    if (player) observer?.observe(player);
    observer?.observe(anchor!);
    return () => { window.removeEventListener("resize", position); observer?.disconnect(); };
  }, [host, open]);

  useEffect(() => {
    if (!open) return;
    const outside = (event: PointerEvent) => {
      if (!rootRef.current?.contains(event.target as Node) && !popupRef.current?.contains(event.target as Node)) updateOpen(false);
    };
    document.addEventListener("pointerdown", outside);
    return () => document.removeEventListener("pointerdown", outside);
  }, [open, updateOpen]);

  const dismiss = () => { updateOpen(false); triggerRef.current?.focus(); };
  const onBlur = (event: React.FocusEvent) => {
    const target = event.relatedTarget as Node | null;
    if (!rootRef.current?.contains(target) && !popupRef.current?.contains(target)) updateOpen(false);
  };
  const render = (content: React.ReactElement) => host ? createPortal(content, host) : content;
  return { id, open, updateOpen, rootRef, triggerRef, popupRef, style, render, dismiss, onBlur };
}

export function PlayerSettings({ playbackRate, onRateChange, captions, captionScale, onCaptionScaleChange, subtitleOffset, onSubtitleOffsetChange, onOpenChange }: {
  playbackRate: number; onRateChange: (value: number) => void; captions: boolean;
  captionScale: number; onCaptionScaleChange: (value: number) => void;
  subtitleOffset: number; onSubtitleOffsetChange: (value: number) => void;
  onOpenChange?: (open: boolean) => void;
}) {
  const popup = usePlayerPopup(onOpenChange);
  return <div className="player-control-menu" ref={popup.rootRef} data-open={popup.open}>
    <button ref={popup.triggerRef} type="button" className="player-control-menu__trigger" aria-label="Player settings" aria-haspopup="dialog" aria-expanded={popup.open} aria-controls={popup.id}
      onClick={() => popup.updateOpen(!popup.open)}
      onKeyDown={(event) => { if (event.key === "ArrowDown") { event.preventDefault(); popup.updateOpen(true); window.requestAnimationFrame(() => popup.popupRef.current?.querySelector<HTMLButtonElement>("button")?.focus()); } else if (event.key === "Escape" && popup.open) { event.preventDefault(); popup.dismiss(); } }}>
      <PlayerIcon name="settings" /><span>Settings</span>
    </button>
    {popup.open && popup.render(<div ref={popup.popupRef} id={popup.id} role="dialog" aria-label="Player settings" className="player-control-menu__list player-settings-panel" style={popup.style} onBlur={popup.onBlur}
      onKeyDown={(event) => { if (event.key === "Escape") { event.preventDefault(); event.stopPropagation(); popup.dismiss(); } }}>
      <span className="player-control-menu__label">Settings</span>
      <fieldset><legend>Playback speed</legend><div className="player-settings-options">{[0.5, 0.75, 1, 1.25, 1.5, 2].map((rate) => <button key={rate} type="button" aria-pressed={rate === playbackRate} onClick={() => onRateChange(rate)}>{rate}×</button>)}</div></fieldset>
      {captions && <><fieldset><legend>Caption size</legend><div className="player-settings-options">{[[0.8, "Small"], [1, "Medium"], [1.25, "Large"], [1.5, "Extra large"]].map(([scale, label]) => <button key={scale} type="button" aria-pressed={Number(scale) === captionScale} onClick={() => onCaptionScaleChange(Number(scale))}>{label}</button>)}</div></fieldset>
      <label className="player-settings-timing">Subtitle timing<input aria-label="Subtitle timing" type="range" min={-5} max={5} step={0.25} value={subtitleOffset} onChange={(event) => onSubtitleOffsetChange(Number(event.target.value))} /><span>{subtitleOffset === 0 ? "Synced" : `${Math.abs(subtitleOffset)}s ${subtitleOffset < 0 ? "earlier" : "later"}`}</span></label><button type="button" className="player-settings-reset" onClick={() => onSubtitleOffsetChange(0)}>Reset timing</button></>}
    </div>)}
  </div>;
}

export interface PlayerMenuOption<T extends string | number> {
  value: T;
  label: string;
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
}

export function PlayerControlMenu<T extends string | number>({
  label,
  icon,
  value,
  options,
  onSelect,
  onOpenChange,
  status,
}: PlayerControlMenuProps<T>) {
  const popup = usePlayerPopup(onOpenChange);
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
        onClick={() => open ? updateOpen(false) : openAndFocus(selectedIndex)}
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
        <span>{selected?.label ?? label}{status && <small className="player-control-menu__status" aria-hidden="true">{status}</small>}</span>
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
          {status && <div className="player-control-menu__summary" role="status">{status}</div>}
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
                onClick={() => {
                  if (option.disabled) return;
                  onSelect(option.value);
                  updateOpen(false);
                  triggerRef.current?.focus();
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
