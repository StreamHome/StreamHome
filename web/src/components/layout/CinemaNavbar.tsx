import React, { useEffect, useState } from 'react';
import { cn } from '../../utils/cn';
import { useProfileStore } from '../../stores/profileStore';

export function CinemaNavbar() {
  const [scrolled, setScrolled] = useState(false);
  const { activeProfile } = useProfileStore();

  useEffect(() => {
    const handleScroll = () => {
      setScrolled(window.scrollY > 10);
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

  const profileColor = activeProfile?.avatar_color || '#E50914';

  return (
    <nav
      className="fixed top-0 w-full h-[68px] z-50 flex items-center justify-between px-12 transition-colors duration-500"
      style={{
        background: scrolled ? '#141414' : 'transparent',
      }}
    >
      {/* Logo */}
      <div className="font-[family-name:var(--font-headline)] text-[var(--text-accent)] text-3xl select-none cursor-pointer tracking-wider pt-1">
        STREAMHOME
      </div>

      {/* Nav Links */}
      <div className="hidden md:flex items-center gap-8 h-full flex-1 ml-12">
        {navLinks.map((link) => (
          <div
            key={link.label}
            className={cn(
              "cursor-pointer transition-colors duration-[var(--duration-fast)]",
              "font-[family-name:var(--font-body)] text-[15px]",
              link.active 
                ? "text-[var(--text-primary)] font-semibold" 
                : "text-[var(--text-secondary)] hover:text-[var(--text-primary)]"
            )}
          >
            {link.label}
          </div>
        ))}
      </div>

      {/* Right Side */}
      <div className="flex items-center gap-6">
        <button className="text-[var(--text-primary)] cursor-pointer">
          <svg width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round">
            <circle cx="11" cy="11" r="8" />
            <path d="M21 21L16.65 16.65" />
          </svg>
        </button>
        <div 
          className="w-9 h-9 rounded-md cursor-pointer"
          style={{ background: `linear-gradient(135deg, ${profileColor}88, ${profileColor})` }}
        />
      </div>
    </nav>
  );
}
