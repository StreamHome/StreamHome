import React from "react";
import { AuthenticationState } from "./AuthenticationState";
import { Navigate } from "react-router-dom";
import { useAuthStore } from "../../stores/authStore";

export function AnonymousOnlyGuard({ children }: { children: React.ReactNode }) {
  const isAuthenticated = useAuthStore((state) => state.isAuthenticated);
  const isHydrated = useAuthStore((state) => state.isHydrated);

  if (!isHydrated) {
    return <AuthenticationState />;
  }
  if (isAuthenticated) {
    return <Navigate to="/profiles" replace />;
  }
  return children;
}
