import React, { useEffect, useRef } from 'react';
import { motion, AnimatePresence } from 'framer-motion';
import { GlassPane } from './GlassPane';
import { cn } from '../../utils/cn';
import { MOTION_EASE, MOTION_TIMINGS, useAppMotion } from '../../motion/motionSystem';

interface ModalProps {
  isOpen: boolean;
  onClose: () => void;
  children: React.ReactNode;
  className?: string;
  ariaLabel?: string;
}

export function Modal({ isOpen, onClose, children, className, ariaLabel = 'Dialog' }: ModalProps) {
  const { reduced } = useAppMotion();
  const dialogRef = useRef<HTMLDivElement>(null);
  const closeRef = useRef(onClose);
  closeRef.current = onClose;
  useEffect(() => {
    if (!isOpen) return;
    const previousFocus = document.activeElement instanceof HTMLElement && !dialogRef.current?.contains(document.activeElement) ? document.activeElement : null;
    const previousOverflow = document.body.style.overflow;
    document.body.style.overflow = 'hidden';
    const focusable = () => Array.from(dialogRef.current?.querySelectorAll<HTMLElement>('button:not(:disabled), [href], input:not(:disabled), select:not(:disabled), textarea:not(:disabled), [tabindex]:not([tabindex="-1"])') ?? []).filter((element) => !element.closest('[hidden]'));
    (focusable()[0] ?? dialogRef.current)?.focus();
    const handleKeyDown = (event: KeyboardEvent) => {
      if (event.key === 'Escape') { event.preventDefault(); closeRef.current(); }
      if (event.key !== 'Tab') return;
      const items = focusable();
      const first = items[0];
      const last = items[items.length - 1];
      if (!first) { event.preventDefault(); dialogRef.current?.focus(); }
      else if (event.shiftKey && (document.activeElement === first || !dialogRef.current?.contains(document.activeElement))) { event.preventDefault(); last.focus(); }
      else if (!event.shiftKey && (document.activeElement === last || !dialogRef.current?.contains(document.activeElement))) { event.preventDefault(); first.focus(); }
    };
    window.addEventListener('keydown', handleKeyDown);
    return () => {
      window.removeEventListener('keydown', handleKeyDown);
      document.body.style.overflow = previousOverflow;
      if (previousFocus?.isConnected) previousFocus.focus();
    };
  }, [isOpen]);

  return <AnimatePresence>{isOpen && <div className="fixed inset-0 z-[100] flex items-center justify-center">
    <motion.div aria-hidden="true" initial={{ opacity: 0 }} animate={{ opacity: 1 }} exit={{ opacity: 0 }} transition={{ duration: reduced ? MOTION_TIMINGS.reduced : MOTION_TIMINGS.dialogEnter }} className="absolute inset-0 bg-black/60 backdrop-blur-[8px]" onClick={onClose} />
    <motion.div ref={dialogRef} role="dialog" aria-modal="true" aria-label={ariaLabel} tabIndex={-1}
      initial={reduced ? { opacity: 0 } : { opacity: 0, y: 10, scale: .985 }}
      animate={{ opacity: 1, y: 0, scale: 1, transition: { duration: reduced ? MOTION_TIMINGS.reduced : MOTION_TIMINGS.dialogEnter, ease: MOTION_EASE } }}
      exit={reduced ? { opacity: 0 } : { opacity: 0, y: 6, scale: .992, transition: { duration: MOTION_TIMINGS.dialogExit, ease: MOTION_EASE } }}
      className="relative z-10 w-full max-w-lg mx-4 max-h-[90dvh] overflow-y-auto">
      <GlassPane className={cn('p-6', className)}>{children}</GlassPane>
    </motion.div>
  </div>}</AnimatePresence>;
}
