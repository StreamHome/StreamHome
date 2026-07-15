import React from 'react';
import { AuroraBackground } from '../../../themes/aurora/AuroraBackground';
import { AuroraNavbar } from '../../../components/layout/AuroraNavbar';

export function AuroraDashboard() {
  return (
    <div className="relative min-h-screen w-full bg-[var(--bg-body)] text-[var(--text-primary)]" data-theme="aurora">
      <AuroraBackground />
      <AuroraNavbar />

      <main className="relative z-10 pt-[100px] px-[var(--spacing-margin-desktop)]">
        <h1 className="font-[family-name:var(--font-headline)] text-4xl mb-4 text-[var(--text-primary)] drop-shadow-[0_0_15px_rgba(255,255,255,0.8)]">Aurora Dashboard</h1>
        <p className="font-[family-name:var(--font-body)] text-[var(--text-secondary)]">Masonry Grid Architecture Loading...</p>
      </main>
    </div>
  );
}
