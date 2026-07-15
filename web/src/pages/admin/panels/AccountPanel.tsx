import React, { useCallback, useEffect, useState } from "react";
import { disable2FA, get2FAStatus, setup2FA, verifySetup2FA } from "../../../api/auth";
import { Button } from "../../../components/ui/Button";
import { GlassPane } from "../../../components/ui/GlassPane";
import { Input } from "../../../components/ui/Input";
import type { TwoFASetupResponse } from "../../../types/api";

export function AccountPanel() {
  const [enabled, setEnabled] = useState(false);
  const [email, setEmail] = useState("");
  const [setup, setSetup] = useState<TwoFASetupResponse | null>(null);
  const [code, setCode] = useState("");
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [message, setMessage] = useState("");

  const load = useCallback(async () => {
    setLoading(true);
    setError("");
    try { const status = await get2FAStatus(); setEnabled(status.twoFactorEnabled); setEmail(status.email); }
    catch (requestError) { setError(requestError instanceof Error ? requestError.message : "Account status could not be loaded."); }
    finally { setLoading(false); }
  }, []);

  useEffect(() => { void load(); }, [load]);

  const beginSetup = async () => {
    setError(""); setMessage("");
    try { setSetup(await setup2FA()); }
    catch (requestError) { setError(requestError instanceof Error ? requestError.message : "2FA setup failed."); }
  };

  const confirmSetup = async () => {
    setError("");
    try { const response = await verifySetup2FA(code); setMessage(response.message); setSetup(null); setCode(""); await load(); }
    catch (requestError) { setError(requestError instanceof Error ? requestError.message : "The code was not accepted."); }
  };

  const disable = async () => {
    setError("");
    try { const response = await disable2FA(code); setMessage(response.message); setCode(""); await load(); }
    catch (requestError) { setError(requestError instanceof Error ? requestError.message : "2FA could not be disabled."); }
  };

  return (
    <section className="mx-auto max-w-3xl p-8">
      <h1 className="text-3xl font-semibold">Account and TOTP</h1>
      <p className="mt-2 text-[var(--text-muted)]">Security controls for the authenticated server account.</p>
      <GlassPane className="mt-8 p-7" spotlight={false}>
        {loading ? <p>Loading account status…</p> : <><div className="flex items-center justify-between gap-4"><div><p className="font-semibold">{email}</p><p className="mt-1 text-sm text-[var(--text-muted)]">TOTP is {enabled ? "enabled" : "disabled"}.</p></div>{!enabled && !setup && <Button onClick={() => void beginSetup()}>Set up TOTP</Button>}</div>
        {setup && <div className="mt-6 rounded border border-[var(--glass-border)] p-5"><p className="text-sm">Add this secret to your authenticator:</p><code className="mt-3 block break-all rounded bg-black/30 p-3">{setup.secret}</code><p className="mt-3 break-all text-xs text-[var(--text-muted)]">{setup.provisioningUri}</p><div className="mt-4 flex gap-3"><Input aria-label="Setup TOTP code" inputMode="numeric" maxLength={6} value={code} onChange={(event) => setCode(event.target.value.replace(/\D/g, ""))} /><Button disabled={code.length !== 6} onClick={() => void confirmSetup()}>Verify</Button></div></div>}
        {enabled && <div className="mt-6 flex max-w-md gap-3"><Input aria-label="TOTP code to disable" inputMode="numeric" maxLength={6} value={code} onChange={(event) => setCode(event.target.value.replace(/\D/g, ""))} placeholder="Six-digit TOTP code" /><Button variant="secondary" disabled={code.length !== 6} onClick={() => void disable()}>Disable TOTP</Button></div>}</>}
        {error && <p className="mt-4 text-sm text-[var(--text-error)]">{error}</p>}{message && <p className="mt-4 text-sm text-green-400">{message}</p>}
      </GlassPane>
    </section>
  );
}
