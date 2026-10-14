import React, { useEffect, useId, useLayoutEffect, useRef, useState } from "react";
import { createPortal } from "react-dom";
import { useAppMotion } from "../../motion/motionSystem";
import { cn } from "../../utils/cn";
import "./AdminSelect.css";

export interface AdminSelectOption {
  value: string;
  label: string;
  disabled?: boolean;
}

export interface AdminSelectProps {
  options: readonly AdminSelectOption[];
  value: string;
  onChange: (value: string) => void;
  id?: string;
  name?: string;
  label?: string;
  className?: string;
  disabled?: boolean;
  "aria-label"?: string;
  "aria-labelledby"?: string;
  "aria-describedby"?: string;
}

interface PopupPosition {
  left: number;
  top: number;
  width: number;
  maxHeight: number;
  theme?: string;
  variables: React.CSSProperties;
}

const PORTAL_VARIABLES = [
  "--font-body", "--font-mono", "--text-primary", "--text-secondary", "--text-muted", "--text-accent",
  "--bg-surface-container", "--bg-surface-container-high", "--bg-surface-container-lowest", "--glass-border",
  "--accent-primary", "--radius", "--admin-control-bg", "--admin-control-border", "--admin-control-radius",
  "--admin-menu-bg", "--admin-menu-active-bg", "--admin-menu-selected-bg",
];

