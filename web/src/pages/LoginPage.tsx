import React, { useEffect, useRef, useState } from "react";
import { AnimatePresence, motion } from "framer-motion";
import { useLocation, useNavigate } from "react-router-dom";
import { login, verify2FA } from "../api/auth";
import { BrandLogo } from "../components/brand/BrandLogo";
import { MOTION_EASE, MOTION_TIMINGS, useAppMotion } from "../motion/motionSystem";
import { useAuthStore } from "../stores/authStore";
import "./login.css";

const EMPTY_TOTP = () => Array<string>(6).fill("");

export function LoginPage() {
  const navigate = useNavigate();
  const location = useLocation();
  const setToken = useAuthStore((state) => state.setToken);
  const { reduced } = useAppMotion();
  const navigationTimer = useRef<number | null>(null);
  const inputRefs = useRef<(HTMLInputElement | null)[]>([]);

  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [showPassword, setShowPassword] = useState(false);
  const [error, setError] = useState("");
  const [isLoading, setIsLoading] = useState(false);
  const [isLeaving, setIsLeaving] = useState(false);
  const [requires2FA, setRequires2FA] = useState(false);
  const [totpCode, setTotpCode] = useState<string[]>(EMPTY_TOTP);

  useEffect(() => () => {
    if (navigationTimer.current !== null) window.clearTimeout(navigationTimer.current);
  }, []);

  const completeLogin = (accessToken: string, accountEmail: string) => {
    setToken(accessToken, accountEmail);
    setIsLeaving(true);
    navigationTimer.current = window.setTimeout(() => navigate("/profiles", { state: location.state }), reduced ? 180 : 520);
  };

  const handleLoginSubmit = async (event: React.FormEvent) => {
    event.preventDefault();
    setError("");
    setIsLoading(true);
    try {
      const response = await login({ email, password });
      if ("requires2fa" in response) {
        setEmail(response.email);
        setRequires2FA(true);
        window.setTimeout(() => inputRefs.current[0]?.focus(), reduced ? 0 : 260);
      } else completeLogin(response.accessToken, response.email);
    } catch (requestError: unknown) {
      setError(requestError instanceof Error ? requestError.message : "Login failed");
    } finally {
      setIsLoading(false);
    }
  };

  const submitTotp = async (digits: string[]) => {
    if (isLoading || digits.some((digit) => digit === "")) return;
    setError("");
    setIsLoading(true);
    try {
      const response = await verify2FA({ email, code: digits.join("") });
      completeLogin(response.accessToken, response.email);
    } catch (requestError: unknown) {
      setError(requestError instanceof Error ? requestError.message : "Invalid TOTP code");
      setTotpCode(EMPTY_TOTP());
      inputRefs.current[0]?.focus();
    } finally {
      setIsLoading(false);
    }
  };

  const handleTotpChange = (index: number, value: string) => {
    const digit = value.replace(/\D/g, "").slice(-1);
    const next = [...totpCode];
    next[index] = digit;
    setTotpCode(next);
    setError("");
    if (digit && index < 5) inputRefs.current[index + 1]?.focus();
    if (digit && index === 5) void submitTotp(next);
  };

  const handleTotpPaste = (event: React.ClipboardEvent<HTMLDivElement>) => {
    const digits = event.clipboardData.getData("text").replace(/\D/g, "").slice(0, 6).split("");
    if (!digits.length) return;
    event.preventDefault();
    const next = EMPTY_TOTP();
    digits.forEach((digit, index) => { next[index] = digit; });
    setTotpCode(next);
    setError("");
    if (digits.length === 6) void submitTotp(next);
    else inputRefs.current[digits.length]?.focus();
  };

  const returnToCredentials = () => {
    setRequires2FA(false);
    setTotpCode(EMPTY_TOTP());
    setError("");
  };

  const stageTransition = { duration: reduced ? MOTION_TIMINGS.reduced : MOTION_TIMINGS.dialogEnter, ease: MOTION_EASE };

  return <motion.main
    className="login-page linear-ember-login"
    data-theme="ember"
    data-interaction="terminal"
    aria-busy={isLoading}
    animate={isLeaving ? { opacity: 0, scale: reduced ? 1 : 1.012, filter: reduced ? "none" : "blur(8px)" } : { opacity: 1, scale: 1, filter: "blur(0px)" }}
    transition={{ duration: reduced ? MOTION_TIMINGS.reduced : MOTION_TIMINGS.viewEnter, ease: MOTION_EASE }}
  >
    <div className="login-ambient" aria-hidden="true"><i /><i /><span /></div>
    <div className="login-server-status" aria-label="Private server authentication"><i />Private server</div>

    <motion.section
      className="login-auth-card"
      initial={reduced ? { opacity: 0 } : { opacity: 0, y: 22, scale: .985, filter: "blur(10px)" }}
      animate={{ opacity: 1, y: 0, scale: 1, filter: "blur(0px)" }}
      transition={{ duration: reduced ? MOTION_TIMINGS.reduced : .64, ease: MOTION_EASE }}
      aria-labelledby="login-title"
    >
      <header className="login-auth-header">
        <BrandLogo className="brand-logo--login" />
        <p>Private media server</p>
        <h1 id="login-title">{requires2FA ? "Verify your identity" : "Sign in to StreamHome"}</h1>
        <span>{requires2FA ? `Enter the authenticator code for ${email}.` : "Use your local server account to continue."}</span>
      </header>

      <AnimatePresence mode="wait" initial={false}>
        {!requires2FA ? <motion.form
          key="credentials"
          className="login-auth-form"
          initial={reduced ? { opacity: 0 } : { opacity: 0, x: -14 }}
          animate={{ opacity: 1, x: 0 }}
          exit={reduced ? { opacity: 0 } : { opacity: 0, x: -14 }}
          transition={stageTransition}
          onSubmit={handleLoginSubmit}
        >
          <div className="login-field">
            <label htmlFor="login-email">Email address</label>
            <input id="login-email" type="email" autoComplete="username" value={email} onChange={(event) => setEmail(event.target.value)} placeholder="operator@streamhome.local" required disabled={isLoading} />
          </div>

          <div className="login-field">
            <label htmlFor="login-password">Password</label>
            <div className="login-password-field">
              <input id="login-password" type={showPassword ? "text" : "password"} autoComplete="current-password" value={password} onChange={(event) => setPassword(event.target.value)} placeholder="Enter your password" required disabled={isLoading} />
              <button type="button" className="login-password-toggle" aria-label={showPassword ? "Hide password" : "Show password"} aria-pressed={showPassword} onClick={() => setShowPassword((visible) => !visible)} disabled={isLoading}>{showPassword ? "Hide" : "Show"}</button>
            </div>
          </div>

          <AnimatePresence initial={false}>{error && <motion.div className="login-error" role="alert" aria-live="polite" initial={{ opacity: 0, y: -5 }} animate={{ opacity: 1, y: 0 }} exit={{ opacity: 0, y: -4 }} transition={{ duration: MOTION_TIMINGS.notice }}><i aria-hidden="true" />{error}</motion.div>}</AnimatePresence>

          <button type="submit" className="login-primary-action" disabled={isLoading}>
            {isLoading && <i className="login-button-spinner" aria-hidden="true" />}
            <span>{isLoading ? "Signing in…" : "Continue"}</span>
            {!isLoading && <b aria-hidden="true">→</b>}
          </button>
        </motion.form> : <motion.div
          key="totp"
          className="login-totp-stage"
          initial={reduced ? { opacity: 0 } : { opacity: 0, x: 14 }}
          animate={{ opacity: 1, x: 0 }}
          exit={reduced ? { opacity: 0 } : { opacity: 0, x: 14 }}
          transition={stageTransition}
        >
          <div className="login-totp-inputs" onPaste={handleTotpPaste} aria-label="Six-digit TOTP code">
            {totpCode.map((digit, index) => <input
              key={index}
              ref={(element) => { inputRefs.current[index] = element; }}
              type="text"
              inputMode="numeric"
              autoComplete={index === 0 ? "one-time-code" : "off"}
              maxLength={1}
              aria-label={`TOTP digit ${index + 1}`}
              data-filled={Boolean(digit)}
              value={digit}
              onChange={(event) => handleTotpChange(index, event.target.value)}
              onKeyDown={(event) => {
                if (event.key === "Backspace" && !totpCode[index] && index > 0) inputRefs.current[index - 1]?.focus();
              }}
              disabled={isLoading}
            />)}
          </div>

          <div className="login-totp-progress" aria-live="polite"><span style={{ width: `${(totpCode.filter(Boolean).length / 6) * 100}%` }} /><small>{totpCode.filter(Boolean).length}/6 digits</small></div>
          <AnimatePresence initial={false}>{error && <motion.div className="login-error" role="alert" aria-live="polite" initial={{ opacity: 0, y: -5 }} animate={{ opacity: 1, y: 0 }} exit={{ opacity: 0, y: -4 }} transition={{ duration: MOTION_TIMINGS.notice }}><i aria-hidden="true" />{error}</motion.div>}</AnimatePresence>
          {isLoading && <div className="login-verifying"><i className="login-button-spinner" aria-hidden="true" />Verifying code…</div>}
          <button type="button" className="login-secondary-action" aria-label="Back to sign in" onClick={returnToCredentials} disabled={isLoading}>← Back to sign in</button>
        </motion.div>}
      </AnimatePresence>

      <footer className="login-auth-footer"><span><i />Local authentication</span><span>TOTP protected when enabled</span></footer>
    </motion.section>
  </motion.main>;
}
