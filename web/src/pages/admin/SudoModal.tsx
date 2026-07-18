import React, { useState } from "react";
import { beginReauthentication, verifyReauthentication } from "../../api/auth";
import { Button } from "../../components/ui/Button";
import { Input } from "../../components/ui/Input";
import { Modal } from "../../components/ui/Modal";

interface SudoModalProps {
  isOpen: boolean;
  onSuccess: () => void | Promise<void>;
  onCancel: () => void;
  actionLabel: string;
}

export function SudoModal({ isOpen, onSuccess, onCancel, actionLabel }: SudoModalProps) {
  const [password, setPassword] = useState("");
  const [code, setCode] = useState("");
  const [challengeToken, setChallengeToken] = useState("");
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");

  const submit = async (event: React.FormEvent) => {
    event.preventDefault();
    setLoading(true);
    setError("");
    try {
      const response = challengeToken ? null : await beginReauthentication(password);
      if (response && "requires2fa" in response) {
        setChallengeToken(response.challengeToken);
        return;
      }
      if (challengeToken) await verifyReauthentication({ challengeToken, method: "totp", code });
      else if (response && "requires2fa" in response) return;
      await onSuccess();
      setPassword("");
      setCode("");
      setChallengeToken("");
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
        <Input label="Password" type="password" value={password} onChange={(event) => setPassword(event.target.value)} required />
        {challengeToken && <Input label="TOTP code" inputMode="numeric" maxLength={6} value={code} onChange={(event) => setCode(event.target.value.replace(/\D/g, ""))} />}
        {error && <p className="text-sm text-[var(--text-error)]">{error}</p>}
        <div className="flex gap-3"><Button type="submit" disabled={loading}>{loading ? "Verifying…" : "Authorize"}</Button><Button type="button" variant="ghost" onClick={onCancel}>Cancel</Button></div>
      </form>
    </Modal>
  );
}
