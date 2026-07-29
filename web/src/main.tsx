import React from 'react';
import ReactDOM from 'react-dom/client';
import App from './App';
import { MotionProvider } from './motion/motionSystem';
import './index.css';

async function renderApplication() {
  const visualFixture = import.meta.env.DEV && window.location.pathname === "/__player-visual-fixture";
  const content = visualFixture
    ? React.createElement((await import("./pages/player/PlayerVisualFixture")).PlayerVisualFixture)
    : <App />;

  ReactDOM.createRoot(document.getElementById('root')!).render(
    <React.StrictMode>
      <MotionProvider>{content}</MotionProvider>
    </React.StrictMode>
  );
}

void renderApplication();
