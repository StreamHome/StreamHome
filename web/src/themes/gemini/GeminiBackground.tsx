import { useEffect, useRef } from 'react';

interface Star {
  x: number;
  y: number;
  size: number;
  baseOpacity: number;
  speed: number;
  timeOffset: number;
}

interface GlowParticle {
  x: number;
  y: number;
  size: number;
  color: string;
  speed: number;
  timeOffset: number;
}

export function GeminiBackground({ suspendWhenHidden = true, respectReducedMotion = true }: { suspendWhenHidden?: boolean; respectReducedMotion?: boolean } = {}) {
  const canvasRef = useRef<HTMLCanvasElement>(null);

  useEffect(() => {
    const canvas = canvasRef.current;
    if (!canvas) return;

    const ctx = canvas.getContext('2d');
    if (!ctx) return;

    let animationFrameId: number;
    let stars: Star[] = [];
    let glows: GlowParticle[] = [];
    let time = 0;

    const resize = () => {
      canvas.width = window.innerWidth;
      canvas.height = window.innerHeight;
      initParticles();
    };

    const initParticles = () => {
      stars = [];
      glows = [];
      const starCount = 120;
      
      for (let i = 0; i < starCount; i++) {
        stars.push({
          x: Math.random() * canvas.width,
          y: Math.random() * canvas.height,
          size: Math.random() * 1 + 1, // 1-2px
          baseOpacity: Math.random() * 0.6 + 0.1, // 0.1-0.7
          speed: Math.random() * 0.02 + 0.005,
          timeOffset: Math.random() * Math.PI * 2
        });
      }

      const glowColors = ['#4285F4', '#9b72cb'];
      for (let i = 0; i < 5; i++) {
        glows.push({
          x: Math.random() * canvas.width,
          y: Math.random() * canvas.height,
          size: Math.random() * 200 + 100, // Large blobs
          color: glowColors[i % glowColors.length],
          speed: Math.random() * 0.01 + 0.005,
          timeOffset: Math.random() * Math.PI * 2
        });
      }
    };

    const reducedMotion = respectReducedMotion && window.matchMedia("(prefers-reduced-motion: reduce)").matches;
    const draw = () => {
      if (suspendWhenHidden && document.hidden) {
        animationFrameId = requestAnimationFrame(draw);
        return;
      }
      ctx.clearRect(0, 0, canvas.width, canvas.height);
      time += 1;

      // Draw glows
      glows.forEach(g => {
        const pulse = (Math.sin(time * g.speed + g.timeOffset) + 1) / 2;
        const gradient = ctx.createRadialGradient(g.x, g.y, 0, g.x, g.y, g.size);
        
        // Ensure color includes opacity
        const rgb = g.color === '#4285F4' ? '66, 133, 244' : '155, 114, 203';
        gradient.addColorStop(0, `rgba(${rgb}, ${0.1 * pulse})`);
        gradient.addColorStop(1, `rgba(${rgb}, 0)`);
        
        ctx.fillStyle = gradient;
        ctx.beginPath();
        ctx.arc(g.x, g.y, g.size, 0, Math.PI * 2);
        ctx.fill();
      });

      // Draw stars
      stars.forEach(s => {
        const pulse = (Math.sin(time * s.speed + s.timeOffset) + 1) / 2;
        const currentOpacity = s.baseOpacity * (0.5 + 0.5 * pulse);
        
        ctx.beginPath();
        ctx.arc(s.x, s.y, s.size, 0, Math.PI * 2);
        ctx.fillStyle = `rgba(255, 255, 255, ${currentOpacity})`;
        ctx.fill();
      });

      if (!reducedMotion) animationFrameId = requestAnimationFrame(draw);
    };

    window.addEventListener('resize', resize);
    resize();
    draw();

    return () => {
      window.removeEventListener('resize', resize);
      if (animationFrameId) cancelAnimationFrame(animationFrameId);
    };
  }, [respectReducedMotion, suspendWhenHidden]);

  return (
    <canvas
      ref={canvasRef}
      style={{
        position: 'fixed',
        inset: 0,
        zIndex: 0,
        pointerEvents: 'none',
        width: '100%',
        height: '100%',
        background: '#09090b'
      }}
    />
  );
}
