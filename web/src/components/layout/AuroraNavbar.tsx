import React from 'react';
import { cn } from '../../utils/cn';
import { useProfileStore } from '../../stores/profileStore';

export function AuroraNavbar() {
  const { activeProfile } = useProfileStore();

  const navLinks = [
    { label: 'Home', active: true },
    { label: 'Movies', active: false },
    { label: 'Series', active: false },
    { label: 'Downloads', active: false },
  ];

  const profileColor = activeProfile?.avatar_color || '#888888';

  return (
    <nav className="fixed top-[16px] left-1/2 -translate-x-1/2 z-50">
      <div 
        className="flex items-center gap-8 rounded-full px-8 py-2"
        style={{
          background: 'var(--glass-fill)',
          backdropFilter: 'blur(40px)',
          border: '1px solid var(--glass-border)',
        }}
      >
        {/* Logo */}
        <div className="font-[family-name:var(--font-headline)] text-[var(--text-accent)] font-bold tracking-widest text-sm select-none cursor-pointer pr-4">
          STREAMHOME
        </div>

        {/* Nav Links */}
        <div className="hidden md:flex items-center gap-6">
          {navLinks.map((link) => (
            <div
              key={link.label}
              className={cn(
                "cursor-pointer transition-colors duration-[var(--duration-fast)]",
                "font-[family-name:var(--font-body)] text-sm",
                link.active 
                  ? "text-[var(--text-primary)] drop-shadow-[0_0_8px_rgba(255,255,255,0.5)]" 
                  : "text-[var(--text-secondary)] hover:text-[var(--text-primary)]"
              )}
            >
              {link.label}
            </div>
          ))}
        </div>

        {/* Right Side */}
        <div className="flex items-center gap-4 pl-4 border-l border-[var(--border-subtle)]">
          <button className="text-[var(--text-secondary)] hover:text-[var(--text-primary)] transition-colors cursor-pointer">
            <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
              <circle cx="11" cy="11" r="8" />
              <path d="M21 21L16.65 16.65" />
            </svg>
          </button>
          <div 
            className="w-7 h-7 rounded-full cursor-pointer shadow-[0_0_10px_rgba(255,255,255,0.1)]"
            style={{ background: `linear-gradient(135deg, ${profileColor}88, ${profileColor})` }}
          />
        </div>
      </div>
    </nav>
  );
}
