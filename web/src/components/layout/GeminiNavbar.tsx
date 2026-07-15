import React, { useState } from 'react';
import { cn } from '../../utils/cn';
import { useProfileStore } from '../../stores/profileStore';
import { motion } from 'framer-motion';

export function GeminiNavbar() {
  const [collapsed, setCollapsed] = useState(false);
  const { activeProfile } = useProfileStore();

  const navLinks = [
    { label: 'Home', active: true },
    { label: 'Movies', active: false },
    { label: 'Series', active: false },
    { label: 'Downloads', active: false },
  ];

  const profileColor = activeProfile?.avatar_color || '#4285F4';

  return (
    <>
      <motion.nav
        initial={false}
        animate={{ width: collapsed ? 80 : 250 }}
        className="fixed left-0 top-0 bottom-0 z-50 flex flex-col"
        style={{
          background: 'var(--glass-fill)',
          backdropFilter: 'blur(var(--glass-blur))',
          borderRight: '1px solid var(--glass-border)',
        }}
      >
        <div className="flex items-center justify-between p-6 h-[80px]">
          {!collapsed && (
            <div className="font-[family-name:var(--font-headline)] font-bold tracking-widest text-[var(--text-accent)] select-none">
              STREAM
            </div>
          )}
          <button 
            onClick={() => setCollapsed(!collapsed)}
            className="text-[var(--text-secondary)] hover:text-[var(--text-primary)] transition-colors mx-auto cursor-pointer"
          >
            <svg width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
              <line x1="3" y1="12" x2="21" y2="12" />
              <line x1="3" y1="6" x2="21" y2="6" />
              <line x1="3" y1="18" x2="21" y2="18" />
            </svg>
          </button>
        </div>

        <div className="flex-1 flex flex-col gap-4 px-4 pt-8">
          {navLinks.map((link) => (
            <div
              key={link.label}
              className={cn(
                "flex items-center cursor-pointer rounded-xl px-4 py-3 transition-colors duration-[var(--duration-fast)]",
                "font-[family-name:var(--font-body)] font-medium",
                link.active 
                  ? "bg-[rgba(255,255,255,0.05)] text-[var(--text-accent)] shadow-[inset_2px_0_0_var(--text-accent)]" 
                  : "text-[var(--text-secondary)] hover:bg-[rgba(255,255,255,0.02)] hover:text-[var(--text-primary)]",
                collapsed && "justify-center px-0 shadow-none"
              )}
            >
              <div className="w-6 h-6 rounded-full bg-white/10" />
              {!collapsed && <span className="ml-4">{link.label}</span>}
            </div>
          ))}
        </div>

        <div className="p-6 border-t border-[var(--border-subtle)] flex items-center gap-4">
          <div 
            className="w-10 h-10 rounded-full cursor-pointer flex-shrink-0"
            style={{ background: `linear-gradient(135deg, ${profileColor}88, ${profileColor})` }}
          />
          {!collapsed && (
            <div className="flex flex-col flex-1 truncate">
              <span className="text-sm font-medium text-[var(--text-primary)] truncate">
                {activeProfile?.name || 'User'}
              </span>
              <span className="text-xs text-[var(--text-muted)] truncate">Profile</span>
            </div>
          )}
        </div>
      </motion.nav>
    </>
  );
}
