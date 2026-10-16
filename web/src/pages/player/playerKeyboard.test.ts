import { afterEach, describe, expect, it } from "vitest";

import { playerControlOwnsKey, restorePlayerPointerFocus } from "./playerKeyboard";

const navigationKeys = ["ArrowLeft", "ArrowRight", "ArrowUp", "ArrowDown", "Home", "End", "PageUp", "PageDown"];
const playbackKeys = [" ", "k", "f", "m", "p"];

function keyEvent(target: Element, key: string, init: KeyboardEventInit = {}) {
  const event = new KeyboardEvent("keydown", { key, bubbles: true, cancelable: true, ...init });
  Object.defineProperty(event, "target", { value: target });
  return event;
}

describe("player keyboard ownership", () => {
  it("leaves every key with Settings while a group is being edited", () => {
    const dialog = document.createElement("div");
    dialog.dataset.playerSettingsDialog = "true";
    const range = dialog.appendChild(document.createElement("input"));
    range.type = "range";
    for (const key of [...navigationKeys, ...playbackKeys, "Enter", "Escape", "Tab"]) {
      expect(playerControlOwnsKey(keyEvent(range, key))).toBe(true);
    }
  });
  it.each(["input", "aria slider"])("keeps native %s navigation while allowing unrelated playback keys", (kind) => {
    const target = document.createElement(kind === "input" ? "input" : "div");
    if (target instanceof HTMLInputElement) target.type = "range";
    else target.setAttribute("role", "slider");
    for (const key of navigationKeys) expect(playerControlOwnsKey(keyEvent(target, key))).toBe(true);
    for (const key of playbackKeys) expect(playerControlOwnsKey(keyEvent(target, key))).toBe(false);
  });

  it.each(["button", "aria button"])("reserves native %s activation without swallowing player seek keys", (kind) => {
    const target = document.createElement(kind === "button" ? "button" : "div");
    if (kind !== "button") target.setAttribute("role", "button");
    for (const key of [" ", "Enter"]) expect(playerControlOwnsKey(keyEvent(target, key))).toBe(true);
    for (const key of [...navigationKeys, "k", "f", "m", "p"]) expect(playerControlOwnsKey(keyEvent(target, key))).toBe(false);

    target.setAttribute("aria-haspopup", "listbox");
    for (const key of ["ArrowUp", "ArrowDown"]) expect(playerControlOwnsKey(keyEvent(target, key))).toBe(true);
    for (const key of ["ArrowLeft", "ArrowRight"]) expect(playerControlOwnsKey(keyEvent(target, key))).toBe(false);
  });

  it.each(["listbox", "menu", "option", "menuitem"])("preserves %s navigation, activation, and dismissal", (role) => {
    const target = document.createElement("div");
    target.setAttribute("role", role);
    const child = target.appendChild(document.createElement("span"));
    for (const key of [...navigationKeys, " ", "Enter", "Escape"]) expect(playerControlOwnsKey(keyEvent(child, key))).toBe(true);
    for (const key of ["k", "f", "m", "p"]) expect(playerControlOwnsKey(keyEvent(child, key))).toBe(false);
  });

  it.each(["input", "textarea", "select", "a", "textbox", "editable", "plaintext-only"])("leaves %s editing and navigation untouched", (kind) => {
    const target = document.createElement(["textbox", "editable", "plaintext-only"].includes(kind) ? "div" : kind);
    if (kind === "textbox") target.setAttribute("role", "textbox");
    if (kind === "editable") target.setAttribute("contenteditable", "");
    if (kind === "plaintext-only") target.setAttribute("contenteditable", "plaintext-only");
    const child = kind === "editable" || kind === "plaintext-only" ? target.appendChild(document.createElement("span")) : target;
    for (const key of [...navigationKeys, ...playbackKeys, "Enter"]) expect(playerControlOwnsKey(keyEvent(child, key))).toBe(true);
  });

  it("preserves modifier, composition, and previously handled events", () => {
    const target = document.createElement("div");
    for (const init of [{ ctrlKey: true }, { metaKey: true }, { altKey: true }, { isComposing: true }]) {
      expect(playerControlOwnsKey(keyEvent(target, "k", init))).toBe(true);
    }
    const prevented = keyEvent(target, "k");
    prevented.preventDefault();
    expect(playerControlOwnsKey(prevented)).toBe(true);
    expect(playerControlOwnsKey(keyEvent(target, "k"))).toBe(false);
  });
});

describe("completed player pointer focus", () => {
  afterEach(() => document.body.replaceChildren());

  it("returns owned pointer focus to the connected player", () => {
    const root = document.createElement("div");
    root.dataset.playerRoot = "true";
    root.tabIndex = -1;
    const button = root.appendChild(document.createElement("button"));
    document.body.append(root);
    button.focus();
    restorePlayerPointerFocus(button);
    expect(document.activeElement).toBe(root);
  });

  it("does not steal focus that an action moved to another control", () => {
    const root = document.createElement("div");
    root.dataset.playerRoot = "true";
    root.tabIndex = -1;
    const first = root.appendChild(document.createElement("button"));
    const next = root.appendChild(document.createElement("button"));
    document.body.append(root);
    next.focus();
    restorePlayerPointerFocus(first);
    expect(document.activeElement).toBe(next);
    root.remove();
    restorePlayerPointerFocus(first);
    expect(document.activeElement).toBe(document.body);
  });
});
