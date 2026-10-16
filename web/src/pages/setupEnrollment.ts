import { useCallback, useEffect, useRef, useState } from "react";
import { ApiError } from "../api/client";
import { beginSetupTOTP, cancelSetupTOTP, verifySetupTOTP, type SetupTOTPEnrollment } from "../api/setup";

export type EnrollmentState = { phase: "idle" | "generating" | "ready" | "verifying" | "verified" | "error" | "expired" | "unknown"; enrollment?: SetupTOTPEnrollment; message?: string };
const unknownMessage = "The earlier server operation may still finish. Its outcome is unknown. Start a new enrollment explicitly and verify it before continuing.";
export function isUnknownSetupOutcome(reason: unknown): boolean {
  return !(reason instanceof ApiError) || reason.status === 0 || reason.status >= 500;
}
export function isRetiredSetupEnrollment(reason: unknown): boolean {
  return reason instanceof ApiError && ([404, 410].includes(reason.status) || ["totp_enrollment_not_found", "totp_enrollment_expired", "totp_email_changed", "totp_not_verified"].includes(reason.code));
}

/** Local serialization orders acknowledged operations only; it cannot order lost server work. */
export function useSetupEnrollment(email: string, frozen: boolean) {
  const [enabled, setEnabledState] = useState(false);
  const [state, setState] = useState<EnrollmentState>({ phase: "idle" });
  const stateRef = useRef(state);
  const enabledRef = useRef(false);
  const emailRef = useRef(email.trim().toLowerCase());
  const generation = useRef(0);
  const mounted = useRef(true);
  const queue = useRef(Promise.resolve());
  const uncertain = useRef(false);
  const locked = useRef(false);
  const frozenRef = useRef(frozen);
  frozenRef.current = frozen;
  const update = useCallback((next: EnrollmentState) => { stateRef.current = next; if (mounted.current) setState(next); }, []);
  const serialize = useCallback((action: () => Promise<void>) => { queue.current = queue.current.then(action, action); return queue.current; }, []);
  const retire = useCallback((next: EnrollmentState = { phase: "idle" }) => {
    const old = stateRef.current.enrollment;
    generation.current += 1;
    update(next);
    if (old) void serialize(async () => {
      try { await cancelSetupTOTP(old.enrollmentId); }
      catch (reason) { if (isUnknownSetupOutcome(reason)) { uncertain.current = true; update({ phase: "unknown", message: unknownMessage }); } }
    });
  }, [serialize, update]);
  useEffect(() => { mounted.current = true; return () => { mounted.current = false; generation.current += 1; }; }, []);
  useEffect(() => {
    const normalized = email.trim().toLowerCase();
    if (normalized !== emailRef.current && !frozenRef.current) { emailRef.current = normalized; retire(uncertain.current ? { phase: "unknown", message: unknownMessage } : { phase: "idle" }); }
  }, [email, retire]);
  useEffect(() => {
    if (!enabled || !state.enrollment || frozen) return;
    const expire = () => { if (stateRef.current.enrollment && Date.now() >= stateRef.current.enrollment.expiresAt * 1000) { generation.current += 1; update({ phase: "expired", message: "This enrollment expired. Generate a new enrollment and verify its code." }); } };
    expire();
    const timer = window.setInterval(expire, 1000);
    return () => window.clearInterval(timer);
  }, [enabled, frozen, state.enrollment, update]);
  const generate = useCallback(() => {
    if (frozenRef.current || locked.current || !enabledRef.current) return;
    locked.current = true;
    const owner = ++generation.current;
    const ownerEmail = emailRef.current;
    const old = stateRef.current.enrollment;
    const explicitUnknownReplacement = stateRef.current.phase === "unknown";
    update({ phase: "generating" });
    void serialize(async () => {
      try {
        if (uncertain.current && !explicitUnknownReplacement) { update({ phase: "unknown", message: unknownMessage }); return; }
        if (old) await cancelSetupTOTP(old.enrollmentId);
        if (owner !== generation.current || !mounted.current) return;
        const enrollment = await beginSetupTOTP(ownerEmail);
        if (owner !== generation.current || !mounted.current || !enabledRef.current) {
          try { await cancelSetupTOTP(enrollment.enrollmentId); }
          catch (reason) { if (isUnknownSetupOutcome(reason)) { uncertain.current = true; update({ phase: "unknown", message: unknownMessage }); } }
          return;
        }
        uncertain.current = false;
        update(Date.now() >= enrollment.expiresAt * 1000 ? { phase: "expired", message: "This enrollment expired. Generate a new enrollment." } : { phase: "ready", enrollment });
      } catch (reason) {
        if (isUnknownSetupOutcome(reason)) { uncertain.current = true; if (mounted.current) update({ phase: "unknown", message: unknownMessage }); }
        else if (owner === generation.current && mounted.current) update({ phase: "error", message: reason instanceof Error ? reason.message : "Enrollment could not be created." });
      } finally { locked.current = false; }
    });
  }, [serialize, update]);
  const choose = useCallback((next: boolean) => {
    if (frozenRef.current || next === enabledRef.current) return;
    enabledRef.current = next;
    setEnabledState(next);
    if (!next) retire(uncertain.current ? { phase: "unknown", message: unknownMessage } : { phase: "idle" });
    else if (uncertain.current) update({ phase: "unknown", message: unknownMessage });
    else generate();
  }, [generate, retire, update]);
  const verify = useCallback((code: string) => {
    const current = stateRef.current;
    if (frozenRef.current || locked.current || !enabledRef.current || !current.enrollment || !["ready", "verified"].includes(current.phase) || !/^\d{6}$/.test(code)) return;
    if (Date.now() >= current.enrollment.expiresAt * 1000) { update({ phase: "expired", message: "This enrollment expired. Generate a new enrollment." }); return; }
    const owner = generation.current;
    locked.current = true;
    update({ ...current, phase: "verifying", message: undefined });
    void serialize(async () => {
      try {
        await verifySetupTOTP(current.enrollment!.enrollmentId, code);
        if (owner === generation.current && mounted.current) update({ phase: "verified", enrollment: current.enrollment });
      } catch (reason) {
        if (owner !== generation.current || !mounted.current) return;
        if (isUnknownSetupOutcome(reason)) { uncertain.current = true; update({ phase: "unknown", message: unknownMessage }); }
        else if (isRetiredSetupEnrollment(reason)) update({ phase: "expired", message: "This enrollment is no longer available. Generate a new enrollment and verify it." });
        else update({ ...current, phase: "ready", message: reason instanceof Error ? reason.message : "The code was not accepted." });
      } finally { locked.current = false; }
    });
  }, [serialize, update]);
  const clearVerification = () => { if (!frozenRef.current && stateRef.current.phase === "verified") update({ ...stateRef.current, phase: "ready" }); };
  const rejectCompletion = () => { generation.current += 1; update({ phase: "expired", message: "Setup could not use this enrollment. Generate a new enrollment and verify it." }); };
  return { enabled, state, choose, generate, verify, clearVerification, rejectCompletion };
}
