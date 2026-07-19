import express from "express";
import path from "path";
import { fileURLToPath } from "url";
import { readFileSync } from "fs";
import { createServer as createViteServer } from "vite";
import { createProxyMiddleware } from "http-proxy-middleware";



async function startServer() {
  const app = express();
  const requestedPort = Number.parseInt(process.env.WEB_PORT ?? "3000", 10);
  const PORT = Number.isInteger(requestedPort) && requestedPort >= 1 && requestedPort <= 65535 ? requestedPort : 3000;

  // Proxy /api and /media requests to the live Python FastAPI backend
  app.use("/api", createProxyMiddleware({
    target: "http://127.0.0.1:8000/api",
    changeOrigin: true
  }));
  app.use("/media", createProxyMiddleware({
    target: "http://127.0.0.1:8000/media",
    changeOrigin: true
  }));

  // Setup basic middlewares
  app.use(express.json());



  // Vite middleware setup
  // `npm run dev` owns Vite development. The packaged Express entrypoint is
  // production-first so a missing inherited NODE_ENV can never expose a dev server.
  const isProd = process.env.NODE_ENV !== "development" || process.argv[1]?.endsWith("server.cjs");
  
  if (!isProd) {
    const vite = await createViteServer({
      server: { middlewareMode: true },
      appType: "spa",
    });
    app.use(vite.middlewares);
  } else {
    const distPath = path.join(path.dirname(fileURLToPath(import.meta.url)), "dist");
    console.log(`[StreamHome Server] Serving web assets from ${distPath}`);
    const indexDocument = readFileSync(path.join(distPath, "index.html"), "utf8");
    app.use(express.static(distPath));
    app.use((req, res, next) => {
      if (req.method !== "GET") return next();
      res.type("html").send(indexDocument);
    });
  }

  app.listen(PORT, "0.0.0.0", () => {
    console.log(`[StreamHome Server] Running at http://localhost:${PORT}`);
  });
}

startServer().catch((err) => {
  console.error("Failed to start StreamHome full-stack server:", err);
});
