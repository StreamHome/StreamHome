import React, { useEffect } from 'react';
import { BrowserRouter, Routes, Route } from 'react-router-dom';
import { useAuthStore } from './stores/authStore';
import './themes/index';

import { LoginPage } from './pages/LoginPage';
import { ProfileSelectPage } from './pages/ProfileSelectPage';
import { ProfileEditPage } from './pages/ProfileEditPage';
import { AuthenticatedApp } from './pages/AuthenticatedApp';
import { AccountSecurityPage } from './pages/AccountSecurityPage';

import { AuthGuard } from './components/guards/AuthGuard';
import { QueryProfileGuard } from './components/guards/QueryProfileGuard';
import { ErrorBoundary } from './components/ui/ErrorBoundary';
import { AppFallbackRedirect, LegacyAdminRedirect, LegacyWatchRedirect } from './navigation/LegacyRedirects';

export default function App() {
  const hydrate = useAuthStore((state) => state.hydrate);

  useEffect(() => {
    hydrate();
  }, [hydrate]);

  return (
    <ErrorBoundary>
    <BrowserRouter>
      <Routes>
        <Route path="/login" element={<LoginPage />} />
        
        <Route path="/profiles" element={
          <AuthGuard>
            <ProfileSelectPage />
          </AuthGuard>
        } />

        <Route path="/profiles/:profileId/edit" element={
          <AuthGuard>
            <ProfileEditPage />
          </AuthGuard>
        } />

        <Route path="/account/security" element={<AuthGuard><AccountSecurityPage /></AuthGuard>} />
        
        <Route path="/watch/:mediaId" element={
          <AuthGuard>
            <LegacyWatchRedirect />
          </AuthGuard>
        } />
        
        <Route path="/admin/*" element={
          <AuthGuard>
            <LegacyAdminRedirect />
          </AuthGuard>
        } />

        <Route path="/" element={
          <AuthGuard>
            <QueryProfileGuard>
              <AuthenticatedApp />
            </QueryProfileGuard>
          </AuthGuard>
        } />

        <Route path="*" element={<AuthGuard><AppFallbackRedirect /></AuthGuard>} />
        
      </Routes>
    </BrowserRouter>
    </ErrorBoundary>
  );
}
