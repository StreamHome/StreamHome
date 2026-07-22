import React, { useState } from "react";
import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import { hasSubtitleOptions, PlayerControlMenu, PlayerIconButton } from "./PlayerControls";

describe("subtitle control availability", () => {
  it("hides the subtitle menu on every presentation when no tracks exist", () => {
    expect(hasSubtitleOptions([])).toBe(false);
    expect(hasSubtitleOptions([{ language: "en" }])).toBe(true);
  });
});

describe("player icon controls", () => {
  it("renders icon-only controls with accessible labels and tooltips", () => {
    render(<PlayerIconButton icon="play" label="Play" onClick={() => undefined} />);

    const button = screen.getByRole("button", { name: "Play" });
    expect(button.getAttribute("data-tooltip")).toBe("Play");
    expect(button.querySelector("svg[aria-hidden='true']")).not.toBeNull();
    expect(button.textContent).toBe("");
  });
});

describe("player control menu", () => {
  function QualityFixture({ onOpenChange = () => undefined }: { onOpenChange?: (open: boolean) => void }) {
    const [quality, setQuality] = useState(-1);
    return (
      <PlayerControlMenu
        label="Quality"
        icon="quality"
        value={quality}
        options={[{ value: -1, label: "Auto" }, { value: 1, label: "720p" }, { value: 2, label: "480p" }]}
        onSelect={setQuality}
        onOpenChange={onOpenChange}
      />
    );
  }

  it("opens a themed listbox and updates the selected option", () => {
    render(<QualityFixture />);

    const trigger = screen.getByRole("button", { name: "Quality: Auto" });
    fireEvent.click(trigger);
    expect(trigger.getAttribute("aria-expanded")).toBe("true");
    expect(screen.getByRole("listbox", { name: "Quality" })).not.toBeNull();

    fireEvent.click(screen.getByRole("option", { name: "480p" }));
    expect(screen.getByRole("button", { name: "Quality: 480p" })).not.toBeNull();
    expect(screen.queryByRole("listbox", { name: "Quality" })).toBeNull();
  });

  it("supports keyboard opening, navigation, Escape, and focus restoration", () => {
    const onOpenChange = vi.fn();
    render(<QualityFixture onOpenChange={onOpenChange} />);

    const trigger = screen.getByRole("button", { name: "Quality: Auto" });
    trigger.focus();
    fireEvent.keyDown(trigger, { key: "ArrowDown" });
    const listbox = screen.getByRole("listbox", { name: "Quality" });
    fireEvent.keyDown(listbox, { key: "End" });
    expect(document.activeElement).toBe(screen.getByRole("option", { name: "480p" }));

    fireEvent.keyDown(listbox, { key: "Escape" });
    expect(document.activeElement).toBe(trigger);
    expect(screen.queryByRole("listbox", { name: "Quality" })).toBeNull();
    expect(onOpenChange).toHaveBeenCalledWith(true);
    expect(onOpenChange).toHaveBeenCalledWith(false);
  });
});