/** Select-only combobox. Active options are previews until explicitly committed. */
export function AdminSelect({ options, value, onChange, id, name, label, className, disabled,
  "aria-label": ariaLabel, "aria-labelledby": ariaLabelledBy, "aria-describedby": ariaDescribedBy }: AdminSelectProps) {
  const generatedId = useId();
  const controlId = id ?? `admin-select-${generatedId}`;
  const listId = `${controlId}-list`;
  const labelId = `${controlId}-label`;
  const trigger = useRef<HTMLButtonElement>(null);
  const menu = useRef<HTMLDivElement>(null);
  const typeahead = useRef({ text: "", timestamp: 0 });
  const [open, setOpen] = useState(false);
  const [activeIndex, setActiveIndex] = useState(-1);
  const [position, setPosition] = useState<PopupPosition | null>(null);
  const { reduced } = useAppMotion();
  const selectedIndex = options.findIndex((option) => option.value === value);
  const enabledIndices = options.flatMap((option, index) => option.disabled ? [] : [index]);
  const unavailable = Boolean(disabled) || enabledIndices.length === 0;
  const expanded = open && !unavailable;
  const selected = options[selectedIndex];

  const close = () => {
    setOpen(false);
    typeahead.current = { text: "", timestamp: 0 };
  };
  const initialIndex = (last = false) => selectedIndex >= 0 && !options[selectedIndex].disabled
    ? selectedIndex : enabledIndices[last ? enabledIndices.length - 1 : 0] ?? -1;
  const show = (index = initialIndex()) => {
    if (unavailable) return;
    setActiveIndex(index);
    setOpen(true);
  };
  const commit = (index: number) => {
    const option = options[index];
    if (!unavailable && option && !option.disabled && option.value !== value) onChange(option.value);
    close();
  };

  useEffect(() => {
    if (unavailable) {
      setOpen(false);
      typeahead.current = { text: "", timestamp: 0 };
    }
  }, [unavailable]);

  useEffect(() => {
    if (expanded && (!options[activeIndex] || options[activeIndex].disabled)) {
      setActiveIndex(initialIndex());
    }
  }, [expanded, activeIndex, options, value]);

  useLayoutEffect(() => {
    if (!expanded) {
      setPosition(null);
      return;
    }
    const updatePosition = () => {
      const button = trigger.current;
      if (!button) return;
      const rect = button.getBoundingClientRect();
      const viewport = window.visualViewport;
      const viewportLeft = viewport?.offsetLeft ?? 0;
      const viewportTop = viewport?.offsetTop ?? 0;
      const viewportWidth = viewport?.width ?? window.innerWidth;
      const viewportHeight = viewport?.height ?? window.innerHeight;
      // Do not leave an orphaned popup when its control has scrolled out of view.
      if (rect.bottom < viewportTop || rect.top > viewportTop + viewportHeight ||
          rect.right < viewportLeft || rect.left > viewportLeft + viewportWidth) {
        setOpen(false);
        return;
      }
      const margin = 8;
      const gap = 6;
      const width = Math.min(Math.max(rect.width, 180), Math.max(0, viewportWidth - margin * 2));
      const left = Math.max(viewportLeft + margin, Math.min(rect.left, viewportLeft + viewportWidth - margin - width));
      const above = Math.max(0, rect.top - viewportTop - margin - gap);
      const below = Math.max(0, viewportTop + viewportHeight - rect.bottom - margin - gap);
      const desiredHeight = Math.min(320, options.length * 44 + 10);
      const opensBelow = below >= Math.min(desiredHeight, 132) || below >= above;
      let maxHeight = Math.min(320, opensBelow ? below : above);
      const measuredHeight = menu.current?.getBoundingClientRect().height || desiredHeight;
      let top = opensBelow ? rect.bottom + gap : rect.top - gap - Math.min(measuredHeight, maxHeight);
      // Extremely short viewports still allow the options to scroll within the viewport.
      if (maxHeight < 44) {
        maxHeight = Math.max(0, viewportHeight - margin * 2);
        top = viewportTop + margin;
      }
      const computed = window.getComputedStyle(button);
      const variables: Record<string, string> = {};
      for (const variable of PORTAL_VARIABLES) {
        const resolved = computed.getPropertyValue(variable).trim();
        if (resolved) variables[variable] = resolved;
      }
      setPosition({ left, top, width, maxHeight, variables: variables as React.CSSProperties,
        theme: button.closest("[data-theme]")?.getAttribute("data-theme") ?? undefined });
    };
    updatePosition();
    const scroll = (event: Event) => {
      if (event.target instanceof Node && menu.current?.contains(event.target)) return;
      updatePosition();
    };
    window.addEventListener("resize", updatePosition);
    document.addEventListener("scroll", scroll, true);
    window.visualViewport?.addEventListener("resize", updatePosition);
    window.visualViewport?.addEventListener("scroll", updatePosition);
    const observer = typeof ResizeObserver !== "undefined" ? new ResizeObserver(updatePosition) : null;
    if (trigger.current) observer?.observe(trigger.current);
    if (menu.current) observer?.observe(menu.current);
    const themeRoot = trigger.current?.closest("[data-theme]");
    const themeObserver = typeof MutationObserver !== "undefined" ? new MutationObserver(updatePosition) : null;
    if (themeRoot) themeObserver?.observe(themeRoot, { attributes: true, attributeFilter: ["class", "style", "data-theme"] });
    return () => {
      window.removeEventListener("resize", updatePosition);
      document.removeEventListener("scroll", scroll, true);
      window.visualViewport?.removeEventListener("resize", updatePosition);
      window.visualViewport?.removeEventListener("scroll", updatePosition);
      observer?.disconnect();
      themeObserver?.disconnect();
    };
  }, [expanded, options, Boolean(position)]);

  useEffect(() => {
    if (!expanded) return;
    const dismissOutside = (event: Event) => {
      const target = event.target;
      if (target instanceof Node && !trigger.current?.contains(target) && !menu.current?.contains(target)) close();
    };
    document.addEventListener("pointerdown", dismissOutside);
    document.addEventListener("focusin", dismissOutside);
    return () => {
      document.removeEventListener("pointerdown", dismissOutside);
      document.removeEventListener("focusin", dismissOutside);
    };
  }, [expanded]);

  useLayoutEffect(() => {
    const list = menu.current;
    const item = list?.querySelector<HTMLElement>(`[data-index="${activeIndex}"]`);
    if (!expanded || !list || !item) return;
    const itemTop = item.offsetTop;
    const itemBottom = itemTop + item.offsetHeight;
    if (itemTop < list.scrollTop) list.scrollTop = itemTop;
    else if (itemBottom > list.scrollTop + list.clientHeight) list.scrollTop = itemBottom - list.clientHeight;
  }, [activeIndex, expanded, position?.maxHeight]);

  const handleKeyDown = (event: React.KeyboardEvent<HTMLButtonElement>) => {
    if (unavailable) return;
    if (Date.now() - typeahead.current.timestamp > 700) typeahead.current.text = "";
    if (event.key === "Escape") {
      if (expanded) { event.preventDefault(); event.stopPropagation(); close(); }
      return;
    }
    if (event.key === "Tab") {
      if (expanded) commit(activeIndex);
      return;
    }
    if (event.key === "ArrowDown" || event.key === "ArrowUp") {
      event.preventDefault();
      if (event.altKey && expanded && event.key === "ArrowUp") { commit(activeIndex); return; }
      if (event.altKey && expanded && event.key === "ArrowDown") return;
      if (!expanded) { show(initialIndex(event.key === "ArrowUp")); return; }
      const offset = event.key === "ArrowDown" ? 1 : -1;
      const current = enabledIndices.indexOf(activeIndex);
      setActiveIndex(enabledIndices[Math.max(0, Math.min(enabledIndices.length - 1, current + offset))]);
      return;
    }
    if (event.key === "Home" || event.key === "End") {
      event.preventDefault();
      show(enabledIndices[event.key === "Home" ? 0 : enabledIndices.length - 1]);
      return;
    }
    if (event.key === "Enter" || (event.key === " " && !typeahead.current.text)) {
      event.preventDefault();
      if (expanded) commit(activeIndex); else show();
      return;
    }
    if (event.key.length !== 1 || event.altKey || event.ctrlKey || event.metaKey) return;
    event.preventDefault();
    const now = Date.now();
    const text = (now - typeahead.current.timestamp > 700 ? "" : typeahead.current.text) + event.key.toLocaleLowerCase();
    typeahead.current = { text, timestamp: now };
    const repeated = [...text].every((character) => character === text[0]);
    const query = repeated ? text[0] : text;
    const start = expanded ? activeIndex : initialIndex();
    const ordered = Array.from({ length: options.length }, (_, index) => (start + (repeated ? 1 : 0) + index + options.length) % options.length);
    const match = ordered.find((index) => !options[index].disabled && options[index].label.toLocaleLowerCase().startsWith(query));
    if (match !== undefined) show(match);
  };

  const accessibleLabel = ariaLabelledBy ?? (!ariaLabel && label ? labelId : undefined);
  return <div className={cn("admin-select", className)}>
    {label && <label id={labelId} htmlFor={controlId} className="admin-select__label">{label}</label>}
    {name && <input type="hidden" name={name} value={value} disabled={disabled} />}
    <button ref={trigger} id={controlId} type="button" role="combobox" className="admin-select__trigger"
      disabled={unavailable} aria-expanded={expanded} aria-haspopup="listbox" aria-controls={expanded ? listId : undefined}
      aria-activedescendant={expanded && activeIndex >= 0 ? `${listId}-option-${activeIndex}` : undefined}
      aria-label={ariaLabel} aria-labelledby={accessibleLabel} aria-describedby={ariaDescribedBy}
      onKeyDown={handleKeyDown} onClick={() => { if (expanded) close(); else show(); }}>
      <span className="admin-select__value">{selected?.label ?? "Choose an option"}</span>
      <svg aria-hidden="true" width="16" height="16" viewBox="0 0 16 16" className="admin-select__chevron"><path d="m4 6 4 4 4-4" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round" /></svg>
    </button>
    {expanded && position && createPortal(<div ref={menu} id={listId} role="listbox"
      className="admin-select-portal" data-theme={position.theme} data-reduced-motion={reduced}
      aria-label={ariaLabel} aria-labelledby={accessibleLabel ?? (!ariaLabel ? controlId : undefined)}
      style={{ ...position.variables, left: position.left, top: position.top, width: position.width, maxHeight: position.maxHeight }}>
      {options.map((option, index) => <div key={option.value} id={`${listId}-option-${index}`} role="option"
        className="admin-select__option" data-index={index} data-active={index === activeIndex}
        aria-selected={option.value === value} aria-disabled={option.disabled || undefined}
        onPointerMove={() => { if (!option.disabled) setActiveIndex(index); }}
        onMouseDown={(event) => event.preventDefault()}
        onClick={() => { if (!option.disabled) { commit(index); trigger.current?.focus(); } }}>
        <span>{option.label}</span><span className="admin-select__check" aria-hidden="true">{option.value === value ? "✓" : ""}</span>
      </div>)}
    </div>, document.body)}
  </div>;
}
