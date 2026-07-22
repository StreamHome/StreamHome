import { defineConfig, loadEnv } from 'vite';
import react from '@vitejs/plugin-react';
import tailwindcss from '@tailwindcss/vite';
import path from 'path';

export default defineConfig(({ mode }) => {
  const env = loadEnv(mode, path.resolve(__dirname, '..'), '');
  const parsedPort = Number.parseInt(env.WEB_PORT || '3000', 10);
  const webPort = Number.isInteger(parsedPort) && parsedPort >= 1 && parsedPort <= 65535 ? parsedPort : 3000;
  return {
    define: {
      'import.meta.env.VITE_APP_VERSION': JSON.stringify(process.env.npm_package_version ?? '0.0.0'),
      'import.meta.env.VITE_BUILD_ID': JSON.stringify(env.VITE_BUILD_ID || process.env.VITE_BUILD_ID || 'dev'),
    },
    plugins: [react(), tailwindcss()],
    resolve: {
      alias: {
        '@': path.resolve(__dirname, './src'),
      },
    },
    server: {
      port: webPort,
      proxy: {
        '/api': 'http://127.0.0.1:8000',
        '/media': 'http://127.0.0.1:8000',
      },
    },
    build: {
      // hls.js is a deferred player-only vendor chunk; keep the warning focused on
      // accidental application bundles rather than this intentionally isolated engine.
      chunkSizeWarningLimit: 550,
      rollupOptions: {
        output: {
          manualChunks(id) {
            if (id.includes('/node_modules/hls.js/')) return 'vendor-hls';
            if (id.includes('/node_modules/framer-motion/') || id.includes('/node_modules/motion-dom/') || id.includes('/node_modules/motion-utils/')) return 'vendor-motion';
            if (id.includes('/node_modules/react/') || id.includes('/node_modules/react-dom/') || id.includes('/node_modules/react-router') || id.includes('/node_modules/scheduler/')) return 'vendor-react';
            return undefined;
          },
        },
      },
    },
  };
});
