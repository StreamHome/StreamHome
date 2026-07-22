import React, { useCallback, useEffect, useRef, useState } from "react";
import { AnimatePresence, motion } from "framer-motion";
import { useLocation, useNavigate } from "react-router-dom";
import { getHealth, login, verify2FA } from "../api/auth";
import { ApiError } from "../api/client";
import { BrandLogo } from "../components/brand/BrandLogo";
import { MOTION_EASE, MOTION_TIMINGS, useAppMotion } from "../motion/motionSystem";
import { useAuthStore } from "../stores/authStore";
import "./login.css";

const REMEMBERED_EMAIL_KEY = "streamhome_remembered_email";
const REQUEST_TIMEOUT = 12_000;
const WEB_VERSION = import.meta.env.VITE_APP_VERSION || "0.0.0";
const BUILD_ID = import.meta.env.VITE_BUILD_ID || "dev";
type AuthStage = "credentials" | "factor" | "success";
type FactorMethod = "totp" | "recovery";
type ServerState = "checking" | "ready" | "offline" | "unreachable";

function FieldIcon({ type }: { type: "email" | "lock" }) {
  return type === "email"
    ? <svg viewBox="0 0 24 24" aria-hidden="true"><path d="M4 6.5h16v11H4zM4.5 7l7.5 6 7.5-6" /></svg>
    : <svg viewBox="0 0 24 24" aria-hidden="true"><rect x="5" y="10" width="14" height="10" rx="2" /><path d="M8 10V7a4 4 0 0 1 8 0v3" /></svg>;
}

function requestController() {
  const controller = new AbortController();
  const timeout = window.setTimeout(() => controller.abort(), REQUEST_TIMEOUT);
  return { signal: controller.signal, stop: () => window.clearTimeout(timeout) };
}

