import React from "react";

export interface BrandLogoProps {
  className?: string;
  showWordmark?: boolean;
}

export function BrandLogo({ className = "", showWordmark = true }: BrandLogoProps) {
  return <span className={`brand-logo${className ? ` ${className}` : ""}`}>
    <img className="brand-logo__image" src="/logo.png" alt={showWordmark ? "" : "StreamHome"} aria-hidden={showWordmark || undefined} />
    {showWordmark && <span className="brand-logo__wordmark">STREAMHOME</span>}
  </span>;
}
