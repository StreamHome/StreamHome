import React, { ElementType, useRef, useState, MouseEvent } from 'react';
import { cn } from '../../utils/cn';

interface GlassPaneProps {
  className?: string;
  children?: React.ReactNode;
  spotlight?: boolean;
  as?: ElementType;
  onMouseMove?: (e: MouseEvent<HTMLElement>) => void;
  onMouseEnter?: (e: MouseEvent<HTMLElement>) => void;
  onMouseLeave?: (e: MouseEvent<HTMLElement>) => void;
}

export function GlassPane({
  className,
  children,
  spotlight = true,
  as: Component = 'div',
  onMouseMove,
  onMouseEnter,
  onMouseLeave,
  ...props
}: GlassPaneProps & React.HTMLAttributes<HTMLElement>) {
  const [isHovered, setIsHovered] = useState(false);
  const spotlightRef = useRef<HTMLDivElement>(null);
  const containerRef = useRef<HTMLElement>(null);

  const handleMouseMove = (e: MouseEvent<HTMLElement>) => {
    if (onMouseMove) onMouseMove(e);
    if (spotlight && spotlightRef.current && containerRef.current) {
      const rect = containerRef.current.getBoundingClientRect();
      const x = e.clientX - rect.left;
      const y = e.clientY - rect.top;
      spotlightRef.current.style.left = `${x}px`;
      spotlightRef.current.style.top = `${y}px`;
    }
  };

  const handleMouseEnter = (e: MouseEvent<HTMLElement>) => {
    if (onMouseEnter) onMouseEnter(e);
    setIsHovered(true);
  };

  const handleMouseLeave = (e: MouseEvent<HTMLElement>) => {
    if (onMouseLeave) onMouseLeave(e);
    setIsHovered(false);
  };

  return (
    <Component
      ref={containerRef}
      className={cn(
        'interactive-glass-pane group relative overflow-hidden',
        'bg-[var(--glass-fill)] backdrop-blur-[var(--glass-blur)] border border-[var(--glass-border)]',
        'rounded-[var(--radius)]',
        'shadow-[var(--glow-subtle)]',
        className
      )}
      onMouseMove={handleMouseMove}
      onMouseEnter={handleMouseEnter}
      onMouseLeave={handleMouseLeave}
      {...props}
    >
      {/* Gemini Gradient Border (Pseudo-element equivalent handled via absolute inset div) */}
      <div className="interaction-pane-border absolute inset-0 rounded-[var(--radius)] pointer-events-none opacity-0" style={{
         boxShadow: 'inset 0 0 0 1px var(--glass-border-hover)'
      }} />

      {spotlight && (
        <div
          ref={spotlightRef}
          className="pointer-events-none absolute w-[400px] h-[400px] -translate-x-1/2 -translate-y-1/2 rounded-full"
          style={{
            background: 'var(--spotlight-gradient)',
            opacity: isHovered ? 1 : 0,
            transition: 'opacity 320ms cubic-bezier(.16, 1, .3, 1)',
            zIndex: 0
          }}
        />
      )}
      <div className="relative z-10 w-full h-full">
        {children}
      </div>
    </Component>
  );
}
