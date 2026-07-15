import React, { useState } from 'react';
import { EmberBackground } from '../../../themes/ember/EmberBackground';
import { ScanLines } from '../../../themes/ember/ScanLines';
import { EmberNavbar } from '../../../components/layout/EmberNavbar';
import { EmberHome } from './EmberHome';
import { EmberMovies } from './EmberMovies';
import { EmberSeries } from './EmberSeries';
import { EmberDownloads } from './EmberDownloads';

export function EmberDashboard() {
  const [activeTab, setActiveTab] = useState<"home" | "movies" | "series" | "downloads">("home");

  // In a real app we would pass activeTab and setActiveTab to EmberNavbar,
  // but EmberNavbar currently hardcodes the tabs. We will keep it simple here.

  return (
    <div className="relative min-h-screen w-full bg-[var(--bg-body)] text-[var(--text-primary)]" data-theme="ember">
      <EmberBackground />
      <ScanLines />
      <EmberNavbar />

      <main className="relative z-10 pt-[64px]">
        {activeTab === "home" && <EmberHome />}
        {activeTab === "movies" && <EmberMovies />}
        {activeTab === "series" && <EmberSeries />}
        {activeTab === "downloads" && <EmberDownloads />}
      </main>
    </div>
  );
}
