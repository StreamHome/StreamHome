import React, { useEffect, useMemo, useRef, useState } from "react";
import { motion } from "framer-motion";
import { MOTION_EASE, MOTION_TIMINGS, useAppMotion } from "../../motion/motionSystem";
import { cn } from "../../utils/cn";
import { serverArtworkCandidates, type ArtworkEpisodeIdentity, type ArtworkMediaIdentity } from "../../utils/media";

interface MediaArtworkProps {
  src: string | null | undefined;
  alt: string;
  className?: string;
  media?: ArtworkMediaIdentity;
  episode?: ArtworkEpisodeIdentity;
}

const artworkResolutionCache = new Map<string, Promise<string | null>>();

export function clearArtworkResolutionCache(): void {
  artworkResolutionCache.clear();
}

export function resolveArtworkCandidates(candidates: string[]): Promise<string | null> {
  const key = candidates.join("\n");
  const cached = artworkResolutionCache.get(key);
  if (cached) return cached;
  const resolution = (async () => {
    for (const candidate of candidates) {
      try {
        const url = new URL(candidate, window.location.origin);
        if (url.origin !== window.location.origin) return candidate;
        const response = await fetch(candidate, { headers: { Range: "bytes=0-0" }, cache: "force-cache" });
        if (!response.ok) continue;
        await response.body?.cancel().catch(() => undefined);
        return candidate;
      } catch {
        // Missing or temporarily unreachable candidates fall through to the next server path.
      }
    }
    return null;
  })();
  artworkResolutionCache.set(key, resolution);
  return resolution;
}

export function MediaArtwork({ src, alt, className, media, episode }: MediaArtworkProps) {
  const candidates = useMemo(() => {
    const backdrop = Boolean(media && src && (src === media.bannerUrl || src === media.localBannerUrl || src === media.remoteBannerUrl));
    const local = backdrop ? media?.localBannerUrl : media?.localThumbnailUrl;
    const remote = backdrop ? media?.remoteBannerUrl : media?.remoteThumbnailUrl;
    return Array.from(new Set([
      ...(local ? serverArtworkCandidates(local, media, episode) : []),
      ...serverArtworkCandidates(src, media, episode),
      ...(remote ? serverArtworkCandidates(remote, media, episode) : []),
    ].filter(Boolean)));
  }, [episode?.episodeNumber, episode?.seasonNumber, media, src]);
  const candidateKey = candidates.join("\n");
  const [candidateIndex, setCandidateIndex] = useState(0);
  const [resolvedSrc, setResolvedSrc] = useState<string | null | undefined>(() => candidates.length > 1 ? undefined : candidates[0] ?? null);
  const [loaded, setLoaded] = useState(false);
  const [probeVersion, setProbeVersion] = useState(0);
  const [probeAttempt, setProbeAttempt] = useState(0);
  const previousCandidateKey = useRef("");
  const { reduced } = useAppMotion();

  useEffect(() => {
    let active = true;
    let retryTimer: number | undefined;
    const candidateChanged = previousCandidateKey.current !== candidateKey;
    previousCandidateKey.current = candidateKey;
    setCandidateIndex(0);
    if (candidateChanged) setLoaded(false);
    if (candidates.length <= 1) {
      setResolvedSrc(candidates[0] ?? null);
      return () => { active = false; };
    }
    if (candidateChanged) setResolvedSrc(undefined);
    void resolveArtworkCandidates(candidates).then((candidate) => {
      if (!active) return;
      setResolvedSrc(candidate);
      const localPending = candidates[0]?.startsWith("/media/") && candidate !== candidates[0] && media?.cacheState !== "error";
      if (localPending && probeAttempt < 7 && !document.hidden) {
        const delays = [1000, 2000, 4000, 8000, 15000, 30000, 30000];
        retryTimer = window.setTimeout(() => {
          artworkResolutionCache.delete(candidateKey);
          setProbeAttempt((value) => value + 1);
          setProbeVersion((value) => value + 1);
        }, delays[probeAttempt]);
      }
    });
    return () => { active = false; if (retryTimer !== undefined) window.clearTimeout(retryTimer); };
  }, [candidateKey, media?.cacheState, probeAttempt, probeVersion]);

  useEffect(() => { setProbeAttempt(0); }, [candidateKey]);

  const selectedSrc = candidates.length > 1 ? resolvedSrc : candidates[candidateIndex] ?? null;

  if (selectedSrc === undefined) {
    return <div role="status" aria-label={`${alt} artwork loading`} className={cn("media-artwork-loading bg-[var(--bg-surface-container)]", className)} />;
  }

  if (!selectedSrc) {
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

  return <motion.img
    key={selectedSrc}
    src={selectedSrc}
    alt={alt}
    className={className}
    initial={{ opacity: 0, scale: reduced ? 1 : 1.018, filter: reduced ? "none" : "blur(7px)" }}
    animate={{ opacity: loaded ? 1 : 0, scale: 1, filter: "blur(0px)" }}
    transition={{ duration: reduced ? MOTION_TIMINGS.reduced : MOTION_TIMINGS.artwork, ease: MOTION_EASE }}
    onLoad={() => setLoaded(true)}
    onError={() => {
      setLoaded(false);
      if (candidates.length > 1) {
        artworkResolutionCache.delete(candidateKey);
        setResolvedSrc(null);
      } else setCandidateIndex((index) => index + 1);
    }}
  />;
}
