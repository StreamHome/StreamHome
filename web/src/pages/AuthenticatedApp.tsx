import React, { useMemo } from "react";
import { useLocation } from "react-router-dom";
import { parseAppQuery } from "../navigation/queryState";
import { AdminGate } from "./admin/AdminGate";
import { DashboardRouter } from "./dashboard/DashboardRouter";
import { PlayerPage } from "./player/PlayerPage";

export function AuthenticatedApp() {
  const location = useLocation();
  const query = useMemo(() => parseAppQuery(location.search), [location.search]);
  if (query.view === "watch") return <PlayerPage />;
  if (query.view === "admin") return <AdminGate />;
  return <DashboardRouter />;
}
