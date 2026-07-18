import React from "react";
import { useLocation, useNavigate } from "react-router-dom";
import { Button } from "../../../components/ui/Button";
import { GlassPane } from "../../../components/ui/GlassPane";

export function AccountPanel() {
  const navigate = useNavigate();
  const location = useLocation();
  return <section className="mx-auto max-w-3xl p-8"><h1 className="text-3xl font-semibold">Account and Security</h1><p className="mt-2 text-[var(--text-muted)]">Manage TOTP, recovery access, devices, and security history on the dedicated account page.</p><GlassPane className="mt-8 p-7" spotlight={false}><h2 className="text-xl font-semibold">Server account protection</h2><p className="mt-2 text-sm text-[var(--text-muted)]">Sensitive controls require recent server-side reauthentication.</p><Button className="mt-6" onClick={() => navigate("/account/security", { state: { returnTo: `${location.pathname}${location.search}${location.hash}` } })}>Open Account Security</Button></GlassPane></section>;
}
