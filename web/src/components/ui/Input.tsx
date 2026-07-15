import React, { InputHTMLAttributes } from 'react';
import { cn } from '../../utils/cn';

interface InputProps extends InputHTMLAttributes<HTMLInputElement> {
  label?: string;
  error?: string;
}

export const Input = React.forwardRef<HTMLInputElement, InputProps>(({
  label,
  error,
  className,
  ...props
}, ref) => {
  return (
    <div className={cn("flex flex-col gap-1", className)}>
      {label && (
        <label className="font-[family-name:var(--font-mono)] text-[12px] tracking-[0.1em] uppercase text-[var(--text-muted)]">
          {label}
        </label>
      )}
      <input
        ref={ref}
        className={cn(
          "bg-transparent border-0 border-b-[1px] border-b-[rgba(255,255,255,0.2)]",
          "text-[var(--text-primary)] font-[family-name:var(--font-mono)]",
          "px-4 py-3 outline-none",
          "transition-all duration-[var(--duration-fast)] ease-[var(--easing-smooth)]",
          "focus:border-b-2 focus:border-[var(--glass-border-hover)] focus:shadow-[var(--glow-subtle)]",
          error && "border-[var(--text-error)] focus:border-[var(--text-error)]"
        )}
        {...props}
      />
      {error && (
        <span className="text-[var(--text-error)] text-[12px] mt-1">
          {error}
        </span>
      )}
    </div>
  );
});
Input.displayName = 'Input';
