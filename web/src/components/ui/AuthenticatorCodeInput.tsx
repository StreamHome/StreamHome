import React, { useId, useRef } from "react";

interface AuthenticatorCodeInputProps {
  value: string;
  onChange: (value: string) => void;
  label?: string;
  disabled?: boolean;
  error?: string;
  autoFocus?: boolean;
}

/** One real input retains paste, mobile autofill, deletion and keyboard semantics. */
export function AuthenticatorCodeInput({ value, onChange, label = "Authenticator code", disabled, error, autoFocus }: AuthenticatorCodeInputProps) {
  const id = useId();
  const inputRef = useRef<HTMLInputElement>(null);
  return <div className="authenticator-field" data-invalid={Boolean(error)}>
    <label htmlFor={id}>{label}</label>
    <div className="authenticator-code">
      <input ref={inputRef} id={id} type="text" inputMode="numeric" pattern="[0-9]{6}" autoComplete="one-time-code" maxLength={6}
        value={value} disabled={disabled} required autoFocus={autoFocus} aria-invalid={error ? true : undefined}
        aria-describedby={error ? `${id}-error` : `${id}-help`}
        onChange={(event) => onChange(event.target.value.replace(/\D/g, "").slice(0, 6))}
        onFocus={() => inputRef.current?.setSelectionRange(value.length, value.length)} />
      <div className="authenticator-slots" aria-hidden="true">{Array.from({ length: 6 }, (_, index) => <span key={index} data-filled={Boolean(value[index])}>{value[index] || ""}</span>)}</div>
    </div>
    <p id={`${id}-help`}>Enter or paste the six-digit code from your authenticator.</p>
    {error && <p id={`${id}-error`} role="alert">{error}</p>}
  </div>;
}
