import React, { useState, useRef, useEffect } from 'react';
import { motion, AnimatePresence } from 'framer-motion';
import { GlassPane } from './GlassPane';
import { cn } from '../../utils/cn';
import { MOTION_EASE, MOTION_TIMINGS, useAppMotion } from '../../motion/motionSystem';

interface Option {
  label: string;
  value: string;
}

interface DropdownProps {
  options: Option[];
  selected: string;
  onChange: (value: string) => void;
  label?: string;
  className?: string;
}

export function Dropdown({ options, selected, onChange, label, className }: DropdownProps) {
  const [isOpen, setIsOpen] = useState(false);
  const { reduced } = useAppMotion();
  const containerRef = useRef<HTMLDivElement>(null);

  const selectedOption = options.find(o => o.value === selected) || options[0];

  useEffect(() => {
    const handleClickOutside = (event: MouseEvent) => {
      if (containerRef.current && !containerRef.current.contains(event.target as Node)) {
        setIsOpen(false);
      }
    };
    document.addEventListener('mousedown', handleClickOutside);
    return () => document.removeEventListener('mousedown', handleClickOutside);
  }, []);

  return (
    <div className={cn("relative flex flex-col gap-1", className)} ref={containerRef}>
      {label && (
        <label className="font-[family-name:var(--font-mono)] text-[12px] tracking-[0.1em] uppercase text-[var(--text-muted)]">
          {label}
        </label>
      )}
      
      <div 
        onClick={() => setIsOpen(!isOpen)}
        className="cursor-pointer"
      >
        <GlassPane 
          className="flex items-center justify-between px-3 h-[40px] select-none"
          spotlight={false}
        >
          <span className="font-[family-name:var(--font-mono)] text-[var(--text-primary)]">
            {selectedOption?.label}
          </span>
          <svg 
            width="12" height="12" viewBox="0 0 12 12" fill="none" 
            className={cn("transition-transform duration-200 text-[var(--text-muted)]", isOpen && "rotate-180")}
          >
            <path d="M2.5 4.5L6 8L9.5 4.5" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round"/>
          </svg>
        </GlassPane>
      </div>

      <AnimatePresence>
        {isOpen && (
          <motion.div
            initial={{ opacity: 0, y: reduced ? 0 : -4, scale: reduced ? 1 : .99 }}
            animate={{ opacity: 1, y: 0, scale: 1, transition: { duration: reduced ? MOTION_TIMINGS.reduced : MOTION_TIMINGS.menuEnter, ease: MOTION_EASE } }}
            exit={{ opacity: 0, y: reduced ? 0 : -3, scale: reduced ? 1 : .995, transition: { duration: reduced ? MOTION_TIMINGS.reduced : MOTION_TIMINGS.menuExit, ease: MOTION_EASE } }}
            className="absolute top-full mt-1 w-full z-50 overflow-hidden"
          >
            <GlassPane spotlight={false} className="py-1 flex flex-col max-h-[250px] overflow-y-auto">
              {options.map((option) => (
                <motion.div
                  key={option.value}
                  onClick={() => {
                    onChange(option.value);
                    setIsOpen(false);
                  }}
                  className={cn(
                    "interaction-option px-3 py-2 cursor-pointer font-[family-name:var(--font-mono)] text-sm",
                    "text-[var(--text-primary)] border-l-[2px] border-transparent",
                    selected === option.value && "border-[var(--glass-border-hover)] bg-[rgba(255,95,31,0.05)] text-[var(--text-accent)]"
                  )}
                >
                  {option.label}
                </motion.div>
              ))}
            </GlassPane>
          </motion.div>
        )}
      </AnimatePresence>
    </div>
  );
}
