import { useEffect } from "react";
import { useAuthStore } from "../../stores/authStore";

/** The application root owns startup reads, including StrictMode retirement. */
export function useSessionHydration() {
  const hydrate = useAuthStore((state) => state.hydrate);
  useEffect(() => {
    void hydrate();
    return () => useAuthStore.getState().cancelHydration();
  }, [hydrate]);
}
