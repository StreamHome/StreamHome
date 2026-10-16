import React, { useRef } from 'react';
import { useDialogLifecycle } from './useDialogLifecycle';
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
  useDialogLifecycle({ open: isOpen, dialogRef, onClose });

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
