import React, { useEffect, useState, useRef } from 'react';
import { motion, AnimatePresence } from 'framer-motion';
import { useLocation, useNavigate } from 'react-router-dom';
import { useAuthStore } from '../stores/authStore';
import { login, verify2FA } from '../api/auth';
import { EmberBackground } from '../themes/ember/EmberBackground';
import { ScanLines } from '../themes/ember/ScanLines';
import { MOTION_EASE, MOTION_TIMINGS, useAppMotion } from '../motion/motionSystem';
import { BrandLogo } from '../components/brand/BrandLogo';

export function LoginPage() {
  const navigate = useNavigate();
  const location = useLocation();
  const setToken = useAuthStore((state) => state.setToken);
  const { reduced } = useAppMotion();

  const [email, setEmail] = useState('');
  const [password, setPassword] = useState('');
  const [error, setError] = useState('');
  const [isLoading, setIsLoading] = useState(false);
  const [isLeaving, setIsLeaving] = useState(false);
  const navigationTimer = useRef<number | null>(null);

  const [requires2FA, setRequires2FA] = useState(false);
  const [totpCode, setTotpCode] = useState<string[]>(Array(6).fill(''));
  const inputRefs = useRef<(HTMLInputElement | null)[]>([]);

  // Spotlight refs
  const [isHovered, setIsHovered] = useState(false);
  const spotlightRef = useRef<HTMLDivElement>(null);
  const containerRef = useRef<HTMLDivElement>(null);

  useEffect(() => () => {
    if (navigationTimer.current !== null) window.clearTimeout(navigationTimer.current);
  }, []);

  const completeLogin = (accessToken: string, accountEmail: string) => {
    setToken(accessToken, accountEmail);
    setIsLeaving(true);
    navigationTimer.current = window.setTimeout(() => navigate('/profiles', { state: location.state }), reduced ? 180 : 680);
  };

  const handleMouseMove = (e: React.MouseEvent<HTMLElement>) => {
    if (spotlightRef.current && containerRef.current) {
      const rect = containerRef.current.getBoundingClientRect();
      const x = e.clientX - rect.left;
      const y = e.clientY - rect.top;
      spotlightRef.current.style.left = `${x}px`;
      spotlightRef.current.style.top = `${y}px`;
    }
  };

  const handleLoginSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    setError('');
    setIsLoading(true);

    try {
      const res = await login({ email, password });
      
      if ("requires2fa" in res) {
        setRequires2FA(true);
      } else {
        completeLogin(res.accessToken, res.email);
      }
    } catch (err: unknown) {
      setError(err instanceof Error ? err.message : 'Login failed');
    } finally {
      setIsLoading(false);
    }
  };

  const handleTotpChange = async (index: number, value: string) => {
    if (!/^\d*$/.test(value)) return;
    
    const newCode = [...totpCode];
    newCode[index] = value;
    setTotpCode(newCode);
    setError('');

    // Auto focus next
    if (value !== '' && index < 5) {
      inputRefs.current[index + 1]?.focus();
    }

    // Auto submit on 6th digit
    if (value !== '' && index === 5 && newCode.every(c => c !== '')) {
      setIsLoading(true);
      try {
        const code = newCode.join('');
        const res = await verify2FA({ email, code });
        completeLogin(res.accessToken, res.email);
      } catch (err: unknown) {
        setError(err instanceof Error ? err.message : 'Invalid 2FA code');
        setTotpCode(Array(6).fill(''));
        inputRefs.current[0]?.focus();
      } finally {
        setIsLoading(false);
      }
    }
  };

  const handleTotpKeyDown = (index: number, e: React.KeyboardEvent) => {
    if (e.key === 'Backspace' && totpCode[index] === '' && index > 0) {
      inputRefs.current[index - 1]?.focus();
    }
  };

  return (
    <motion.div className="login-page relative w-full min-h-screen flex flex-col items-center justify-center overflow-hidden bg-[var(--bg-body)]" data-theme="ember" animate={isLeaving ? { opacity: 0, scale: 1.025, filter: 'blur(10px)' } : { opacity: 1, scale: 1, filter: 'blur(0px)' }} transition={{ duration: reduced ? MOTION_TIMINGS.reduced : .68, ease: MOTION_EASE }}>
      <EmberBackground suspendWhenHidden respectReducedMotion />
      <ScanLines />

      <motion.div
        initial={reduced ? { opacity: 0 } : { opacity: 0, y: 30 }}
        animate={error && !reduced ? { x: [-10, 10, -10, 10, 0], opacity: 1, y: 0 } : { opacity: 1, y: 0, x: 0 }}
        transition={error && !reduced ? { duration: 0.4 } : { duration: reduced ? MOTION_TIMINGS.reduced : 0.8, ease: [0.16, 1, 0.3, 1] }}
        className="relative z-10 w-[calc(100%-32px)] max-w-[500px] mx-auto"
      >
        {/* HERO SECTION - Extracted outside the box! */}
        <section className="mb-8 flex flex-col items-start gap-4">
          <div 
            className="inline-flex items-center gap-2 border border-[#ffb59c] rounded-sm text-[#ffb59c] font-[family-name:var(--font-mono)] text-[12px] uppercase tracking-[0.1em] font-medium"
            style={{ padding: '4px 12px' }}
          >
            <span className="w-2 h-2 rounded-full bg-[#ffb59c] animate-pulse shrink-0"></span>
            AUTH REQUIRED
          </div>
          <h1 className="font-[family-name:var(--font-headline)] text-white text-5xl md:text-6xl tracking-[0.02em] font-bold select-none min-h-[60px]">
            <BrandLogo className="brand-logo--login" />
          </h1>
          <p className="font-[family-name:var(--font-mono)] text-[var(--text-muted)] text-[12px] tracking-[0.2em] uppercase mt-2 opacity-80">
            Secure Terminal Access
          </p>
        </section>

        {/* GLASS PANE - Now only wraps the form! */}
        <div 
          ref={containerRef}
          onMouseMove={handleMouseMove}
          onMouseEnter={() => setIsHovered(true)}
          onMouseLeave={() => setIsHovered(false)}
          className="relative w-full rounded-lg border border-[rgba(255,95,31,0.15)] bg-[rgba(30,16,11,0.4)] backdrop-blur-[12px] overflow-hidden shadow-2xl"
          style={{ padding: '40px' }}
        >
          {/* Spotlight Effect */}
          <div 
            ref={spotlightRef}
            className="absolute pointer-events-none w-[400px] h-[400px] -translate-x-1/2 -translate-y-1/2 rounded-full z-0"
            style={{ 
              background: 'radial-gradient(circle, rgba(255, 95, 31, 0.4) 0%, transparent 70%)',
              opacity: isHovered ? 1 : 0,
              transition: 'opacity 300ms ease'
            }}
          />

          <div className="relative z-10 w-full h-full">
            <AnimatePresence mode="wait">
              {!requires2FA ? (
                <motion.form
                  key="login-form"
                  exit={reduced ? { opacity: 0 } : { opacity: 0, x: -30 }}
                  transition={{ duration: reduced ? MOTION_TIMINGS.reduced : 0.3 }}
                  onSubmit={handleLoginSubmit}
                  className="flex flex-col gap-8"
                >
                  <div className="flex flex-col gap-3">
                    <label className="font-[family-name:var(--font-mono)] text-[12px] tracking-[0.1em] uppercase text-[var(--text-muted)] opacity-90 pl-1 font-medium">
                      Email Address
                    </label>
                    <div className="relative group">
                      <input
                        type="email"
                        value={email}
                        onChange={(e) => setEmail(e.target.value)}
                        required
                        disabled={isLoading}
                        className="w-full bg-[rgba(0,0,0,0.2)] border border-[rgba(255,255,255,0.05)] text-white font-[family-name:var(--font-mono)] text-[16px] h-[60px] outline-none transition-all duration-300 focus:bg-[rgba(255,95,31,0.03)] focus:border-[#f97316] focus:shadow-[0_0_15px_rgba(255,95,31,0.15)] rounded-sm"
                        style={{ paddingLeft: '24px', paddingRight: '24px' }}
                        placeholder="operator@streamhome.local"
                      />
                    </div>
                  </div>
                  
                  <div className="flex flex-col gap-3">
                    <label className="font-[family-name:var(--font-mono)] text-[12px] tracking-[0.1em] uppercase text-[var(--text-muted)] opacity-90 pl-1 font-medium">
                      Master Password
                    </label>
                    <div className="relative group">
                      <input
                        type="password"
                        value={password}
                        onChange={(e) => setPassword(e.target.value)}
                        required
                        disabled={isLoading}
                        className="w-full bg-[rgba(0,0,0,0.2)] border border-[rgba(255,255,255,0.05)] text-white font-[family-name:var(--font-mono)] text-[16px] h-[60px] outline-none transition-all duration-300 focus:bg-[rgba(255,95,31,0.03)] focus:border-[#f97316] focus:shadow-[0_0_15px_rgba(255,95,31,0.15)] rounded-sm"
                        style={{ paddingLeft: '24px', paddingRight: '24px' }}
                        placeholder="••••••••••••"
                      />
                    </div>
                  </div>
                  
                  {error && (
                    <motion.div 
                      initial={reduced ? { opacity: 0 } : { opacity: 0, y: -5 }} animate={{ opacity: 1, y: 0 }}
                      className="inline-flex items-center gap-2 border border-[#ffb4ab] rounded-sm text-[#ffb4ab] font-[family-name:var(--font-mono)] text-[12px] tracking-[0.1em]"
                      style={{ padding: '4px 12px' }}
                    >
                      <span className="w-2 h-2 rounded-full bg-[#ffb4ab] animate-pulse shrink-0"></span>
                      <span>{error}</span>
                    </motion.div>
                  )}

                  <div className="pt-2">
                    <button 
                      type="submit" 
                      disabled={isLoading} 
                      className="w-full relative z-10 h-[64px] mt-4 bg-[rgba(30,16,11,0.4)] backdrop-blur-[12px] border border-[#f97316] shadow-[0_0_10px_rgba(255,95,31,0.5)] text-white font-[family-name:var(--font-mono)] text-[16px] tracking-[0.1em] font-medium rounded hover:shadow-[0_0_15px_rgba(255,95,31,0.8)] hover:scale-[1.02] active:scale-[0.98] transition-all duration-300 disabled:opacity-50 disabled:cursor-not-allowed uppercase"
                    >
                      <span className="relative z-10 font-bold">
                        {isLoading ? 'Authenticating...' : 'Initialize Connection'}
                      </span>
                    </button>
                  </div>
                </motion.form>
              ) : (
                <motion.div
                  key="totp-form"
                  initial={reduced ? { opacity: 0 } : { opacity: 0, x: 30 }}
                  animate={{ opacity: 1, x: 0 }}
                  transition={{ duration: reduced ? MOTION_TIMINGS.reduced : 0.4, ease: [0.16, 1, 0.3, 1] }}
                  className="flex flex-col items-center justify-center gap-10 py-6"
                >
                  <div className="text-center space-y-2">
                    <p className="font-[family-name:var(--font-mono)] text-[var(--text-accent)] text-sm uppercase tracking-widest animate-pulse">
                      Identity Verification
                    </p>
                    <p className="font-[family-name:var(--font-mono)] text-[var(--text-secondary)] text-[12px] uppercase tracking-[0.1em] opacity-60">
                      Enter 6-digit TOTP token
                    </p>
                  </div>
                  
                  <div className="flex gap-4">
                    {totpCode.map((digit, i) => (
                      <input
                        key={i}
                        ref={(el) => { inputRefs.current[i] = el; }}
                        type="text"
                        maxLength={1}
                        value={digit}
                        onChange={(e) => handleTotpChange(i, e.target.value)}
                        onKeyDown={(e) => handleTotpKeyDown(i, e)}
                        disabled={isLoading}
                        className="w-12 h-16 md:w-14 md:h-16 text-center text-2xl font-[family-name:var(--font-mono)] bg-[rgba(0,0,0,0.4)] border border-[var(--border-surface)] text-white outline-none transition-all duration-[var(--duration-fast)] focus:border-[#f97316] focus:shadow-[0_0_15px_rgba(255,95,31,0.3)] focus:bg-[rgba(255,95,31,0.05)] rounded-sm"
                      />
                    ))}
                  </div>

                  {error && (
                    <div className="text-[#ffb4ab] text-[13px] font-[family-name:var(--font-mono)] border border-[#ffb4ab] bg-[rgba(255,0,0,0.05)] px-4 py-3 rounded-sm w-full text-center">
                      {error}
                    </div>
                  )}

                  <button 
                    onClick={() => { setRequires2FA(false); setTotpCode(Array(6).fill('')); setError(''); }}
                    disabled={isLoading}
                    className="mt-4 font-[family-name:var(--font-mono)] text-[11px] tracking-[0.1em] uppercase text-[var(--text-muted)] hover:text-white transition-colors duration-[var(--duration-fast)] border-b border-transparent hover:border-white pb-1"
                  >
                    Abort Connection
                  </button>
                </motion.div>
              )}
            </AnimatePresence>
          </div>
        </div>
      </motion.div>
    </motion.div>
  );
}
