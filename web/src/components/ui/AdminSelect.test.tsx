import React, { useState } from "react";
import { act, cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { AdminSelect, type AdminSelectOption } from "./AdminSelect";

const options: AdminSelectOption[] = [
  { value: "local", label: "Local disk" },
  { value: "drive", label: "Google Drive (not configured)", disabled: true },
  { value: "balanced", label: "Balanced" },
  { value: "best", label: "Best quality" },
  { value: "compact", label: "Compact" },
];

function Controlled({ onChange = () => undefined }: { onChange?: (value: string) => void }) {
  const [value, setValue] = useState("local");
  return <div data-theme="ember"><AdminSelect id="storage" label="Storage engine" options={options} value={value}
    onChange={(next) => { onChange(next); setValue(next); }} /><button>Next field</button></div>;
}

function activeLabel(control = screen.getByRole("combobox", { name: "Storage engine" })) {
  return document.getElementById(control.getAttribute("aria-activedescendant") || "")?.textContent;
}

describe("AdminSelect", () => {
  beforeEach(() => {
    Object.defineProperty(window, "innerWidth", { configurable: true, value: 1024 });
    Object.defineProperty(window, "innerHeight", { configurable: true, value: 768 });
  });
  afterEach(() => { cleanup(); vi.restoreAllMocks(); vi.useRealTimers(); });

  it("labels its controlled value and portals themed options outside clipped containers", () => {
    const view = render(<div style={{ overflow: "hidden" }} data-theme="aurora"><p id="help">Choose the actual storage destination.</p>
      <AdminSelect id="engine" label="Storage engine" name="storage_engine" aria-describedby="help"
        options={options} value="local" onChange={vi.fn()} /></div>);
    const control = screen.getByRole("combobox", { name: "Storage engine" });
    expect(control.id).toBe("engine");
    expect(control.getAttribute("aria-describedby")).toBe("help");
    expect(control.textContent).toBe("Local disk");
    expect((view.container.querySelector('input[name="storage_engine"]') as HTMLInputElement).value).toBe("local");
    control.focus();
    fireEvent.click(control);
    const list = screen.getByRole("listbox", { name: "Storage engine" });
    expect(list.parentElement).toBe(document.body);
    expect(list.getAttribute("data-theme")).toBe("aurora");
    expect(control.getAttribute("aria-controls")).toBe(list.id);
    expect(document.activeElement).toBe(control);
    expect(screen.getByRole("option", { name: "Local disk", selected: true })).toBeTruthy();
    expect(screen.getByRole("option", { name: "Google Drive (not configured)" }).getAttribute("aria-disabled")).toBe("true");
  });

  it("lets an explicit accessible name clarify a short visible label and honors aria-labelledby first", () => {
    const view = render(<AdminSelect label="Expiration" aria-label="API key expiration" options={options} value="local" onChange={vi.fn()} />);
    fireEvent.click(screen.getByRole("combobox", { name: "API key expiration" }));
    expect(screen.getByRole("listbox", { name: "API key expiration" })).toBeTruthy();
    view.rerender(<><span id="context">Integration expiration</span><AdminSelect label="Expiration" aria-label="API key expiration"
      aria-labelledby="context" options={options} value="local" onChange={vi.fn()} /></>);
    fireEvent.click(screen.getByRole("combobox", { name: "Integration expiration" }));
    expect(screen.getByRole("listbox", { name: "Integration expiration" })).toBeTruthy();
  });

  it("previews enabled options with arrows, commits Enter, and does not submit its surrounding form", () => {
    const onChange = vi.fn();
    const submit = vi.fn((event: React.FormEvent) => event.preventDefault());
    render(<form onSubmit={submit}><Controlled onChange={onChange} /></form>);
    const control = screen.getByRole("combobox", { name: "Storage engine" });
    control.focus();
    fireEvent.keyDown(control, { key: "ArrowDown" });
    fireEvent.keyDown(control, { key: "ArrowDown" });
    expect(activeLabel()).toBe("Balanced");
    expect(onChange).not.toHaveBeenCalled();
    expect(screen.getByRole("option", { name: "Local disk", selected: true })).toBeTruthy();
    fireEvent.keyDown(control, { key: "Enter" });
    expect(onChange).toHaveBeenCalledExactlyOnceWith("balanced");
    expect(control.textContent).toBe("Balanced");
    expect(screen.queryByRole("listbox")).toBeNull();
    expect(submit).not.toHaveBeenCalled();
    expect(document.activeElement).toBe(control);
  });

  it("supports Home/End and Space while Escape cancels a preview without changing selection", () => {
    const onChange = vi.fn();
    render(<Controlled onChange={onChange} />);
    const control = screen.getByRole("combobox", { name: "Storage engine" });
    control.focus();
    fireEvent.keyDown(control, { key: "End" });
    expect(activeLabel()).toBe("Compact");
    fireEvent.keyDown(control, { key: "Home" });
    expect(activeLabel()).toBe("Local disk✓");
    fireEvent.keyDown(control, { key: "End" });
    fireEvent.keyDown(control, { key: "Escape" });
    expect(screen.queryByRole("listbox")).toBeNull();
    expect(onChange).not.toHaveBeenCalled();
    fireEvent.keyDown(control, { key: " " });
    fireEvent.keyDown(control, { key: "End" });
    fireEvent.keyDown(control, { key: " " });
    expect(onChange).toHaveBeenCalledExactlyOnceWith("compact");
  });

  it("opens with Alt+Down and commits the active option with Alt+Up", () => {
    const onChange = vi.fn();
    render(<Controlled onChange={onChange} />);
    const control = screen.getByRole("combobox", { name: "Storage engine" });
    fireEvent.keyDown(control, { key: "ArrowDown", altKey: true });
    expect(activeLabel()).toBe("Local disk✓");
    fireEvent.keyDown(control, { key: "ArrowDown", altKey: true });
    expect(activeLabel()).toBe("Local disk✓");
    fireEvent.keyDown(control, { key: "ArrowDown" });
    fireEvent.keyDown(control, { key: "ArrowUp", altKey: true });
    expect(onChange).toHaveBeenCalledExactlyOnceWith("balanced");
    expect(screen.queryByRole("listbox")).toBeNull();
  });

  it("supports accumulated typeahead, repeated-character cycling, and an expired search buffer", () => {
    vi.useFakeTimers();
    vi.setSystemTime(new Date("2026-10-12T12:00:00Z"));
    const onChange = vi.fn();
    render(<Controlled onChange={onChange} />);
    const control = screen.getByRole("combobox", { name: "Storage engine" });
    fireEvent.keyDown(control, { key: "b" });
    expect(activeLabel()).toBe("Balanced");
    fireEvent.keyDown(control, { key: "b" });
    expect(activeLabel()).toBe("Best quality");
    vi.setSystemTime(new Date("2026-10-12T12:00:01Z"));
    fireEvent.keyDown(control, { key: "b" });
    fireEvent.keyDown(control, { key: "e" });
    expect(activeLabel()).toBe("Best quality");
    vi.setSystemTime(new Date("2026-10-12T12:00:02Z"));
    fireEvent.keyDown(control, { key: " " });
    expect(onChange).toHaveBeenCalledExactlyOnceWith("best");
  });

  it("commits the active enabled option on Tab without preventing focus from leaving", () => {
    const onChange = vi.fn();
    render(<Controlled onChange={onChange} />);
    const control = screen.getByRole("combobox", { name: "Storage engine" });
    control.focus();
    fireEvent.keyDown(control, { key: "End" });
    expect(fireEvent.keyDown(control, { key: "Tab" })).toBe(true);
    expect(onChange).toHaveBeenCalledExactlyOnceWith("compact");
    act(() => screen.getByRole("button", { name: "Next field" }).focus());
    expect(document.activeElement?.textContent).toBe("Next field");
    expect(screen.queryByRole("listbox")).toBeNull();
  });

  it("ignores disabled pointer options and preserves focus when selecting an enabled one", () => {
    const onChange = vi.fn();
    render(<Controlled onChange={onChange} />);
    const control = screen.getByRole("combobox", { name: "Storage engine" });
    control.focus();
    fireEvent.click(control);
    fireEvent.pointerMove(screen.getByRole("option", { name: "Google Drive (not configured)" }));
    fireEvent.click(screen.getByRole("option", { name: "Google Drive (not configured)" }));
    expect(onChange).not.toHaveBeenCalled();
    expect(screen.getByRole("listbox")).toBeTruthy();
    fireEvent.pointerMove(screen.getByRole("option", { name: "Best quality" }));
    expect(activeLabel()).toBe("Best quality");
    fireEvent.click(screen.getByRole("option", { name: "Best quality" }));
    expect(onChange).toHaveBeenCalledExactlyOnceWith("best");
    expect(document.activeElement).toBe(control);
  });

  it("dismisses outside pointer and focus changes without committing a preview", () => {
    const onChange = vi.fn();
    render(<Controlled onChange={onChange} />);
    const control = screen.getByRole("combobox", { name: "Storage engine" });
    control.focus();
    fireEvent.keyDown(control, { key: "End" });
    fireEvent.pointerDown(screen.getByRole("button", { name: "Next field" }));
    expect(screen.queryByRole("listbox")).toBeNull();
    fireEvent.keyDown(control, { key: "End" });
    act(() => screen.getByRole("button", { name: "Next field" }).focus());
    expect(screen.queryByRole("listbox")).toBeNull();
    expect(onChange).not.toHaveBeenCalled();
  });

  it("closes immediately if the parent disables the control and cannot reopen until enabled", () => {
    const onChange = vi.fn();
    const view = render(<AdminSelect options={options} value="local" onChange={onChange} aria-label="Destination" />);
    const control = screen.getByRole("combobox", { name: "Destination" });
    fireEvent.click(control);
    view.rerender(<AdminSelect options={options} value="local" onChange={onChange} aria-label="Destination" disabled />);
    expect(screen.queryByRole("listbox")).toBeNull();
    expect((control as HTMLButtonElement).disabled).toBe(true);
    fireEvent.keyDown(control, { key: "End" });
    fireEvent.click(control);
    expect(screen.queryByRole("listbox")).toBeNull();
    expect(onChange).not.toHaveBeenCalled();
    view.rerender(<AdminSelect options={options} value="local" onChange={onChange} aria-label="Destination" />);
    fireEvent.keyDown(control, { key: "ArrowDown" });
    expect(screen.getByRole("listbox")).toBeTruthy();
  });

  it("reconciles a removed or disabled active option without selecting unsupported data", () => {
    const onChange = vi.fn();
    const view = render(<AdminSelect options={options} value="local" onChange={onChange} label="Storage engine" />);
    const control = screen.getByRole("combobox", { name: "Storage engine" });
    fireEvent.keyDown(control, { key: "End" });
    view.rerender(<AdminSelect options={options.slice(0, 3)} value="local" onChange={onChange} label="Storage engine" />);
    expect(activeLabel()).toBe("Local disk✓");
    fireEvent.keyDown(control, { key: "Enter" });
    expect(onChange).not.toHaveBeenCalled();
    view.rerender(<AdminSelect options={options.map((option) => ({ ...option, disabled: true }))} value="local" onChange={onChange} label="Storage engine" />);
    expect((control as HTMLButtonElement).disabled).toBe(true);
  });

  it("flips above a low anchor, clamps narrow viewports, and follows resize and scroll", () => {
    const rect = { x: 280, y: 245, left: 280, top: 245, right: 480, bottom: 289, width: 200, height: 44, toJSON() {} };
    vi.spyOn(HTMLElement.prototype, "getBoundingClientRect").mockImplementation(function (this: HTMLElement) {
      return this.tagName === "BUTTON" ? rect : { ...rect, width: 200, height: 220 };
    });
    Object.defineProperty(window, "innerWidth", { configurable: true, value: 320 });
    Object.defineProperty(window, "innerHeight", { configurable: true, value: 320 });
    render(<Controlled />);
    fireEvent.click(screen.getByRole("combobox", { name: "Storage engine" }));
    const list = screen.getByRole("listbox");
    expect(list.style.left).toBe("112px");
    expect(Number.parseFloat(list.style.top)).toBeLessThan(rect.top);
    expect(Number.parseFloat(list.style.top) + Number.parseFloat(list.style.maxHeight)).toBeLessThanOrEqual(312);
    rect.left = 10; rect.right = 210; rect.top = 20; rect.bottom = 64;
    fireEvent.resize(window);
    expect(list.style.left).toBe("10px");
    expect(list.style.top).toBe("70px");
    rect.top = -90; rect.bottom = -46;
    fireEvent.scroll(document);
    expect(screen.queryByRole("listbox")).toBeNull();
  });

  it("removes its portal and observers on unmount", () => {
    const disconnect = vi.fn();
    vi.stubGlobal("ResizeObserver", class { observe() {} disconnect = disconnect; });
    const view = render(<Controlled />);
    fireEvent.click(screen.getByRole("combobox", { name: "Storage engine" }));
    expect(screen.getByRole("listbox")).toBeTruthy();
    view.unmount();
    expect(screen.queryByRole("listbox")).toBeNull();
    expect(disconnect).toHaveBeenCalled();
    vi.unstubAllGlobals();
  });
});
