import React from 'react';
import { useSearchParams, Navigate } from 'react-router-dom';
import { useTheme } from '../contexts/ThemeContext';

import EmberHome from '../themes/ember/EmberHome';
import AuroraHome from '../themes/aurora/AuroraHome';
import CinemaHome from '../themes/cinema/CinemaHome';
import GeminiHome from '../themes/gemini/GeminiHome';

export default function DashboardRouter() {
  const { theme } = useTheme();
  const [searchParams] = useSearchParams();
  
  const profileId = searchParams.get('profile');
  const view = searchParams.get('view') || 'home';

  if (!profileId) {
    return <Navigate to="/profiles" replace />;
  }

  const renderTheme = () => {
    switch (theme) {
      case 'ember':
        return <EmberHome tab={view} profileId={profileId} />;
      case 'aurora':
        return <AuroraHome tab={view} profileId={profileId} />;
      case 'cinema':
        return <CinemaHome tab={view} profileId={profileId} />;
      case 'gemini':
        return <GeminiHome tab={view} profileId={profileId} />;
      default:
        return <EmberHome tab={view} profileId={profileId} />;
    }
  };

  return (
    <div className="w-full h-full min-h-screen">
      {renderTheme()}
    </div>
  );
}
