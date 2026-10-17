import { useEffect, useRef } from "react";

/** One read chain at a time. A retired visit/visibility epoch cannot publish data. */
export function useStorageAutoRead(active: boolean, paused: boolean, epoch: string | number, read: (signal: AbortSignal) => Promise<number>) {
  const readRef = useRef(read);
  readRef.current = read;
  useEffect(() => {
    if (!active || paused) return;
    let retired = false;
    let controller: AbortController | null = null;
    let timer: ReturnType<typeof setTimeout> | undefined;
    let failures = 0;
    const visible = () => !retired && document.visibilityState !== "hidden";
    const cancel = () => {
      clearTimeout(timer);
      controller?.abort();
      controller = null;
    };
    const run = async () => {
      if (!visible() || controller) return;
      clearTimeout(timer);
      const owner = new AbortController();
      controller = owner;
      let delay = 15000;
      try {
        delay = await readRef.current(owner.signal);
        failures = 0;
      } catch {
        failures += 1;
        delay = Math.min(60000, 3000 * 2 ** Math.min(failures - 1, 5));
      } finally {
        if (controller === owner) {
          controller = null;
          if (visible() && !owner.signal.aborted) timer = setTimeout(() => void run(), delay);
        }
      }
    };
    const visibility = () => { if (visible()) void run(); else cancel(); };
    const focus = () => { if (visible()) void run(); };
    document.addEventListener("visibilitychange", visibility);
    window.addEventListener("focus", focus);
    void run();
    return () => {
      retired = true;
      cancel();
      document.removeEventListener("visibilitychange", visibility);
      window.removeEventListener("focus", focus);
    };
  }, [active, paused, epoch]);
}
