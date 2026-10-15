import React, { useState } from "react";
import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import { hasSubtitleOptions, PlayerControlMenu, PlayerIconButton, PlayerMenuProvider, PlayerSettings, PlayerVolume } from "./PlayerControls";

function pointerClick(target: HTMLElement) {
  fireEvent.pointerDown(target, { pointerType: "mouse", pointerId: 1 });
  act(() => target.focus());
  fireEvent.pointerUp(target, { pointerType: "mouse", pointerId: 1 });
  fireEvent.click(target, { detail: 1 });
}

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

  it("releases completed pointer focus while preserving keyboard activation focus", () => {
    const action = vi.fn();
    const view = render(<div data-player-root="true" tabIndex={-1}><PlayerIconButton icon="rewind" label="Rewind" onClick={action} /></div>);
    const root = view.container.firstElementChild;
    const button = screen.getByRole("button", { name: "Rewind" });

    pointerClick(button);
    expect(action).toHaveBeenCalledTimes(1);
    expect(document.activeElement).toBe(root);

    act(() => button.focus());
    fireEvent.click(button, { detail: 0 });
    expect(action).toHaveBeenCalledTimes(2);
    expect(document.activeElement).toBe(button);
  });
});

describe("player volume control", () => {
  function VolumeFixture({
    onVolumeChange = () => undefined,
    onToggleMute = () => undefined,
    onOpenChange,
  }: {
    onVolumeChange?: (value: number) => void;
    onToggleMute?: () => void;
    onOpenChange?: (open: boolean) => void;
  }) {
    const [volume, setVolume] = useState(0.65);
    const [muted, setMuted] = useState(false);
    return <PlayerVolume
      volume={volume}
      muted={muted}
      onVolumeChange={(value) => { setVolume(value); onVolumeChange(value); }}
      onToggleMute={() => { setMuted((current) => !current); onToggleMute(); }}
      onOpenChange={onOpenChange}
    />;
  }

  it("reveals a textless vertical slider on hover without stealing focus or changing audio", () => {
    const onVolumeChange = vi.fn();
    const onToggleMute = vi.fn();
    render(<><VolumeFixture onVolumeChange={onVolumeChange} onToggleMute={onToggleMute} /><button type="button">Outside</button></>);

    const trigger = screen.getByRole("button", { name: "Volume" });
    const outside = screen.getByRole("button", { name: "Outside" });
    act(() => outside.focus());
    expect(trigger.getAttribute("aria-haspopup")).toBe("dialog");
    expect(trigger.getAttribute("aria-expanded")).toBe("false");
    expect(screen.queryByRole("slider", { name: "Volume" })).toBeNull();
    expect(screen.queryByRole("dialog", { name: "Volume" })).toBeNull();

    fireEvent.pointerEnter(trigger, { pointerType: "mouse" });
    expect(trigger.getAttribute("aria-expanded")).toBe("true");
    const dialog = screen.getByRole("dialog", { name: "Volume" });
    const slider = screen.getByRole("slider", { name: "Volume" }) as HTMLInputElement;
    expect(dialog.textContent).toBe("");
    expect(dialog.querySelector("button")).toBeNull();
    expect(slider.getAttribute("aria-orientation")).toBe("vertical");
    expect(slider.value).toBe("0.65");
    expect(document.activeElement).toBe(outside);
    expect(onVolumeChange).not.toHaveBeenCalled();
    expect(onToggleMute).not.toHaveBeenCalled();
  });

  it("changes the range level and toggles mute from the main speaker button", () => {
    const onVolumeChange = vi.fn();
    const onToggleMute = vi.fn();
    render(<VolumeFixture onVolumeChange={onVolumeChange} onToggleMute={onToggleMute} />);

    const trigger = screen.getByRole("button", { name: "Volume" });
    fireEvent.pointerEnter(trigger, { pointerType: "mouse" });
    const slider = screen.getByRole("slider", { name: "Volume" }) as HTMLInputElement;
    fireEvent.change(slider, { target: { value: "0.37" } });
    expect(onVolumeChange).toHaveBeenCalledWith(0.37);
    expect(slider.value).toBe("0.37");

    fireEvent.click(trigger);
    expect(onToggleMute).toHaveBeenCalledTimes(1);
    expect(trigger.getAttribute("aria-description")).toBe("Muted");
    expect(screen.getByRole("dialog", { name: "Volume" })).not.toBeNull();
    fireEvent.click(trigger);
    expect(onToggleMute).toHaveBeenCalledTimes(2);
    expect(slider.value).toBe("0.37");
    expect(onVolumeChange).toHaveBeenCalledTimes(1);
  });

  it("opens on keyboard focus and restores focus after Escape without reopening", async () => {
    const onOpenChange = vi.fn();
    render(<><VolumeFixture onOpenChange={onOpenChange} /><button type="button">Outside</button></>);

    const trigger = screen.getByRole("button", { name: "Volume" });
    act(() => trigger.focus());
    expect(screen.getByRole("dialog", { name: "Volume" })).not.toBeNull();
    expect(document.activeElement).toBe(trigger);
    fireEvent.keyDown(trigger, { key: "ArrowDown" });
    await waitFor(() => expect(document.activeElement).toBe(screen.getByRole("slider", { name: "Volume" })));
    for (const key of ["ArrowUp", "ArrowDown"]) {
      const nativeStep = new KeyboardEvent("keydown", { key, bubbles: true, cancelable: true });
      fireEvent(screen.getByRole("slider", { name: "Volume" }), nativeStep);
      expect(nativeStep.defaultPrevented).toBe(false);
    }
    fireEvent.keyDown(screen.getByRole("dialog", { name: "Volume" }), { key: "Escape" });
    expect(screen.queryByRole("dialog", { name: "Volume" })).toBeNull();
    expect(document.activeElement).toBe(trigger);
    expect(onOpenChange).toHaveBeenLastCalledWith(false);

    fireEvent.keyDown(trigger, { key: "ArrowUp" });
    await waitFor(() => expect(document.activeElement).toBe(screen.getByRole("slider", { name: "Volume" })));
    fireEvent.pointerDown(screen.getByRole("button", { name: "Outside" }));
    await waitFor(() => expect(screen.queryByRole("dialog", { name: "Volume" })).toBeNull());

    fireEvent.keyDown(trigger, { key: "ArrowDown" });
    await waitFor(() => expect(document.activeElement).toBe(screen.getByRole("slider", { name: "Volume" })));
    act(() => screen.getByRole("button", { name: "Outside" }).focus());
    await waitFor(() => expect(screen.queryByRole("dialog", { name: "Volume" })).toBeNull());
    expect(onOpenChange).toHaveBeenLastCalledWith(false);
  });

  it("allows the pointer to cross the portal gap and closes after leaving both controls", () => {
    vi.useFakeTimers();
    try {
      render(<VolumeFixture />);
      const trigger = screen.getByRole("button", { name: "Volume" });
      fireEvent.pointerEnter(trigger, { pointerType: "mouse" });
      const dialog = screen.getByRole("dialog", { name: "Volume" });

      fireEvent.pointerLeave(trigger, { pointerType: "mouse", relatedTarget: document.body });
      act(() => vi.advanceTimersByTime(50));
      expect(screen.getByRole("dialog", { name: "Volume" })).toBe(dialog);
      fireEvent.pointerEnter(dialog, { pointerType: "mouse" });
      act(() => vi.advanceTimersByTime(200));
      expect(screen.getByRole("dialog", { name: "Volume" })).toBe(dialog);

      fireEvent.pointerLeave(dialog, { pointerType: "mouse", relatedTarget: document.body });
      act(() => vi.advanceTimersByTime(200));
      expect(screen.queryByRole("dialog", { name: "Volume" })).toBeNull();
    } finally {
      vi.useRealTimers();
    }
  });

  it("dismisses a hover-open volume with Escape from another focused menu without reopening", () => {
    render(<PlayerMenuProvider onOpenChange={() => undefined}>
      <VolumeFixture />
      <PlayerControlMenu label="Quality" icon="quality" value="auto" options={[{ value: "auto", label: "Auto" }]} onSelect={() => undefined} />
    </PlayerMenuProvider>);

    const quality = screen.getByRole("button", { name: "Quality: Auto" });
    const volume = screen.getByRole("button", { name: "Volume" });
    act(() => quality.focus());
    fireEvent.pointerEnter(volume, { pointerType: "mouse" });
    expect(screen.getByRole("dialog", { name: "Volume" })).not.toBeNull();
    expect(document.activeElement).toBe(quality);

    fireEvent.keyDown(quality, { key: "Escape" });
    expect(screen.queryByRole("dialog", { name: "Volume" })).toBeNull();
    expect(volume.getAttribute("aria-expanded")).toBe("false");
    expect(document.activeElement).toBe(volume);
  });

  it("retains the popup while keyboard focus is on the speaker or range", () => {
    vi.useFakeTimers();
    try {
      render(<><VolumeFixture /><button type="button">Outside</button></>);
      const trigger = screen.getByRole("button", { name: "Volume" });
      act(() => trigger.focus());
      fireEvent.pointerLeave(trigger, { pointerType: "mouse", relatedTarget: document.body });
      act(() => vi.advanceTimersByTime(200));
      const dialog = screen.getByRole("dialog", { name: "Volume" });

      act(() => screen.getByRole("slider", { name: "Volume" }).focus());
      fireEvent.pointerLeave(dialog, { pointerType: "mouse", relatedTarget: document.body });
      act(() => vi.advanceTimersByTime(200));
      expect(screen.getByRole("dialog", { name: "Volume" })).toBe(dialog);

      act(() => screen.getByRole("button", { name: "Outside" }).focus());
      act(() => vi.advanceTimersByTime(200));
      expect(screen.queryByRole("dialog", { name: "Volume" })).toBeNull();
    } finally {
      vi.useRealTimers();
    }
  });

  it("keeps a drag active outside the popup until the pointer is released", () => {
    vi.useFakeTimers();
    try {
      render(<VolumeFixture />);
      const trigger = screen.getByRole("button", { name: "Volume" });
      fireEvent.pointerEnter(trigger, { pointerType: "mouse" });
      const dialog = screen.getByRole("dialog", { name: "Volume" });
      fireEvent.pointerDown(screen.getByRole("slider", { name: "Volume" }), { pointerId: 1, pointerType: "mouse", buttons: 1 });
      fireEvent.pointerLeave(trigger, { pointerType: "mouse", relatedTarget: document.body });
      fireEvent.pointerLeave(dialog, { pointerType: "mouse", relatedTarget: document.body });
      act(() => vi.advanceTimersByTime(200));
      expect(screen.getByRole("dialog", { name: "Volume" })).toBe(dialog);

      fireEvent.pointerUp(document.body, { pointerId: 1, pointerType: "mouse" });
      act(() => vi.advanceTimersByTime(200));
      expect(screen.queryByRole("dialog", { name: "Volume" })).toBeNull();
    } finally {
      vi.useRealTimers();
    }
  });

  it("shares the menu owner with quality and settings", () => {
    render(<PlayerMenuProvider onOpenChange={() => undefined}>
      <VolumeFixture />
      <PlayerControlMenu label="Quality" icon="quality" value="auto" options={[{ value: "auto", label: "Auto" }]} onSelect={() => undefined} />
      <PlayerSettings playbackRate={1} onRateChange={() => undefined} captions={false} captionScale={1} onCaptionScaleChange={() => undefined} subtitleOffset={0} onSubtitleOffsetChange={() => undefined} />
    </PlayerMenuProvider>);

    const volume = screen.getByRole("button", { name: "Volume" });
    const quality = screen.getByRole("button", { name: "Quality: Auto" });
    fireEvent.pointerEnter(volume, { pointerType: "mouse" });
    fireEvent.click(quality);
    expect(screen.queryByRole("dialog", { name: "Volume" })).toBeNull();
    expect(screen.getByRole("listbox", { name: "Quality" })).not.toBeNull();

    fireEvent.pointerEnter(volume, { pointerType: "mouse" });
    expect(screen.queryByRole("listbox", { name: "Quality" })).toBeNull();
    expect(screen.getByRole("dialog", { name: "Volume" })).not.toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "Player settings" }));
    expect(screen.queryByRole("dialog", { name: "Volume" })).toBeNull();
    expect(screen.getByRole("dialog", { name: "Player settings" })).not.toBeNull();
    expect(volume.getAttribute("aria-expanded")).toBe("false");
  });

  it("returns completed volume pointer actions to the player without changing keyboard focus behavior", async () => {
    const onVolumeChange = vi.fn();
    const onToggleMute = vi.fn();
    const view = render(<div data-player-root="true" tabIndex={-1}><VolumeFixture onVolumeChange={onVolumeChange} onToggleMute={onToggleMute} /></div>);
    const root = view.container.firstElementChild;
    const trigger = screen.getByRole("button", { name: "Volume" });
    fireEvent.pointerEnter(trigger, { pointerType: "mouse" });
    pointerClick(trigger);
    expect(onToggleMute).toHaveBeenCalledTimes(1);
    expect(document.activeElement).toBe(root);
    expect(screen.getByRole("dialog", { name: "Volume" })).not.toBeNull();

    const slider = screen.getByRole("slider", { name: "Volume" });
    fireEvent.pointerDown(slider, { pointerType: "mouse", pointerId: 1 });
    act(() => slider.focus());
    fireEvent.change(slider, { target: { value: "0.25" } });
    fireEvent.pointerUp(slider, { pointerType: "mouse", pointerId: 1 });
    expect(onVolumeChange).toHaveBeenCalledWith(0.25);
    expect(document.activeElement).toBe(root);

    act(() => trigger.focus());
    fireEvent.keyDown(trigger, { key: "ArrowDown" });
    await waitFor(() => expect(document.activeElement).toBe(screen.getByRole("slider", { name: "Volume" })));
    fireEvent.change(screen.getByRole("slider", { name: "Volume" }), { target: { value: "0.45" } });
    expect(document.activeElement).toBe(screen.getByRole("slider", { name: "Volume" }));
  });
});

