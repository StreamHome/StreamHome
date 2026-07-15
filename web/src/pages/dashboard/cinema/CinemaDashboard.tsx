import React from 'react';
import { CinemaBackground } from '../../../themes/cinema/CinemaBackground';
import { CinemaNavbar } from '../../../components/layout/CinemaNavbar';

export function CinemaDashboard() {
  return (
    <div className="relative min-h-screen w-full bg-[var(--bg-body)] text-[var(--text-primary)]" data-theme="cinema">
      <CinemaBackground />
      <CinemaNavbar />

      <main className="relative z-10 pt-[68px]">
        <div className="h-[60vh] flex items-center justify-center bg-black/50">
          <h1 className="font-[family-name:var(--font-headline)] text-5xl font-bold tracking-wider">Classic Cinema Layout Loading...</h1>
        </div>
      </main>
    </div>
  );
}
