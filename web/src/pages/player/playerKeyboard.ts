import type React from "react";

const navigationKeys = new Set(["ArrowLeft", "ArrowRight", "ArrowUp", "ArrowDown", "Home", "End", "PageUp", "PageDown"]);
const activationKeys = new Set([" ", "Enter"]);

/** Let the focused widget keep the keys it actually uses. */
export function playerControlOwnsKey(event: KeyboardEvent): boolean {
  if (event.defaultPrevented || event.isComposing || event.ctrlKey || event.metaKey || event.altKey) return true;
  const target = event.target;
  if (!(target instanceof Element)) return false;
  if (target.closest("[data-player-settings-dialog]")) return true;
  if (target.closest("textarea, select, a, [role='textbox']")) return true;
  const editable = target.closest<HTMLElement>("[contenteditable]");
  if (editable && editable.getAttribute("contenteditable") !== "false") return true;
  const input = target.closest<HTMLInputElement>("input");
  if (input && input.type !== "range") return true;
  if (input?.type === "range" || target.closest("[role='slider']")) return navigationKeys.has(event.key);
  if (target.closest("[role='listbox'], [role='menu'], [role='option'], [role='menuitem']")) {
    return navigationKeys.has(event.key) || activationKeys.has(event.key) || event.key === "Escape";
  }
  const button = target.closest("button, [role='button']");
  return Boolean(button && (activationKeys.has(event.key)
    || (button.hasAttribute("aria-haspopup") && (event.key === "ArrowUp" || event.key === "ArrowDown"))));
}

/** Release completed pointer interactions without stealing another control's focus. */
export function restorePlayerPointerFocus(target: HTMLElement | null): void {
  const root = target?.closest<HTMLElement>("[data-player-root='true']");
  if (!root?.isConnected || !target) return;
  const active = root.ownerDocument.activeElement;
  if (active === root) return;
  if (active && active !== root.ownerDocument.body && active !== root && !target.contains(active)) return;
  root.focus({ preventScroll: true });
}

export function restorePlayerPointerClickFocus(event: React.MouseEvent<HTMLElement>): void {
  if (event.detail > 0) restorePlayerPointerFocus(event.currentTarget);
}