describe("player settings pointer completion", () => {
  it.each([700, 180])("opens compact topbar Settings below its anchor within a %ipx player", async (height) => {
    const bounds = vi.spyOn(HTMLElement.prototype, "getBoundingClientRect").mockImplementation(function (this: HTMLElement) {
      if (this.dataset.playerRoot === "true") return new DOMRect(100, 50, 1200, height);
      if (this.classList.contains("player-topbar__settings")) return new DOMRect(1220, 70, 44, 44);
      return new DOMRect();
    });
    const width = vi.spyOn(HTMLElement.prototype, "clientWidth", "get").mockImplementation(function (this: HTMLElement) {
      return this.dataset.playerRoot === "true" ? 1200 : 0;
    });
    const frameHeight = vi.spyOn(HTMLElement.prototype, "clientHeight", "get").mockImplementation(function (this: HTMLElement) {
      return this.dataset.playerRoot === "true" ? height : 0;
    });
    const contentHeight = vi.spyOn(HTMLElement.prototype, "scrollHeight", "get").mockImplementation(function (this: HTMLElement) {
      return this.classList.contains("player-settings-panel") ? 420 : 0;
    });
    const view = render(<div data-player-root="true" tabIndex={-1}>
      <PlayerSettings compact className="player-topbar__settings" playbackRate={1} onRateChange={() => undefined}
        captions captionScale={1} onCaptionScaleChange={() => undefined} subtitleOffset={0} onSubtitleOffsetChange={() => undefined} />
    </div>);
    try {
      const trigger = screen.getByRole("button", { name: "Player settings" });
      expect(trigger.getAttribute("aria-haspopup")).toBe("dialog");
      expect(trigger.getAttribute("aria-expanded")).toBe("false");
      act(() => trigger.focus());
      fireEvent.keyDown(trigger, { key: "ArrowDown" });
      const dialog = screen.getByRole("dialog", { name: "Player settings" });
      await waitFor(() => expect(document.activeElement).toBe(screen.getByRole("button", { name: "0.5×" })));

      expect(trigger.getAttribute("aria-controls")).toBe(dialog.id);
      expect(trigger.getAttribute("aria-expanded")).toBe("true");
      expect(dialog.parentElement).toBe(view.container.firstElementChild);
      expect(Number.parseFloat(dialog.style.left) + Number.parseFloat(dialog.style.width)).toBe(1164);
      expect(Number.parseFloat(dialog.style.top)).toBe(72);
      expect(Number.parseFloat(dialog.style.maxHeight)).toBe(height - 84);
      expect(Number.parseFloat(dialog.style.top) + Math.min(420, Number.parseFloat(dialog.style.maxHeight))).toBeLessThanOrEqual(height - 12);
      expect(dialog.style.overflowY).toBe("auto");

      fireEvent.keyDown(dialog, { key: "Escape" });
      expect(screen.queryByRole("dialog", { name: "Player settings" })).toBeNull();
      expect(document.activeElement).toBe(trigger);
      expect(trigger.getAttribute("aria-expanded")).toBe("false");
    } finally {
      view.unmount();
      contentHeight.mockRestore();
      frameHeight.mockRestore();
      width.mockRestore();
      bounds.mockRestore();
    }
  });

  it.each([
    { label: "1.5×", kind: "rate", value: 1.5 },
    { label: "Large", kind: "caption", value: 1.25 },
    { label: "Reset timing", kind: "timing", value: 0 },
  ])("closes after pointer selection of $label and restores player focus", ({ label, kind, value }) => {
    const callbacks = { rate: vi.fn(), caption: vi.fn(), timing: vi.fn() };
    const view = render(<div data-player-root="true" tabIndex={-1}>
      <PlayerSettings playbackRate={1} onRateChange={callbacks.rate} captions captionScale={1} onCaptionScaleChange={callbacks.caption} subtitleOffset={0} onSubtitleOffsetChange={callbacks.timing} />
    </div>);
    pointerClick(screen.getByRole("button", { name: "Player settings" }));
    pointerClick(screen.getByRole("button", { name: label }));
    expect(callbacks[kind as keyof typeof callbacks]).toHaveBeenCalledWith(value);
    expect(screen.queryByRole("dialog", { name: "Player settings" })).toBeNull();
    expect(document.activeElement).toBe(view.container.firstElementChild);
  });

  it("keeps keyboard choices focused and restores focus only after timing pointer completion", () => {
    const onTimingChange = vi.fn();
    const view = render(<div data-player-root="true" tabIndex={-1}>
      <PlayerSettings playbackRate={1} onRateChange={() => undefined} captions captionScale={1} onCaptionScaleChange={() => undefined} subtitleOffset={0} onSubtitleOffsetChange={onTimingChange} />
    </div>);
    const trigger = screen.getByRole("button", { name: "Player settings" });
    act(() => trigger.focus());
    fireEvent.click(trigger, { detail: 0 });
    const rate = screen.getByRole("button", { name: "1.5×" });
    act(() => rate.focus());
    fireEvent.click(rate, { detail: 0 });
    expect(screen.getByRole("dialog", { name: "Player settings" })).not.toBeNull();
    expect(document.activeElement).toBe(rate);

    const timing = screen.getByRole("slider", { name: "Subtitle timing" });
    act(() => timing.focus());
    fireEvent.change(timing, { target: { value: "0.25" } });
    expect(document.activeElement).toBe(timing);
    expect(screen.getByRole("dialog", { name: "Player settings" })).not.toBeNull();
    fireEvent.pointerDown(timing, { pointerType: "mouse", pointerId: 1 });
    fireEvent.change(timing, { target: { value: "0.5" } });
    fireEvent.pointerUp(timing, { pointerType: "mouse", pointerId: 1 });
    expect(onTimingChange).toHaveBeenLastCalledWith(0.5);
    expect(screen.queryByRole("dialog", { name: "Player settings" })).toBeNull();
    expect(document.activeElement).toBe(view.container.firstElementChild);
  });
});

