import React from "react";
import { Navigate } from "react-router-dom";
import { useAuthStore } from "../../stores/authStore";

export function AnonymousOnlyGuard({ children }: { children: React.ReactNode }) {
  const isAuthenticated = useAuthStore((state) => state.isAuthenticated);
  const isHydrated = useAuthStore((state) => state.isHydrated);

  if (!isHydrated) {
    return <div className="min-h-screen bg-[#0a0a0a]" aria-label="Loading authentication" />;
  }
  if (isAuthenticated) {
    return <Navigate to="/profiles" replace />;
  }
  return children;
}
