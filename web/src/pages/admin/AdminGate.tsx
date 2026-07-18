import React, { useEffect, useState } from "react";
import { AnimatePresence, motion } from "framer-motion";
import { beginReauthentication, getReauthenticationStatus, verifyReauthentication } from "../../api/auth";
import { Button } from "../../components/ui/Button";
import { GlassPane } from "../../components/ui/GlassPane";
import { Input } from "../../components/ui/Input";
import { useThemeStore } from "../../stores/themeStore";
import { getThemeDefinition } from "../../themes/application/themeRegistry";
import { AdminCenter } from "./AdminCenter";
import { MOTION_EASE, MOTION_TIMINGS, useAppMotion } from "../../motion/motionSystem";

export function AdminGate() {
  const theme = useThemeStore((state) => state.activeTheme);
  const definition = getThemeDefinition(theme);
  const Background = definition.Background;
  const [password, setPassword] = useState("");
  const [code, setCode] = useState("");
  const [challengeToken, setChallengeToken] = useState("");
  const [needsCode, setNeedsCode] = useState(false);
  const [authenticated, setAuthenticated] = useState(false);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");
  const { reduced } = useAppMotion();

  useEffect(() => {
    getReauthenticationStatus().then((status) => setAuthenticated(status.reauthenticated)).catch(() => setAuthenticated(false));
  }, []);

  const submit = async (event: React.FormEvent) => {
    event.preventDefault();
    setLoading(true);
    setError("");
    try {
      if (needsCode) {
        if (code.length !== 6) throw new Error("Enter the six-digit TOTP code.");
        await verifyReauthentication({ challengeToken, method: "totp", code });
      } else {
        const response = await beginReauthentication(password);
        if ("requires2fa" in response) {
          setChallengeToken(response.challengeToken);
          setNeedsCode(true);
          setLoading(false);
          return;
        }
      }
      setAuthenticated(true);
    } catch (requestError) {
      setError(requestError instanceof Error ? requestError.message : "Reauthentication failed.");
    } finally {
      setLoading(false);
    }
  };

  if (authenticated) return <motion.div key="admin" initial={{ opacity: 0 }} animate={{ opacity: 1 }} transition={{ duration: reduced ? MOTION_TIMINGS.reduced : MOTION_TIMINGS.viewEnter }}><AdminCenter /></motion.div>;

  return (
    <main className={`theme-app admin-auth-screen ${definition.shellClass}`} data-theme={theme} data-interaction={definition.interaction.id}>
      <Background />
      <motion.div initial={reduced ? { opacity: 0 } : { opacity: 0, y: 24, scale: .97 }} animate={{ opacity: 1, y: 0, scale: 1 }} transition={{ duration: reduced ? MOTION_TIMINGS.reduced : MOTION_TIMINGS.dialogEnter, ease: MOTION_EASE }}><GlassPane className="admin-auth-panel" spotlight={false}>
        <h1 className="text-2xl font-semibold">Admin reauthentication</h1>
        <p className="mt-2 text-sm text-[var(--text-muted)]">Confirm the server account before opening administrative controls.</p>
        <form className="mt-7 flex flex-col gap-4" onSubmit={submit}>
          <AnimatePresence mode="wait" initial={false}>{!needsCode ? <motion.div key="password" initial={{ opacity: 0, x: reduced ? 0 : -12 }} animate={{ opacity: 1, x: 0 }} exit={{ opacity: 0, x: reduced ? 0 : 12 }} transition={{ duration: reduced ? MOTION_TIMINGS.reduced : MOTION_TIMINGS.notice }}><Input label="Password" type="password" value={password} onChange={(event) => setPassword(event.target.value)} required /></motion.div> : <motion.div key="totp" initial={{ opacity: 0, x: reduced ? 0 : 12 }} animate={{ opacity: 1, x: 0 }} exit={{ opacity: 0, x: reduced ? 0 : -12 }} transition={{ duration: reduced ? MOTION_TIMINGS.reduced : MOTION_TIMINGS.notice }}><Input label="TOTP code" inputMode="numeric" maxLength={6} value={code} onChange={(event) => setCode(event.target.value.replace(/\D/g, ""))} required /></motion.div>}</AnimatePresence>
          <AnimatePresence>{error && <motion.p className="text-sm text-[var(--text-error)]" initial={{ opacity: 0, y: -5 }} animate={{ opacity: 1, y: 0 }} exit={{ opacity: 0 }} transition={{ duration: MOTION_TIMINGS.notice }}>{error}</motion.p>}</AnimatePresence>
          <Button type="submit" disabled={loading}>{loading ? "Verifying…" : needsCode ? "Verify TOTP" : "Verify password"}</Button>
        </form>
      </GlassPane></motion.div>
    </main>
  );
}
