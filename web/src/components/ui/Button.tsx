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
  
  const baseClasses = "inline-flex items-center justify-center font-[family-name:var(--font-mono)] uppercase tracking-[0.1em] transition-all cursor-pointer select-none rounded-[var(--radius)] disabled:opacity-50 disabled:cursor-not-allowed";
  
  const sizeClasses = {
    sm: "px-3 py-1.5 text-xs",
    md: "px-6 py-2.5 text-sm",
    lg: "px-8 py-3.5 text-base"
  };
  
  const variantClasses = {
    primary: "bg-[var(--glass-fill)] border border-[var(--glass-border-hover)] text-[var(--text-accent)] hover:bg-[var(--accent-container)] hover:text-[var(--accent-on-primary)] hover:shadow-[var(--glow-intense)] duration-[var(--duration-fast)] ease-[var(--easing-smooth)]",
    secondary: "bg-transparent border border-[var(--glass-border)] text-white hover:border-[var(--glass-border-hover)] duration-[var(--duration-fast)] ease-[var(--easing-smooth)]",
    ghost: "bg-transparent text-[var(--text-secondary)] hover:text-white hover:bg-[var(--glass-fill)] border border-transparent duration-[var(--duration-fast)] ease-[var(--easing-smooth)]"
  };

  return (
    <button
      className={cn(baseClasses, sizeClasses[size], variantClasses[variant], className)}
      disabled={disabled}
      {...props}
    >
      {children}
    </button>
  );
}