describe("player control menu", () => {
  it("keeps one menu owner and releases an open control when it unmounts", () => {
    const owner=vi.fn();
    const fixture=(quality:boolean) => <PlayerMenuProvider onOpenChange={owner}>
      {quality && <PlayerControlMenu label="Quality" icon="quality" value="original" options={[{value:"original",label:"1080p · Original",shortLabel:"1080p"}]} status="Switching · Playing 720p" summary="Switching · Playing 720p" onSelect={() => undefined} />}
      <PlayerSettings playbackRate={1} onRateChange={() => undefined} captions={false} captionScale={1} onCaptionScaleChange={() => undefined} subtitleOffset={0} onSubtitleOffsetChange={() => undefined} />
    </PlayerMenuProvider>;
    const view=render(fixture(true));
    fireEvent.click(screen.getByRole("button",{name:"Quality: 1080p · Original"}));
    expect(screen.getByRole("status").textContent).toBe("Switching · Playing 720p");
    fireEvent.click(screen.getByRole("button",{name:"Player settings"}));
    expect(screen.queryByRole("listbox")).toBeNull();
    expect(screen.queryByRole("status")).toBeNull();
    expect(screen.getByRole("dialog",{name:"Player settings"})).toBeTruthy();
    fireEvent.click(screen.getByRole("button",{name:"Quality: 1080p · Original"}));
    view.rerender(fixture(false));
    expect(owner).toHaveBeenLastCalledWith(false);
  });
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
        status="Switching · Playing 480p"
        summary="Switching · Playing 480p"
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
    expect(screen.getByRole("status").textContent).toBe("Switching · Playing 480p");
    fireEvent.keyDown(listbox, { key: "End" });
    expect(document.activeElement).toBe(screen.getByRole("option", { name: "480p" }));

    fireEvent.keyDown(listbox, { key: "Escape" });
    expect(document.activeElement).toBe(trigger);
    expect(screen.queryByRole("listbox", { name: "Quality" })).toBeNull();
    expect(screen.queryByRole("status")).toBeNull();
    expect(onOpenChange).toHaveBeenCalledWith(true);
    expect(onOpenChange).toHaveBeenCalledWith(false);
  });

  it("restores player focus after a pointer choice and trigger focus after a keyboard choice", async () => {
    const view = render(<div data-player-root="true" tabIndex={-1}><QualityFixture /></div>);
    pointerClick(screen.getByRole("button", { name: "Quality: Auto" }));
    pointerClick(screen.getByRole("option", { name: "480p" }));
    expect(document.activeElement).toBe(view.container.firstElementChild);

    const trigger = screen.getByRole("button", { name: "Quality: 480p" });
    act(() => trigger.focus());
    fireEvent.keyDown(trigger, { key: "ArrowDown" });
    await waitFor(() => expect(document.activeElement).toBe(screen.getByRole("option", { name: "480p" })));
    fireEvent.click(screen.getByRole("option", { name: "720p" }), { detail: 0 });
    expect(screen.queryByRole("listbox", { name: "Quality" })).toBeNull();
    expect(document.activeElement).toBe(trigger);
    expect(screen.getByRole("button", { name: "Quality: 720p" })).toBe(trigger);
  });

  it("keeps the selected quality compact without losing its accessible name or status", () => {
    render(
      <PlayerControlMenu
        label="Quality"
        icon="quality"
        value="original"
        options={[{ value: "original", label: "1080p · Original", shortLabel: "1080p" }]}
        status="Playing 1080p · Original"
        onSelect={() => undefined}
      />,
    );

    const trigger = screen.getByRole("button", { name: "Quality: 1080p · Original" });
    expect(trigger.textContent).toBe("1080p");
    expect(trigger.getAttribute("aria-description")).toBe("Playing 1080p · Original");
    expect(screen.queryByText("Playing 1080p · Original")).toBeNull();
    expect(screen.queryByRole("status")).toBeNull();

    fireEvent.click(trigger);
    expect(screen.getByRole("option", { name: "1080p · Original" }).getAttribute("aria-selected")).toBe("true");
    expect(screen.queryByRole("status")).toBeNull();
  });

  it("shows the optional transition summary only while its menu is open", () => {
    const onSelect = vi.fn();
    const fixture = (summary?: string) => (
      <PlayerControlMenu
        label="Quality"
        icon="quality"
        value="original"
        options={[
          { value: "original", label: "1080p · Original", shortLabel: "1080p" },
          { value: "720", label: "720p" },
        ]}
        status="Switching · Playing 720p"
        summary={summary}
        onSelect={onSelect}
      />
    );
    const view = render(fixture("Switching to 1080p · Playing 720p"));
    const trigger = screen.getByRole("button", { name: "Quality: 1080p · Original" });
    expect(screen.queryByRole("status")).toBeNull();

    fireEvent.click(trigger);
    expect(screen.getByRole("status").textContent).toBe("Switching to 1080p · Playing 720p");
    expect(screen.queryByText("Switching · Playing 720p")).toBeNull();
    fireEvent.click(screen.getByRole("option", { name: "720p" }));
    expect(onSelect).toHaveBeenCalledWith("720");
    expect(screen.queryByRole("status")).toBeNull();
    expect(document.activeElement).toBe(trigger);

    view.rerender(fixture());
    fireEvent.click(trigger);
    expect(screen.getByRole("listbox", { name: "Quality" })).not.toBeNull();
    expect(screen.queryByRole("status")).toBeNull();
  });

  it("allows a pending quality to be requested and prioritized", () => {
    const onSelect = vi.fn();
    render(
      <PlayerControlMenu
        label="Quality"
        icon="quality"
        value={-1}
        options={[
          { value: -1, label: "Auto" },
          { value: 144, label: "144p", status: "Preparing" },
        ]}
        onSelect={onSelect}
      />,
    );

    fireEvent.click(screen.getByRole("button", { name: "Quality: Auto" }));
    const pending = screen.getByRole("option", { name: /144p Preparing/ });
    expect(pending.getAttribute("aria-disabled")).toBeNull();
    fireEvent.click(pending);
    expect(onSelect).toHaveBeenCalledWith(144);
  });

  it("does not reopen the player chrome when a closed menu unmounts", () => {
    const onOpenChange = vi.fn();
    const view = render(<QualityFixture onOpenChange={onOpenChange} />);

    view.unmount();
    expect(onOpenChange).not.toHaveBeenCalled();
  });
});
