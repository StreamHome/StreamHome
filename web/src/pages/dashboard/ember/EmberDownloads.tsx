import React from 'react';
import { GlassPane } from '../../../components/ui/GlassPane';
import { ProgressBar } from '../../../components/ui/ProgressBar';
import { ProgressRing } from '../../../components/ui/ProgressRing';

export function EmberDownloads() {
  return (
    <div className="w-full min-h-screen pb-20 px-[var(--spacing-margin-desktop)] pt-8">
      <h1 className="font-[family-name:var(--font-headline)] text-[var(--text-primary)] text-3xl font-bold tracking-wide mb-8">
        Local Storage
      </h1>
      
      {/* Storage Bar */}
      <GlassPane className="p-6 mb-12">
        <div className="flex justify-between font-[family-name:var(--font-mono)] text-sm mb-4">
          <span className="text-[var(--text-primary)]">DEVICE STORAGE</span>
          <span className="text-[var(--text-secondary)]">45.2 GB / 128 GB USED</span>
        </div>
        <ProgressBar progress={0.35} />
      </GlassPane>

      <h2 className="font-[family-name:var(--font-headline)] text-[var(--text-primary)] text-xl font-semibold tracking-wide mb-6">
        Active Transfers
      </h2>
      
      {/* Active Transfer Cards */}
      <div className="flex flex-col gap-4 mb-12">
        {[1, 2].map((i) => (
          <GlassPane key={i} className="p-4 flex items-center justify-between">
            <div className="flex items-center gap-6">
              <div className="w-16 h-24 bg-[rgba(255,255,255,0.05)] rounded-md"></div>
              <div>
                <h3 className="font-[family-name:var(--font-body)] text-[var(--text-primary)] text-lg">
                  Inception
                </h3>
                <div className="font-[family-name:var(--font-mono)] text-[var(--text-muted)] text-xs mt-1">
                  DOWNLOADING • 12 MB/s
                </div>
              </div>
            </div>
            <div className="flex items-center gap-6 pr-4">
              <div className="font-[family-name:var(--font-mono)] text-[var(--accent-container)] text-xl">
                68%
              </div>
              <ProgressRing progress={0.68} size={48} strokeWidth={4} />
            </div>
          </GlassPane>
        ))}
      </div>

      <h2 className="font-[family-name:var(--font-headline)] text-[var(--text-primary)] text-xl font-semibold tracking-wide mb-6">
        Offline Library
      </h2>
      
      {/* Offline Grid (Stubbed) */}
      <div className="grid grid-cols-2 md:grid-cols-4 lg:grid-cols-6 gap-6">
        {[1, 2, 3].map(i => (
          <div key={i} className="aspect-[2/3] rounded-[var(--radius)] bg-[rgba(255,255,255,0.05)] flex items-center justify-center font-[family-name:var(--font-mono)] text-[var(--text-muted)] text-xs">
            SAVED
          </div>
        ))}
      </div>
    </div>
  );
}
