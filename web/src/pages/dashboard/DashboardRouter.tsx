import React from 'react';
import { useThemeStore } from '../../stores/themeStore';
import { EmberDashboard } from './ember/EmberDashboard';
import { AuroraDashboard } from './aurora/AuroraDashboard';
import { CinemaDashboard } from './cinema/CinemaDashboard';
import { GeminiDashboard } from './gemini/GeminiDashboard';

export function DashboardRouter() {
  const { activeTheme } = useThemeStore();

  switch (activeTheme) {
    case 'aurora':
      return <AuroraDashboard />;
    case 'cinema':
      return <CinemaDashboard />;
    case 'gemini':
      return <GeminiDashboard />;
    case 'ember':
    default:
      return <EmberDashboard />;
  }
}
