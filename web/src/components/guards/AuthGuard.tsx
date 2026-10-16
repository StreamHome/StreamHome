import React from "react";
import { AuthenticationState } from "./AuthenticationState";
import { Navigate, useLocation } from "react-router-dom";
import { useAuthStore } from "../../stores/authStore";

export function AuthGuard({ children }: { children: React.ReactNode }) {
  const isAuthenticated = useAuthStore((state) => state.isAuthenticated);
  const isHydrated = useAuthStore((state) => state.isHydrated);
  const location = useLocation();

  if (!isHydrated) {
    return <AuthenticationState />;
  }
  if (!isAuthenticated) {
    return <Navigate to="/login" state={{ from: location }} replace />;
  }
  return children;
}
