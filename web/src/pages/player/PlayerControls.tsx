import React, { useEffect, useId, useRef, useState } from "react";

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

export interface PlayerMenuOption<T extends string | number> {
  value: T;
  label: string;
}

interface PlayerControlMenuProps<T extends string | number> {
  label: string;
  icon: PlayerIconName;
  value: T;
  options: PlayerMenuOption<T>[];
  onSelect: (value: T) => void;
  onOpenChange?: (open: boolean) => void;
}

export function PlayerControlMenu<T extends string | number>({
  label,
  icon,
  value,
  options,
  onSelect,
  onOpenChange,
}: PlayerControlMenuProps<T>) {
  const [open, setOpen] = useState(false);
  const rootRef = useRef<HTMLDivElement>(null);
  const triggerRef = useRef<HTMLButtonElement>(null);
  const optionRefs = useRef<Array<HTMLButtonElement | null>>([]);
  const listboxId = useId();
  const selectedIndex = Math.max(0, options.findIndex((option) => Object.is(option.value, value)));
  const selected = options[selectedIndex] ?? options[0];

  const updateOpen = (next: boolean) => {
    setOpen(next);
    onOpenChange?.(next);
  };

  const focusOption = (index: number) => {
    const normalized = (index + options.length) % options.length;
    optionRefs.current[normalized]?.focus();
  };

  useEffect(() => {
    if (!open) return;
    const handlePointerDown = (event: PointerEvent) => {
      if (!rootRef.current?.contains(event.target as Node)) updateOpen(false);
    };
    document.addEventListener("pointerdown", handlePointerDown);
    return () => document.removeEventListener("pointerdown", handlePointerDown);
  }, [open]);

  useEffect(() => () => onOpenChange?.(false), [onOpenChange]);

  const openAndFocus = (index: number) => {
    updateOpen(true);
    window.requestAnimationFrame(() => focusOption(index));
  };

  return (
    <div className="player-control-menu" ref={rootRef} data-open={open}>
      <button
        ref={triggerRef}
        type="button"
        className="player-control-menu__trigger"
        aria-label={`${label}: ${selected?.label ?? "Unavailable"}`}
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
          }
        }}
      >
        <PlayerIcon name={icon} />
        <span>{selected?.label ?? label}</span>
        <PlayerIcon name="chevron" className="player-control-menu__chevron" />
      </button>

      {open && (
        <div
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
              focusOption(activeIndex - 1);
            } else if (event.key === "Home") {
              event.preventDefault();
              focusOption(0);
            } else if (event.key === "End") {
              event.preventDefault();
              focusOption(options.length - 1);
            } else if (event.key === "Escape") {
              event.preventDefault();
              updateOpen(false);
              triggerRef.current?.focus();
            }
          }}
        >
          <span className="player-control-menu__label">{label}</span>
          {options.map((option, index) => {
            const isSelected = Object.is(option.value, value);
            return (
              <button
                key={`${String(option.value)}-${index}`}
                ref={(element) => { optionRefs.current[index] = element; }}
                type="button"
                role="option"
                aria-selected={isSelected}
                tabIndex={index === selectedIndex ? 0 : -1}
                onClick={() => {
                  onSelect(option.value);
                  updateOpen(false);
                  triggerRef.current?.focus();
                }}
              >
                <span>{option.label}</span>
                {isSelected && <PlayerIcon name="check" />}
              </button>
            );
          })}
        </div>
      )}
    </div>
  );
}
