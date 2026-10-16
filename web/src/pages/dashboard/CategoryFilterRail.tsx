import React, { useEffect, useRef } from "react";
import { useAppMotion } from "../../motion/motionSystem";
import type { CategoryOption } from "./catalogPresentation";

export function CategoryFilterRail({
  options,
  active,
  variant,
  onSelect,
  preservePageScroll = false,
}: {
  options: CategoryOption[];
  active: string;
  variant: "shared" | "ember";
  onSelect: (category: string) => void;
  preservePageScroll?: boolean;
}) {
  const activeButton = useRef<HTMLButtonElement>(null);
  const track = useRef<HTMLDivElement>(null);
  const { reduced } = useAppMotion();

  useEffect(() => {
    if (preservePageScroll) {
      const element = track.current;
      const button = activeButton.current;
      if (!element || !button) return;
      const left = element.scrollLeft + button.getBoundingClientRect().left - element.getBoundingClientRect().left - (element.clientWidth - button.offsetWidth) / 2;
      element.scrollTo?.({ left: Math.max(0, left), behavior: reduced ? "auto" : "smooth" });
      return;
    }
    activeButton.current?.scrollIntoView?.({ behavior: reduced ? "auto" : "smooth", block: "nearest", inline: "center" });
  }, [active, reduced, preservePageScroll]);

  return <nav className={`category-filter category-filter--${variant}`} aria-label="Catalog categories"><div ref={track} className="category-filter__track">{options.map((option) => {
    const selected = option.value.toLocaleLowerCase() === active.toLocaleLowerCase();
    return <button ref={selected ? activeButton : undefined} key={option.value} type="button" data-kind={option.kind} data-active={selected} aria-pressed={selected} onClick={() => onSelect(option.value)}>{option.label}</button>;
  })}</div></nav>;
}
