import { useEffect, useRef } from 'react';

interface Particle {
  x: number;
  y: number;
  size: number;
  opacity: number;
  speed: number;
}

export function EmberBackground({ suspendWhenHidden = false, respectReducedMotion = false }: { suspendWhenHidden?: boolean; respectReducedMotion?: boolean } = {}) {
  const canvasRef = useRef<HTMLCanvasElement>(null);

  useEffect(() => {
    const canvas = canvasRef.current;
    if (!canvas) return;

    const ctx = canvas.getContext('2d');
    if (!ctx) return;

    let animationFrameId: number;
    let particles: Particle[] = [];

    const resize = () => {
      canvas.width = window.innerWidth;
      canvas.height = window.innerHeight;
      initParticles();
    };

    const initParticles = () => {
      particles = [];
      const particleCount = 60;
      for (let i = 0; i < particleCount; i++) {
        particles.push({
          x: Math.random() * canvas.width,
          y: Math.random() * canvas.height,
          size: Math.random() * 2 + 1, // 1-3px
          opacity: Math.random() * 0.6 + 0.3, // 0.3-0.9
          speed: Math.random() * 0.5 + 0.3 // 0.3-0.8 px/frame
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

      particles.forEach(p => {
        ctx.beginPath();
        ctx.arc(p.x, p.y, p.size, 0, Math.PI * 2);
        ctx.fillStyle = `rgba(255, 95, 31, ${p.opacity})`;
        ctx.fill();

        p.y -= p.speed;

        if (p.y < 0) {
          p.y = canvas.height;
          p.x = Math.random() * canvas.width;
        }
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
        height: '100%'
      }}
    />
  );
}
