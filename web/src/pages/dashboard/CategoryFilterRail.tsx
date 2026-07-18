import React, { useEffect, useRef } from "react";
import { useAppMotion } from "../../motion/motionSystem";
import type { CategoryOption } from "./catalogPresentation";

export function CategoryFilterRail({
  options,
  active,
  variant,
  onSelect,
}: {
  options: CategoryOption[];
  active: string;
  variant: "shared" | "ember";
  onSelect: (category: string) => void;
}) {
  const activeButton = useRef<HTMLButtonElement>(null);
  const { reduced } = useAppMotion();

  useEffect(() => {
    activeButton.current?.scrollIntoView?.({ behavior: reduced ? "auto" : "smooth", block: "nearest", inline: "center" });
  }, [active, reduced]);

  return <nav className={`category-filter category-filter--${variant}`} aria-label="Catalog categories"><div className="category-filter__track">{options.map((option) => {
    const selected = option.value.toLocaleLowerCase() === active.toLocaleLowerCase();
    return <button ref={selected ? activeButton : undefined} key={option.value} type="button" data-kind={option.kind} data-active={selected} aria-pressed={selected} onClick={() => onSelect(option.value)}>{option.label}</button>;
  })}</div></nav>;
}
