import { useEffect, useLayoutEffect, useRef, type RefObject } from "react";

interface DialogLifecycleOptions {
  open: boolean;
  dialogRef: RefObject<HTMLElement | null>;
  onClose: () => void;
  invokerRef?: RefObject<HTMLElement | null>;
  inertRef?: RefObject<HTMLElement | null>;
  returnFocus?: () => HTMLElement | null;
}

function dialogControls(dialog: HTMLElement): HTMLElement[] {
  return Array.from(dialog.querySelectorAll<HTMLElement>('button:not(:disabled), [href], input:not(:disabled), select:not(:disabled), textarea:not(:disabled), [tabindex]:not([tabindex="-1"])')).filter((element) => {
    if (element.tabIndex < 0 || element.matches(":disabled")) return false;
    if (element.closest('[hidden], [inert], [aria-hidden="true"]')) return false;
    const style = window.getComputedStyle(element);
    return style.display !== "none" && style.visibility !== "hidden";
  });
}

/** Shared behavior only: callers retain their own dialog markup and presentation. */
export function useDialogLifecycle({ open, dialogRef, onClose, invokerRef, inertRef, returnFocus }: DialogLifecycleOptions) {
  const callbacks = useRef({ onClose, returnFocus });
  callbacks.current = { onClose, returnFocus };
  const repairPendingFocus = useRef<(() => void) | null>(null);
  useEffect(() => {
    if (!open) return;
    const dialog = dialogRef.current;
    const previousFocus = invokerRef?.current ?? (document.activeElement instanceof HTMLElement && !dialog?.contains(document.activeElement) ? document.activeElement : null);
    const background = inertRef?.current;
    const previousInert = background?.inert ?? false;
    const previousOverflow = document.body.style.overflow;
    if (background) background.inert = true;
    document.body.style.overflow = "hidden";
    const focusable = () => dialog ? dialogControls(dialog) : [];
    const focusFirst = () => (focusable()[0] ?? dialog)?.focus();
    repairPendingFocus.current = () => {
      if (!dialog) return;
      const active = document.activeElement;
      const unavailable = active instanceof HTMLElement && (active.matches(":disabled") || Boolean(active.closest('[hidden], [inert], [aria-hidden="true"]')));
      if (!dialog.contains(active) || unavailable) focusFirst();
    };
    focusFirst();
    const handleKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape") { event.preventDefault(); callbacks.current.onClose(); return; }
      if (event.key !== "Tab") return;
      const items = focusable();
      const first = items[0];
      const last = items[items.length - 1];
      if (!first) { event.preventDefault(); dialog?.focus(); }
      else if (event.shiftKey && (document.activeElement === first || !dialog?.contains(document.activeElement))) { event.preventDefault(); last.focus(); }
      else if (!event.shiftKey && (document.activeElement === last || !dialog?.contains(document.activeElement))) { event.preventDefault(); first.focus(); }
    };
    const containFocus = (event: FocusEvent) => { if (event.target instanceof Node && !dialog?.contains(event.target)) focusFirst(); };
    window.addEventListener("keydown", handleKeyDown);
    document.addEventListener("focusin", containFocus);
    return () => {
      repairPendingFocus.current = null;
      window.removeEventListener("keydown", handleKeyDown);
      document.removeEventListener("focusin", containFocus);
      document.body.style.overflow = previousOverflow;
      if (background) background.inert = previousInert;
      const target = callbacks.current.returnFocus?.() ?? previousFocus;
      if (target?.isConnected) target.focus();
    };
  }, [open, dialogRef, invokerRef, inertRef]);

  // Disabling the focused submit can drop native focus to BODY without focusin.
  // Repair after each owning render, before paint, without recapturing the invoker.
  useLayoutEffect(() => {
    if (open) repairPendingFocus.current?.();
  });
}
