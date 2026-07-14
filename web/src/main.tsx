import {StrictMode} from 'react';
import {createRoot} from 'react-dom/client';
import './index.css';
import { registerSW } from 'virtual:pwa-register';

registerSW({ immediate: true });

createRoot(document.getElementById('root')!).render(
  <StrictMode>
    <div id="app-root" className="min-h-screen bg-black text-white p-8">
      <h1>StreamHome Redesign Mode</h1>
      <p>Frontend UI wiped.</p>
    </div>
  </StrictMode>,
);