export function LoginPage() {
  const navigate = useNavigate();
  const location = useLocation();
  const setToken = useAuthStore((state) => state.setToken);
  const { reduced } = useAppMotion();
  const navigationTimer = useRef<number | null>(null);
  const codeInput = useRef<HTMLInputElement | null>(null);
  const remembered = localStorage.getItem(REMEMBERED_EMAIL_KEY) ?? "";
  const [email, setEmail] = useState(remembered);
  const [password, setPassword] = useState("");
  const [rememberEmail, setRememberEmail] = useState(Boolean(remembered));
  const [showPassword, setShowPassword] = useState(false);
  const [capsLock, setCapsLock] = useState(false);
  const [focusedField, setFocusedField] = useState("");
  const [error, setError] = useState("");
  const [errorCode, setErrorCode] = useState("");
  const [isLoading, setIsLoading] = useState(false);
  const [progressLabel, setProgressLabel] = useState("");
  const [stage, setStage] = useState<AuthStage>("credentials");
  const [factorMethod, setFactorMethod] = useState<FactorMethod>("totp");
  const [challengeToken, setChallengeToken] = useState("");
  const [code, setCode] = useState("");
  const [inputNotice, setInputNotice] = useState("");
  const [lockedSeconds, setLockedSeconds] = useState(0);
  const [serverState, setServerState] = useState<ServerState>("checking");
  const [serverVersion, setServerVersion] = useState("—");
  const [serverClockOffset, setServerClockOffset] = useState(0);
  const [totpSeconds, setTotpSeconds] = useState(30);

  const checkServer = useCallback(async () => {
    if (!navigator.onLine) { setServerState("offline"); return; }
    const controller = new AbortController();
    const timeout = window.setTimeout(() => controller.abort(), 4_000);
    try {
      const health = await getHealth(controller.signal);
      setServerState("ready");
      setServerVersion(health.version);
      setServerClockOffset(health.serverTime * 1000 - Date.now());
    } catch { setServerState(navigator.onLine ? "unreachable" : "offline"); }
    finally { window.clearTimeout(timeout); }
  }, []);

  useEffect(() => {
    void checkServer();
    const interval = window.setInterval(() => void checkServer(), 30_000);
    const refresh = () => void checkServer();
    window.addEventListener("online", refresh); window.addEventListener("offline", refresh);
    return () => { window.clearInterval(interval); window.removeEventListener("online", refresh); window.removeEventListener("offline", refresh); if (navigationTimer.current !== null) window.clearTimeout(navigationTimer.current); };
  }, [checkServer]);

  useEffect(() => {
    const update = () => setTotpSeconds(30 - (Math.floor((Date.now() + serverClockOffset) / 1000) % 30));
    update(); const timer = window.setInterval(update, 1000); return () => window.clearInterval(timer);
  }, [serverClockOffset]);

  useEffect(() => {
    if (!lockedSeconds) return;
    const timer = window.setInterval(() => setLockedSeconds((value) => Math.max(0, value - 1)), 1000);
    return () => window.clearInterval(timer);
  }, [lockedSeconds > 0]);

  useEffect(() => {
    const onKey = (event: KeyboardEvent) => { if (event.key === "Escape" && stage === "factor" && !isLoading) returnToCredentials(); };
    window.addEventListener("keydown", onKey); return () => window.removeEventListener("keydown", onKey);
  });

  const showRequestError = (requestError: unknown, fallback: string) => {
    const apiError = requestError instanceof ApiError ? requestError : null;
    setError(apiError?.message ?? (requestError instanceof Error ? requestError.message : fallback));
    setErrorCode(apiError?.code ?? "request_failed");
    if (apiError?.retryAfterSeconds) setLockedSeconds(apiError.retryAfterSeconds);
  };

  const completeLogin = (accountEmail: string) => {
    if (rememberEmail) localStorage.setItem(REMEMBERED_EMAIL_KEY, accountEmail);
    else localStorage.removeItem(REMEMBERED_EMAIL_KEY);
    setToken("", accountEmail);
    setProgressLabel("Opening profiles");
    setStage("success");
    navigationTimer.current = window.setTimeout(() => navigate("/profiles", { state: location.state }), reduced ? 220 : 900);
  };

  const handleLoginSubmit = async (event: React.FormEvent) => {
    event.preventDefault();
    if (lockedSeconds) return;
    setError(""); setErrorCode(""); setIsLoading(true); setProgressLabel(serverState === "ready" ? "Verifying credentials" : "Contacting server");
    const controller = requestController();
    try {
      const response = await login({ email: email.trim(), password }, controller.signal);
      if ("requires2fa" in response) {
        setEmail(response.email); setChallengeToken(response.challengeToken); setStage("factor"); setFactorMethod("totp"); setCode(""); setProgressLabel("");
        window.setTimeout(() => codeInput.current?.focus(), reduced ? 0 : 220);
      } else completeLogin(response.email);
    } catch (requestError) { setProgressLabel(""); showRequestError(requestError, "Login failed."); }
    finally { controller.stop(); setIsLoading(false); }
  };

  const submitFactor = async (event?: React.FormEvent) => {
    event?.preventDefault();
    const ready = factorMethod === "totp" ? code.length === 6 : code.replace(/[-\s]/g, "").length >= 12;
    if (!ready || isLoading || lockedSeconds) return;
    setError(""); setErrorCode(""); setIsLoading(true); setProgressLabel(factorMethod === "totp" ? "Verifying authenticator code" : "Verifying recovery code");
    const controller = requestController();
    try {
      const response = await verify2FA({ challengeToken, method: factorMethod, code }, controller.signal);
      completeLogin(response.email);
    } catch (requestError) { setProgressLabel(""); showRequestError(requestError, "The code was not accepted."); setCode(""); codeInput.current?.focus(); }
    finally { controller.stop(); setIsLoading(false); }
  };

  const returnToCredentials = () => { setStage("credentials"); setCode(""); setChallengeToken(""); setError(""); setErrorCode(""); setFactorMethod("totp"); setProgressLabel(""); };
  const emailValid = /^[^\s@]+@[^\s@]+$/.test(email);
  const passwordValid = password.length > 0;
  const factorReady = factorMethod === "totp" ? code.length === 6 : code.replace(/[-\s]/g, "").length >= 12;
  const circumference = 2 * Math.PI * 18;
  const stageTransition = { duration: reduced ? MOTION_TIMINGS.reduced : MOTION_TIMINGS.dialogEnter, ease: MOTION_EASE };
  const statusLabel = serverState === "ready" ? "Server ready" : serverState === "checking" ? "Checking server" : serverState === "offline" ? "Device offline" : "Server unreachable";

  return <motion.main className="login-page linear-ember-login" data-theme="ember" data-interaction="terminal" data-focus={focusedField} aria-busy={isLoading}>
    <div className="login-ambient" aria-hidden="true"><i /><i /><span /></div>
    <button type="button" className="login-server-status" data-state={serverState} onClick={() => void checkServer()} aria-label={`${statusLabel}. Check again.`}><i />{statusLabel}</button>
    <motion.section className="login-auth-card" initial={reduced ? { opacity: 0 } : { opacity: 0, y: 10, scale: .995 }} animate={{ opacity: 1, y: 0, scale: 1 }} transition={{ duration: reduced ? MOTION_TIMINGS.reduced : MOTION_TIMINGS.dialogEnter, ease: MOTION_EASE }} aria-labelledby="login-title">
      <header className="login-auth-header"><BrandLogo className="brand-logo--login" /><p>Private media server</p><h1 id="login-title">{stage === "factor" ? "Verify your identity" : stage === "success" ? "Welcome back" : "Sign in to StreamHome"}</h1><span>{stage === "factor" ? factorMethod === "totp" ? `Enter the authenticator code for ${email}.` : "Enter one of your unused recovery codes." : stage === "success" ? "Your secure session is ready." : "Use your local server account to continue."}</span></header>
      <AnimatePresence mode="sync" initial={false}>
        {stage === "credentials" ? <motion.form key="credentials" className="login-auth-form" initial={{ opacity: 0, x: reduced ? 0 : -14 }} animate={{ opacity: 1, x: 0 }} exit={{ opacity: 0, x: reduced ? 0 : -14 }} transition={stageTransition} onSubmit={handleLoginSubmit}>
          <div className="login-field" data-valid={emailValid || undefined}><label htmlFor="login-email">Email address</label><div className="login-input-shell"><FieldIcon type="email" /><input id="login-email" type="email" autoComplete="username" value={email} onFocus={() => setFocusedField("email")} onBlur={() => setFocusedField("")} onChange={(event) => { setEmail(event.target.value); setError(""); }} placeholder="operator@streamhome.local" required disabled={isLoading} /><i className="login-valid-mark" aria-hidden="true">✓</i></div></div>
          <div className="login-field" data-valid={passwordValid || undefined}><label htmlFor="login-password">Password</label><div className="login-password-field login-input-shell"><FieldIcon type="lock" /><input id="login-password" type={showPassword ? "text" : "password"} autoComplete="current-password" value={password} onFocus={() => setFocusedField("password")} onBlur={() => { setFocusedField(""); setCapsLock(false); }} onKeyUp={(event) => setCapsLock(event.getModifierState("CapsLock"))} onKeyDown={(event) => setCapsLock(event.getModifierState("CapsLock"))} onChange={(event) => { setPassword(event.target.value); setError(""); }} placeholder="Enter your password" required disabled={isLoading} /><button type="button" className="login-password-toggle" aria-label={showPassword ? "Hide password" : "Show password"} aria-pressed={showPassword} onClick={() => setShowPassword((visible) => !visible)} disabled={isLoading}>{showPassword ? "Hide" : "Show"}</button></div>{capsLock && <p className="login-field-notice" role="status">Caps Lock is on</p>}</div>
          <label className="login-remember"><input type="checkbox" checked={rememberEmail} onChange={(event) => setRememberEmail(event.target.checked)} /><span>Remember email on this device</span></label>
          <AnimatePresence initial={false}>{error && <motion.div className="login-error" data-code={errorCode} role="alert" initial={{ opacity: 0, y: -5 }} animate={{ opacity: 1, y: 0 }} exit={{ opacity: 0 }}><i /> <span>{error}{lockedSeconds > 0 && ` Try again in ${Math.floor(lockedSeconds / 60)}:${String(lockedSeconds % 60).padStart(2, "0")}.`}</span></motion.div>}</AnimatePresence>
          {progressLabel && <div className="login-progress-label" role="status"><i className="login-button-spinner" />{progressLabel}</div>}
          <button type="submit" className="login-primary-action" disabled={isLoading || !emailValid || !passwordValid || lockedSeconds > 0}><span>{isLoading ? "Please wait…" : lockedSeconds ? "Account temporarily locked" : "Continue"}</span>{!isLoading && !lockedSeconds && <b aria-hidden="true">→</b>}</button>
          <p className="login-keyboard-hint"><kbd>Enter</kbd> to continue</p>
        </motion.form> : stage === "factor" ? <motion.form key="factor" className="login-totp-stage" initial={{ opacity: 0, x: reduced ? 0 : 14 }} animate={{ opacity: 1, x: 0 }} exit={{ opacity: 0, x: reduced ? 0 : 14 }} transition={stageTransition} onSubmit={submitFactor}>
          {factorMethod === "totp" ? <div className="login-code-layout"><div className="login-totp-inputs" onClick={() => codeInput.current?.focus()} data-focused={focusedField === "code"}><input ref={codeInput} className="login-code-input" type="text" inputMode="numeric" autoComplete="one-time-code" aria-label="Six-digit authenticator code" value={code} onFocus={() => setFocusedField("code")} onBlur={() => setFocusedField("")} onPaste={() => setInputNotice("Code pasted and ready to verify.")} onChange={(event) => { const next = event.target.value.replace(/\D/g, "").slice(0, 6); if (next.length - code.length > 1) setInputNotice("Authenticator autofill received."); setCode(next); setError(""); }} disabled={isLoading} />{Array.from({ length: 6 }, (_, index) => <span key={index} data-filled={Boolean(code[index])}>{code[index] || ""}</span>)}</div><div className="login-totp-ring" aria-label={`${totpSeconds} seconds remain in the current authenticator window`}><svg viewBox="0 0 44 44"><circle cx="22" cy="22" r="18" /><circle cx="22" cy="22" r="18" style={{ strokeDasharray: circumference, strokeDashoffset: circumference * (1 - totpSeconds / 30) }} /></svg><b>{totpSeconds}</b></div></div> : <div className="login-field"><label htmlFor="login-recovery">Recovery code</label><input ref={codeInput} id="login-recovery" className="login-recovery-input" autoComplete="one-time-code" value={code} onFocus={() => setFocusedField("code")} onBlur={() => setFocusedField("")} onPaste={() => setInputNotice("Recovery code pasted.")} onChange={(event) => { setCode(event.target.value.toUpperCase().slice(0, 19)); setError(""); }} placeholder="XXXX-XXXX-XXXX-XXXX" disabled={isLoading} /></div>}
          <p className="login-input-notice" aria-live="polite">{inputNotice}</p>
          {error && <div className="login-error" role="alert"><i /><span>{error}{lockedSeconds > 0 && ` Try again in ${Math.floor(lockedSeconds / 60)}:${String(lockedSeconds % 60).padStart(2, "0")}.`}</span></div>}
          {progressLabel && <div className="login-progress-label" role="status"><i className="login-button-spinner" />{progressLabel}</div>}
          <button type="submit" className="login-primary-action" disabled={!factorReady || isLoading || lockedSeconds > 0}>{isLoading ? "Verifying…" : factorMethod === "totp" ? "Verify authenticator" : "Use recovery code"}</button>
          <button type="button" className="login-method-action" onClick={() => { setFactorMethod((current) => current === "totp" ? "recovery" : "totp"); setCode(""); setError(""); setInputNotice(""); window.setTimeout(() => codeInput.current?.focus(), 0); }}>{factorMethod === "totp" ? "Use a recovery code" : "Use authenticator code"}</button>
          <button type="button" className="login-secondary-action" onClick={returnToCredentials} disabled={isLoading}>← Back to sign in</button><p className="login-keyboard-hint"><kbd>Enter</kbd> verify · <kbd>Esc</kbd> go back</p>
        </motion.form> : <motion.div key="success" className="login-success" initial={{ opacity: 0, scale: reduced ? 1 : .9 }} animate={{ opacity: 1, scale: 1 }} transition={stageTransition}><motion.i initial={{ pathLength: 0 }} animate={{ pathLength: 1 }}>✓</motion.i><p>Authentication complete</p><span>{progressLabel}</span></motion.div>}
      </AnimatePresence>
      <footer className="login-auth-footer"><span><i />Local TOTP security</span><span>Server v{serverVersion} · Web {WEB_VERSION}{BUILD_ID !== "dev" ? ` · ${BUILD_ID}` : ""}</span></footer>
    </motion.section>
  </motion.main>;
}
