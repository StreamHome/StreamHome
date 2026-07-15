import React from 'react';
import { GeminiBackground } from '../../../themes/gemini/GeminiBackground';
import { GeminiNavbar } from '../../../components/layout/GeminiNavbar';

export function GeminiDashboard() {
  return (
    <div className="relative min-h-screen w-full bg-[var(--bg-body)] text-[var(--text-primary)] flex" data-theme="gemini">
      <GeminiBackground />
      <GeminiNavbar />

      {/* Main content needs left margin to account for the sidebar. 
          Assuming 80px when collapsed and 250px when expanded. 
          For now we'll add a static margin that works, or let the flex layout handle it. 
          Actually GeminiNavbar is fixed, so we add padding-left. */}
      <main className="relative z-10 flex-1 pl-[80px] md:pl-[250px] p-8 transition-all duration-300">
        <h1 className="font-[family-name:var(--font-headline)] text-3xl font-medium tracking-wide">Gemini Workspace Loading...</h1>
      </main>
    </div>
  );
}
