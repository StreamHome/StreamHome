import React, { useEffect, useState } from "react";
import { cn } from "../../utils/cn";
import { isServerArtworkUrl } from "../../utils/media";

interface MediaArtworkProps {
  src: string | null | undefined;
  alt: string;
  className?: string;
}

export function MediaArtwork({ src, alt, className }: MediaArtworkProps) {
  const usable = isServerArtworkUrl(src);
  const [failed, setFailed] = useState(false);

  useEffect(() => setFailed(false), [src]);

  if (!usable || failed) {
    return (
      <div
        role="img"
        aria-label={`${alt} artwork unavailable`}
        className={cn("grid place-items-center bg-[var(--bg-surface-container)] text-[var(--text-muted)]", className)}
      >
        <span className="px-4 text-center font-[family-name:var(--font-mono)] text-xs uppercase tracking-wider">
          Artwork unavailable
        </span>
      </div>
    );
  }

  return <img src={src} alt={alt} className={className} onError={() => setFailed(true)} />;
}
