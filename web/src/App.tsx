import React, { lazy, Suspense, useEffect } from 'react';
import { BrowserRouter, Routes, Route } from 'react-router-dom';
import { useAuthStore } from './stores/authStore';
import './themes/index';

import { AuthGuard } from './components/guards/AuthGuard';
import { QueryProfileGuard } from './components/guards/QueryProfileGuard';
import { ErrorBoundary } from './components/ui/ErrorBoundary';
import { SetupStateGate } from './components/guards/SetupStateGate';
import { AppFallbackRedirect, LegacyAccountSecurityRedirect, LegacyAdminRedirect, LegacyWatchRedirect } from './navigation/LegacyRedirects';

const LoginPage = lazy(() => import('./pages/LoginPage').then((module) => ({ default: module.LoginPage })));
const ProfileSelectPage = lazy(() => import('./pages/ProfileSelectPage').then((module) => ({ default: module.ProfileSelectPage })));
const ProfileEditPage = lazy(() => import('./pages/ProfileEditPage').then((module) => ({ default: module.ProfileEditPage })));
const AuthenticatedApp = lazy(() => import('./pages/AuthenticatedApp').then((module) => ({ default: module.AuthenticatedApp })));
const SetupPage = lazy(() => import('./pages/SetupPage').then((module) => ({ default: module.SetupPage })));

function RouteChunkFallback() {
  return <main aria-busy="true" aria-live="polite" style={{ minHeight: '100dvh', background: '#0b0807', color: '#f4ebe7', display: 'grid', placeItems: 'center', fontFamily: 'system-ui, sans-serif' }}>Opening StreamHome...</main>;
}

export default function App() {
  const hydrate = useAuthStore((state) => state.hydrate);

  useEffect(() => {
    hydrate();
  }, [hydrate]);

  return (
    <ErrorBoundary>
    <BrowserRouter>
      <SetupStateGate>
      <Suspense fallback={<RouteChunkFallback />}><Routes>
        <Route path="/setup" element={<SetupPage />} />
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

        <Route path="/account/security" element={<AuthGuard><LegacyAccountSecurityRedirect /></AuthGuard>} />
        
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
        
      </Routes></Suspense>
      </SetupStateGate>
    </BrowserRouter>
    </ErrorBoundary>
  );
}
