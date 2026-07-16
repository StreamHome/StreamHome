import React from 'react';
import { cn } from '../../utils/cn';

interface ButtonProps extends React.ButtonHTMLAttributes<HTMLButtonElement> {
  variant?: "primary" | "secondary" | "ghost";
  size?: "sm" | "md" | "lg";
}

export function Button({
  variant = "primary",
  size = "md",
  className,
  children,
  disabled,
  ...props
}: ButtonProps) {
  
  const baseClasses = "interaction-button inline-flex items-center justify-center font-[family-name:var(--font-mono)] uppercase tracking-[0.1em] cursor-pointer select-none rounded-[var(--radius)] disabled:opacity-50 disabled:cursor-not-allowed";
  
  const sizeClasses = {
    sm: "px-3 py-1.5 text-xs",
    md: "px-6 py-2.5 text-sm",
    lg: "px-8 py-3.5 text-base"
  };
  
  const variantClasses = {
    primary: "bg-[var(--glass-fill)] border border-[var(--glass-border-hover)] text-[var(--text-accent)]",
    secondary: "bg-transparent border border-[var(--glass-border)] text-white",
    ghost: "bg-transparent text-[var(--text-secondary)] border border-transparent"
  };

  return (
    <button
      className={cn(baseClasses, sizeClasses[size], variantClasses[variant], className)}
      data-interaction-variant={variant}
      disabled={disabled}
      {...props}
    >
      {children}
    </button>
  );
}
