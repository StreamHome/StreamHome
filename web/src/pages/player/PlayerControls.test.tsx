import React, { useState } from "react";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import { hasSubtitleOptions, PlayerControlMenu, PlayerIconButton, PlayerMenuProvider, PlayerSettings, PlayerVolume } from "./PlayerControls";

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

  it("keeps the slider hidden until requested without changing volume or mute", () => {
    const onVolumeChange = vi.fn();
    const onToggleMute = vi.fn();
    render(<VolumeFixture onVolumeChange={onVolumeChange} onToggleMute={onToggleMute} />);

    const trigger = screen.getByRole("button", { name: "Volume" });
    expect(trigger.getAttribute("aria-haspopup")).toBe("dialog");
    expect(trigger.getAttribute("aria-expanded")).toBe("false");
    expect(screen.queryByRole("slider", { name: "Volume" })).toBeNull();
    expect(screen.queryByRole("dialog", { name: "Volume" })).toBeNull();

    fireEvent.click(trigger);
    expect(trigger.getAttribute("aria-expanded")).toBe("true");
    expect(screen.getByRole("dialog", { name: "Volume" })).not.toBeNull();
    expect((screen.getByRole("slider", { name: "Volume" }) as HTMLInputElement).value).toBe("0.65");
    expect(onVolumeChange).not.toHaveBeenCalled();
    expect(onToggleMute).not.toHaveBeenCalled();

    fireEvent.click(trigger);
    expect(screen.queryByRole("slider", { name: "Volume" })).toBeNull();
    expect(onVolumeChange).not.toHaveBeenCalled();
    expect(onToggleMute).not.toHaveBeenCalled();
  });

  it("changes volume and toggles mute through the open dialog", () => {
    const onVolumeChange = vi.fn();
    const onToggleMute = vi.fn();
    render(<VolumeFixture onVolumeChange={onVolumeChange} onToggleMute={onToggleMute} />);

    fireEvent.click(screen.getByRole("button", { name: "Volume" }));
    const slider = screen.getByRole("slider", { name: "Volume" }) as HTMLInputElement;
    fireEvent.change(slider, { target: { value: "0.37" } });
    expect(onVolumeChange).toHaveBeenCalledWith(0.37);
    expect(slider.value).toBe("0.37");

    fireEvent.click(screen.getByRole("button", { name: "Mute" }));
    expect(onToggleMute).toHaveBeenCalledTimes(1);
    expect(screen.getByRole("dialog", { name: "Volume" })).not.toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "Unmute" }));
    expect(onToggleMute).toHaveBeenCalledTimes(2);
    expect(slider.value).toBe("0.37");
    expect(onVolumeChange).toHaveBeenCalledTimes(1);
  });

  it("supports keyboard focus, Escape, outside dismissal, and focus dismissal", async () => {
    const onOpenChange = vi.fn();
    render(<><VolumeFixture onOpenChange={onOpenChange} /><button type="button">Outside</button></>);

    const trigger = screen.getByRole("button", { name: "Volume" });
    trigger.focus();
    fireEvent.keyDown(trigger, { key: "ArrowDown" });
    await waitFor(() => expect(document.activeElement).toBe(screen.getByRole("slider", { name: "Volume" })));
    fireEvent.keyDown(screen.getByRole("dialog", { name: "Volume" }), { key: "Escape" });
    expect(screen.queryByRole("dialog", { name: "Volume" })).toBeNull();
    expect(document.activeElement).toBe(trigger);
    expect(onOpenChange).toHaveBeenLastCalledWith(false);

    fireEvent.click(trigger);
    fireEvent.pointerDown(screen.getByRole("button", { name: "Outside" }));
    expect(screen.queryByRole("dialog", { name: "Volume" })).toBeNull();

    fireEvent.keyDown(trigger, { key: "ArrowDown" });
    await waitFor(() => expect(document.activeElement).toBe(screen.getByRole("slider", { name: "Volume" })));
    fireEvent.blur(screen.getByRole("slider", { name: "Volume" }), { relatedTarget: screen.getByRole("button", { name: "Outside" }) });
    expect(screen.queryByRole("dialog", { name: "Volume" })).toBeNull();
    expect(onOpenChange).toHaveBeenLastCalledWith(false);
  });

  it("shares the menu owner with quality and settings", () => {
    render(<PlayerMenuProvider onOpenChange={() => undefined}>
      <VolumeFixture />
      <PlayerControlMenu label="Quality" icon="quality" value="auto" options={[{ value: "auto", label: "Auto" }]} onSelect={() => undefined} />
      <PlayerSettings playbackRate={1} onRateChange={() => undefined} captions={false} captionScale={1} onCaptionScaleChange={() => undefined} subtitleOffset={0} onSubtitleOffsetChange={() => undefined} />
    </PlayerMenuProvider>);

    const volume = screen.getByRole("button", { name: "Volume" });
    const quality = screen.getByRole("button", { name: "Quality: Auto" });
    fireEvent.click(volume);
    fireEvent.click(quality);
    expect(screen.queryByRole("dialog", { name: "Volume" })).toBeNull();
    expect(screen.getByRole("listbox", { name: "Quality" })).not.toBeNull();

    fireEvent.click(volume);
    expect(screen.queryByRole("listbox", { name: "Quality" })).toBeNull();
    expect(screen.getByRole("dialog", { name: "Volume" })).not.toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "Player settings" }));
    expect(screen.queryByRole("dialog", { name: "Volume" })).toBeNull();
    expect(screen.getByRole("dialog", { name: "Player settings" })).not.toBeNull();
    expect(volume.getAttribute("aria-expanded")).toBe("false");
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
