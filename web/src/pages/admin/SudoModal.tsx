import React, { useState } from "react";
import { login, verify2FA } from "../../api/auth";
import { Button } from "../../components/ui/Button";
import { Input } from "../../components/ui/Input";
import { Modal } from "../../components/ui/Modal";
import { useAuthStore } from "../../stores/authStore";

interface SudoModalProps {
  isOpen: boolean;
  onSuccess: () => void | Promise<void>;
  onCancel: () => void;
  actionLabel: string;
}

export function SudoModal({ isOpen, onSuccess, onCancel, actionLabel }: SudoModalProps) {
  const storedEmail = useAuthStore((state) => state.email);
  const [email, setEmail] = useState(storedEmail ?? "");
  const [password, setPassword] = useState("");
  const [code, setCode] = useState("");
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");

  const submit = async (event: React.FormEvent) => {
    event.preventDefault();
    setLoading(true);
    setError("");
    try {
      const response = await login({ email, password });
      if ("requires2fa" in response) {
        if (code.length !== 6) throw new Error("A six-digit TOTP code is required for this account.");
        await verify2FA({ email, code });
      }
      await onSuccess();
      setPassword("");
      setCode("");
    } catch (requestError) {
      setError(requestError instanceof Error ? requestError.message : "Reauthentication failed.");
    } finally {
      setLoading(false);
    }
  };

  return (
    <Modal isOpen={isOpen} onClose={onCancel}>
      <form className="flex flex-col gap-4 p-7" onSubmit={submit}>
        <h2 className="text-2xl font-semibold">Authorization required</h2>
        <p className="text-sm text-[var(--text-muted)]">Confirm the server account to {actionLabel.toLowerCase()}.</p>
        <Input label="Account email" type="email" value={email} onChange={(event) => setEmail(event.target.value)} required />
        <Input label="Password" type="password" value={password} onChange={(event) => setPassword(event.target.value)} required />
        <Input label="TOTP code (when enabled)" inputMode="numeric" maxLength={6} value={code} onChange={(event) => setCode(event.target.value.replace(/\D/g, ""))} />
        {error && <p className="text-sm text-[var(--text-error)]">{error}</p>}
        <div className="flex gap-3"><Button type="submit" disabled={loading}>{loading ? "Verifying…" : "Authorize"}</Button><Button type="button" variant="ghost" onClick={onCancel}>Cancel</Button></div>
      </form>
    </Modal>
  );
}
