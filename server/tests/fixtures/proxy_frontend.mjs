import { createRequire } from "node:module";
import { createTrustedProxy } from "../../../web/proxySecurity.ts";

const express = createRequire(new URL("../../../web/package.json", import.meta.url))("express");

const app = express();
app.use("/api", createTrustedProxy(`${process.argv[2]}/api`, process.argv[3] ?? ""));
const server = app.listen(0, "127.0.0.1", () => {
  console.log(JSON.stringify({ port: server.address().port }));
});
process.on("SIGTERM", () => server.close(() => process.exit(0)));
