import React, { useEffect, useState } from 'react';
import { cn } from '../../utils/cn';
import { useProfileStore } from '../../stores/profileStore';

export function EmberNavbar() {
  const [scrolled, setScrolled] = useState(false);
  const { activeProfile } = useProfileStore();

  useEffect(() => {
    const handleScroll = () => {
      setScrolled(window.scrollY > 50);
    };
    window.addEventListener('scroll', handleScroll);
    return () => window.removeEventListener('scroll', handleScroll);
  }, []);

  const navLinks = [
    { label: 'Home', active: true },
    { label: 'Movies', active: false },
    { label: 'Series', active: false },
    { label: 'Downloads', active: false },
  ];

  const profileColor = activeProfile?.avatar_color || '#ff5f1f';

  return (
    <nav
      className="fixed top-0 w-full h-[64px] z-50 flex items-center justify-between px-[var(--spacing-margin-desktop)] transition-all duration-300"
      style={{
        background: scrolled ? 'rgba(30, 16, 11, 0.85)' : 'rgba(30, 16, 11, 0.6)',
        backdropFilter: scrolled ? 'blur(20px)' : 'blur(12px)',
        borderBottom: '1px solid rgba(255, 95, 31, 0.15)',
      }}
    >
      {/* Logo */}
      <div className="font-[family-name:var(--font-mono)] text-[var(--text-accent)] tracking-[0.2em] font-bold text-lg select-none cursor-pointer">
        STREAMHOME
      </div>

      {/* Nav Links */}
      <div className="hidden md:flex items-center gap-8 h-full">
        {navLinks.map((link) => (
          <div
            key={link.label}
            className={cn(
              "h-full flex items-center cursor-pointer transition-colors duration-[var(--duration-fast)]",
              "font-[family-name:var(--font-mono)] text-sm tracking-wide border-b-2",
              link.active 
                ? "text-[var(--accent-container)] border-[var(--accent-container)]" 
                : "text-[var(--text-secondary)] border-transparent hover:text-[var(--text-accent)]"
            )}
          >
            {link.label}
          </div>
        ))}
      </div>

      {/* Right Side */}
      <div className="flex items-center gap-6">
        <button className="text-[var(--text-secondary)] hover:text-[var(--text-accent)] transition-colors cursor-pointer">
          <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
            <circle cx="11" cy="11" r="8" />
            <path d="M21 21L16.65 16.65" />
          </svg>
        </button>
        <div 
          className="w-8 h-8 rounded-full border border-white/20 cursor-pointer shadow-[0_0_10px_rgba(0,0,0,0.5)]"
          style={{ background: `linear-gradient(135deg, ${profileColor}88, ${profileColor})` }}
        />
      </div>
    </nav>
  );
}
