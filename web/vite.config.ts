import { defineConfig, loadEnv } from 'vite';
import react from '@vitejs/plugin-react';
import tailwindcss from '@tailwindcss/vite';
import path from 'path';
import { createReadStream, existsSync, statSync } from 'node:fs';
import { execFileSync } from 'node:child_process';

function resolveBuildId(configuredBuildId: string): string {
  if (configuredBuildId && configuredBuildId !== 'dev') return configuredBuildId;
  try {
    return execFileSync('git', ['rev-parse', '--short=12', 'HEAD'], {
      cwd: path.resolve(__dirname, '..'),
      encoding: 'utf8',
      stdio: ['ignore', 'pipe', 'ignore'],
    }).trim() || 'dev';
  } catch {
    return 'dev';
  }
}

export default defineConfig(({ mode }) => {
  const env = loadEnv(mode, path.resolve(__dirname, '..'), '');
  const parsedPort = Number.parseInt(env.WEB_PORT || '3000', 10);
  const webPort = Number.isInteger(parsedPort) && parsedPort >= 1 && parsedPort <= 65535 ? parsedPort : 3000;
  const buildId = resolveBuildId(process.env.VITE_BUILD_ID || process.env.STREAMHOME_BUILD_ID || env.VITE_BUILD_ID || 'dev');
  return {
    define: {
      'import.meta.env.VITE_APP_VERSION': JSON.stringify(process.env.npm_package_version ?? '0.0.0'),
      'import.meta.env.VITE_BUILD_ID': JSON.stringify(buildId),
    },
    plugins: [
      react(),
      tailwindcss(),
      {
        name: 'streamhome-build-identity',
        generateBundle() {
          this.emitFile({
            type: 'asset',
            fileName: '.streamhome-build',
            source: `${buildId}\n`,
          });
        },
      },
      {
        name: 'streamhome-player-visual-fixture',
        apply: 'serve',
        configureServer(server) {
          server.middlewares.use('/__player-visual-fixture', (request, response, next) => {
            const relative = decodeURIComponent((request.url || '').split('?')[0]).replace(/^\/+/, '');
            const asset = path.resolve(__dirname, 'test-assets/player-dual-audio-hls', relative);
            const root = path.resolve(__dirname, 'test-assets/player-dual-audio-hls');
            if (!asset.startsWith(`${root}${path.sep}`) || !existsSync(asset) || !statSync(asset).isFile()) {
              next();
              return;
            }
            response.statusCode = 200;
            response.setHeader(
              'Content-Type',
              asset.endsWith('.m3u8')
                ? 'application/vnd.apple.mpegurl'
                : asset.endsWith('.mp4') || asset.endsWith('.m4s') ? 'video/mp4' : 'application/octet-stream',
            );
            response.setHeader('Cache-Control', 'no-store');
            createReadStream(asset).pipe(response);
          });
          server.middlewares.use('/__player-visual-fixture.mp4', (request, response) => {
            const asset = path.resolve(__dirname, 'test-assets/player-visual-fixture.mp4');
            const size = statSync(asset).size;
            const range = request.headers.range?.match(/^bytes=(\d*)-(\d*)$/);
            response.setHeader('Content-Type', 'video/mp4');
            response.setHeader('Cache-Control', 'no-store');
            response.setHeader('Accept-Ranges', 'bytes');
            if (range) {
              const start = range[1] ? Number.parseInt(range[1], 10) : 0;
              const requestedEnd = range[2] ? Number.parseInt(range[2], 10) : size - 1;
              const end = Math.min(size - 1, requestedEnd);
              if (!Number.isInteger(start) || !Number.isInteger(end) || start < 0 || start > end || start >= size) {
                response.statusCode = 416;
                response.setHeader('Content-Range', `bytes */${size}`);
                response.end();
                return;
              }
              response.statusCode = 206;
              response.setHeader('Content-Range', `bytes ${start}-${end}/${size}`);
              response.setHeader('Content-Length', String(end - start + 1));
              if (request.method === 'HEAD') response.end();
              else createReadStream(asset, { start, end }).pipe(response);
              return;
            }
            response.statusCode = 200;
            response.setHeader('Content-Length', String(size));
            if (request.method === 'HEAD') response.end();
            else createReadStream(asset).pipe(response);
          });
        },
      },
    ],
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
