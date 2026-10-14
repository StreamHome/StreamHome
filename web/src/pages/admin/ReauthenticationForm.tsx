import React, { useEffect, useRef, useState } from "react";
import { AnimatePresence, motion } from "framer-motion";
import { beginReauthentication, verifyReauthentication } from "../../api/auth";
import { ApiError } from "../../api/client";
import { AuthenticatorCodeInput } from "../../components/ui/AuthenticatorCodeInput";
import { Button } from "../../components/ui/Button";
import { Input } from "../../components/ui/Input";
import { MOTION_TIMINGS, useAppMotion } from "../../motion/motionSystem";

interface ReauthenticationFormProps {
  onSuccess: () => void | Promise<void>;
  onCancel?: () => void;
  submitLabel?: string;
  autoFocus?: boolean;
  headingLevel?: 2 | 3;
}

export function ReauthenticationForm({ onSuccess, onCancel, submitLabel = "Continue", autoFocus = true, headingLevel = 3 }: ReauthenticationFormProps) {
  const StepHeading = headingLevel === 2 ? "h2" : "h3";
  const [password, setPassword] = useState("");
  const [visible, setVisible] = useState(false);
  const [challengeToken, setChallengeToken] = useState("");
  const [code, setCode] = useState("");
  const [method, setMethod] = useState<"totp" | "recovery">("totp");
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");
  const { reduced } = useAppMotion();
  const passwordRef = useRef<HTMLInputElement>(null);
  const active = useRef(true);
  useEffect(() => { active.current = true; return () => { active.current = false; }; }, []);

  const reset = () => { setChallengeToken(""); setCode(""); setPassword(""); setMethod("totp"); setVisible(false); setError(""); };
  useEffect(() => { if (!challengeToken && autoFocus) passwordRef.current?.focus(); }, [challengeToken, autoFocus]);
  const submit = async (event: React.FormEvent) => {
    event.preventDefault();
    if (loading) return;
    setLoading(true); setError("");
    try {
      if (challengeToken) {
        await verifyReauthentication({ challengeToken, method, code });
      } else {
        const response = await beginReauthentication(password);
        if (!active.current) return;
        if ("requires2fa" in response) { setChallengeToken(response.challengeToken); setPassword(""); setVisible(false); return; }
      }
      if (!active.current) return;
      // A successful challenge is consumed. Never reuse it if the subsequent action fails.
      reset();
      await onSuccess();
    } catch (requestError) {
      if (!active.current) return;
      if (requestError instanceof ApiError && ["invalid_challenge", "challenge_expired"].includes(requestError.code)) reset();
      setError(requestError instanceof Error ? requestError.message : "Identity confirmation failed. Please try again.");
    } finally { if (active.current) setLoading(false); }
  };

  return <form className="admin-auth-form" onSubmit={submit} aria-busy={loading}>
    <div className="admin-auth-step-heading"><StepHeading>{challengeToken ? method === "totp" ? "Authenticator" : "Recovery code" : "Password"}</StepHeading><p>{challengeToken ? method === "totp" ? "Enter the six-digit code from your authenticator app." : "Enter an unused recovery code. Each code can be used once." : "Enter your account password to continue."}</p></div>
    {!challengeToken && <div className="admin-password-field">
      <Input ref={passwordRef} className="admin-field" label="Password" type={visible ? "text" : "password"} autoComplete="current-password"
        value={password} onChange={(event) => setPassword(event.target.value)} required disabled={loading} autoFocus={autoFocus} />
      <button type="button" className="admin-text-action" disabled={loading} onClick={() => setVisible(!visible)} aria-pressed={visible}>{visible ? "Hide password" : "Show password"}</button>
    </div>}
    <AnimatePresence initial={false}>{challengeToken && <motion.div key="factor" className="admin-factor-step"
      initial={reduced ? { opacity: 0 } : { opacity: 0, y: 8 }} animate={{ opacity: 1, y: 0 }} exit={{ opacity: 0 }}
      transition={{ duration: reduced ? MOTION_TIMINGS.reduced : MOTION_TIMINGS.notice }}>
      <p className="admin-auth-completed" role="status">✓ Password confirmed</p>
      {method === "totp" ? <AuthenticatorCodeInput value={code} onChange={setCode} disabled={loading} error={error || undefined} autoFocus /> :
        <><Input className="admin-field" label="Recovery code" autoComplete="off" value={code} onChange={(event) => setCode(event.target.value.toUpperCase())} aria-describedby="reauth-recovery-help" error={error || undefined} required disabled={loading} autoFocus /><p id="reauth-recovery-help" className="admin-field-help">Use the code saved during setup, in XXXX-XXXX-XXXX-XXXX format. Spaces and hyphens are accepted.</p></>}
    </motion.div>}</AnimatePresence>
    {error && !challengeToken && <p className="admin-form-message admin-form-message--error" role="alert">{error}</p>}
    <div className="admin-auth-actions"><Button className="admin-primary-action" type="submit" disabled={loading}>{loading ? "Verifying…" : challengeToken ? method === "totp" ? "Verify authenticator" : "Verify recovery code" : submitLabel}</Button>
      {onCancel && <Button type="button" variant="ghost" disabled={loading} onClick={onCancel}>Cancel</Button>}</div>
    {challengeToken && <div className="admin-auth-alternatives"><button type="button" className="admin-text-action" disabled={loading} onClick={() => { setMethod(method === "totp" ? "recovery" : "totp"); setCode(""); setError(""); }}>{method === "totp" ? "Use recovery code" : "Use authenticator"}</button><button type="button" className="admin-text-action" disabled={loading} onClick={reset}>Start again</button></div>}
  </form>;
}
