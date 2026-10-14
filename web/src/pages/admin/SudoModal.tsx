import React from "react";
import { Modal } from "../../components/ui/Modal";
import { ReauthenticationForm } from "./ReauthenticationForm";

interface SudoModalProps {
  isOpen: boolean;
  onSuccess: () => void | Promise<void>;
  onCancel: () => void;
  actionLabel: string;
}

export function SudoModal({ isOpen, onSuccess, onCancel, actionLabel }: SudoModalProps) {
  // Closing must discard pending credentials immediately, including during exit motion.
  if (!isOpen) return null;
  return <Modal isOpen={isOpen} onClose={onCancel} className="admin-sudo-modal" ariaLabel="Confirm your identity">
    <div className="admin-sudo-form">
      <header><p>PROTECTED ACTION</p><h2>Confirm your identity</h2><span>Confirm access to {actionLabel.toLowerCase()}.</span></header>
      <ReauthenticationForm onSuccess={onSuccess} onCancel={onCancel} submitLabel="Authorize" autoFocus={false} />
    </div>
  </Modal>;
}
