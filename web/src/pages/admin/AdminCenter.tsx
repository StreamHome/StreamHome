import React, { useState } from "react";
import { useNavigate } from "react-router-dom";
import { Button } from "../../components/ui/Button";
import { AccountPanel } from "./panels/AccountPanel";
import { DownloadsPanel } from "./panels/DownloadsPanel";
import { StoragePanel } from "./panels/StoragePanel";

type Panel = "account" | "storage" | "downloads";

export function AdminCenter() {
  const navigate = useNavigate();
  const [panel, setPanel] = useState<Panel>("account");
  return (
    <div className="min-h-screen bg-[var(--bg-body)] text-[var(--text-primary)]" data-theme="ember">
      <header className="sticky top-0 z-30 border-b border-[var(--glass-border)] bg-[var(--bg-body)]/90 px-6 backdrop-blur-xl"><div className="mx-auto flex min-h-18 max-w-7xl flex-wrap items-center gap-3 py-3"><h1 className="mr-auto text-xl font-semibold">Admin center</h1>{(["account", "storage", "downloads"] as Panel[]).map((item) => <Button key={item} size="sm" variant={panel === item ? "primary" : "ghost"} onClick={() => setPanel(item)}>{item === "account" ? "Account & TOTP" : item === "storage" ? "Storage & HEVC" : "Downloads"}</Button>)}<Button size="sm" variant="secondary" onClick={() => navigate("/")}>Exit</Button></div></header>
      {panel === "account" && <AccountPanel />}{panel === "storage" && <StoragePanel />}{panel === "downloads" && <DownloadsPanel />}
    </div>
  );
}
