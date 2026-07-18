import React from "react";
import { useNavigate } from "react-router-dom";
import { Button } from "../../../components/ui/Button";
import { GlassPane } from "../../../components/ui/GlassPane";
import { appUrl } from "../../../navigation/queryState";

interface AccountPanelProps {
  profileId: string;
}

export function AccountPanel({ profileId }: AccountPanelProps) {
  const navigate = useNavigate();
  const openSecurity = () => navigate(appUrl(profileId, "admin", { section: "security" }));

  return (
    <section className="admin-panel admin-panel--account">
      <header className="admin-panel__header">
        <p>IDENTITY / ACCESS</p>
        <h1>Account and Security</h1>
        <span>Manage authentication, recovery access, signed-in devices, and security history.</span>
      </header>
      <GlassPane className="admin-card admin-security-card" spotlight={false}>
        <div className="admin-card__icon" aria-hidden="true"><span>01</span></div>
        <div className="admin-card__copy">
          <p>SERVER ACCOUNT PROTECTION</p>
          <h2>Security controls stay behind recent authentication.</h2>
          <span>Open the dedicated security workspace to manage TOTP, recovery codes, active sessions, and the account audit trail.</span>
        </div>
        <Button className="admin-card__action" onClick={openSecurity}>Open Account Security</Button>
      </GlassPane>
    </section>
  );
}
