import React, { useId, useRef, useState } from "react";
import { createPortal } from "react-dom";
import { Modal } from "../../components/ui/Modal";
import "./AdminHelp.css";

interface AdminHelpProps {
  label: string;
  title?: string;
  description?: string;
  variant?: "help" | "details";
  className?: string;
  children: React.ReactNode;
}

/** Optional explanations and diagnostics; required warnings stay in the page. */
export function AdminHelp({ label, title = label, description, variant = "help", className = "", children }: AdminHelpProps) {
  const [open, setOpen] = useState(false);
  const [portalTarget, setPortalTarget] = useState<Element | null>(null);
  const trigger = useRef<HTMLButtonElement>(null);
  const headingId = useId();
  const dialog = <Modal isOpen={open} onClose={() => setOpen(false)} ariaLabel={title} className={`admin-help-dialog admin-help-dialog--${variant}`}>
    <header className="admin-help-dialog__header">
      <div><p>{variant === "details" ? "DETAILS" : "HELP"}</p><h2 id={headingId}>{title}</h2>{description && <span>{description}</span>}</div>
      <button className="admin-help-dialog__close" type="button" aria-label={`Close ${title}`} onClick={() => setOpen(false)}><span aria-hidden="true">×</span></button>
    </header>
    <div className="admin-help-dialog__content" role="region" aria-labelledby={headingId} tabIndex={0}>{children}</div>
  </Modal>;
  return <div className={`admin-context-help ${className}`}>
    <button ref={trigger} className="admin-help-trigger" type="button" aria-haspopup="dialog" aria-expanded={open} onClick={() => {
      // Keep inherited theme tokens without putting a fixed dialog inside panel motion.
      setPortalTarget(trigger.current?.closest(".admin-shell") ?? document.body);
      setOpen(true);
    }}><span className="admin-help-trigger__icon" aria-hidden="true">?</span><span>{label}</span></button>
    {portalTarget && createPortal(dialog, portalTarget)}
  </div>;
}
