import React, { useEffect, useState } from "react";
import { motion } from "framer-motion";
import { useNavigate } from "react-router-dom";
import { getReauthenticationStatus } from "../../api/auth";
import { Button } from "../../components/ui/Button";
import { GlassPane } from "../../components/ui/GlassPane";
import { useThemeStore } from "../../stores/themeStore";
import { useProfileStore } from "../../stores/profileStore";
import { appUrl } from "../../navigation/queryState";
import { getThemeDefinition } from "../../themes/application/themeRegistry";
import { AdminCenter } from "./AdminCenter";
import { ReauthenticationForm } from "./ReauthenticationForm";
import { MOTION_EASE, MOTION_TIMINGS, useAppMotion } from "../../motion/motionSystem";

export function AdminGate() {
  const navigate = useNavigate();
  const profile = useProfileStore((state) => state.activeProfile);
  const theme = useThemeStore((state) => state.activeTheme);
  const definition = getThemeDefinition(theme);
  const Background = definition.Background;
  const [authenticated, setAuthenticated] = useState(false);
  const [checking, setChecking] = useState(true);
  const { reduced } = useAppMotion();

  useEffect(() => {
    let active = true;
    getReauthenticationStatus().then((status) => { if (active) setAuthenticated(status.reauthenticated); })
      .catch(() => { if (active) setAuthenticated(false); })
      .finally(() => { if (active) setChecking(false); });
    return () => { active = false; };
  }, []);

  if (authenticated) {
    return <motion.div key="admin" initial={{ opacity: 0 }} animate={{ opacity: 1 }} transition={{ duration: reduced ? MOTION_TIMINGS.reduced : MOTION_TIMINGS.viewEnter }}><AdminCenter /></motion.div>;
  }

  return <main className={`theme-app admin-auth-screen ${definition.shellClass}`} data-theme={theme} data-interaction={definition.interaction.id}>
    <Background />
    <Button className="admin-auth-back" variant="ghost" onClick={() => navigate(profile ? appUrl(profile.id, "home") : "/profiles")}>← Back to StreamHome</Button>
    <motion.div className="admin-auth-stage" initial={reduced ? { opacity: 0 } : { opacity: 0, y: 16 }} animate={{ opacity: 1, y: 0 }} transition={{ duration: reduced ? MOTION_TIMINGS.reduced : MOTION_TIMINGS.dialogEnter, ease: MOTION_EASE }}>
      <GlassPane className="admin-auth-panel" spotlight={false}>
        <header className="admin-auth-panel__header"><p>ADMIN ACCESS</p><h1>Confirm your identity</h1><span>Confirm access to administrator controls. Follow the verification step below.</span></header>
        {checking ? <p className="admin-auth-checking" role="status">Checking access…</p> : <ReauthenticationForm headingLevel={2} onSuccess={() => setAuthenticated(true)} />}
      </GlassPane>
    </motion.div>
  </main>;
}
