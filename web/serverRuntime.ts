/** Listener policy shared by the packaged server and its focused tests. */
export function webListener(environment: NodeJS.ProcessEnv): { port: number; host: string } {
  if (environment.STREAMHOME_DEPLOYMENT_REVISION !== undefined) {
    const revision = environment.STREAMHOME_DEPLOYMENT_REVISION;
    if (!/^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/.test(revision)
        || revision === "00000000-0000-0000-0000-000000000000") {
      throw new Error("The managed deployment revision is invalid.");
    }
    const rawPort = environment.WEB_PORT ?? "";
    const port = /^[0-9]{1,5}$/.test(rawPort) ? Number(rawPort) : NaN;
    if (!Number.isInteger(port) || port < 1024 || port > 65535 || port === 8000) {
      throw new Error("The managed web port must be an integer from 1024 to 65535, excluding 8000.");
    }
    const host = environment.WEB_BIND;
    if (host !== "127.0.0.1" && host !== "0.0.0.0") {
      throw new Error("The managed web listener address is invalid.");
    }
    return { port, host };
  }
  const requested = Number.parseInt(environment.WEB_PORT ?? "3000", 10);
  return { port: Number.isInteger(requested) && requested >= 1 && requested <= 65535 ? requested : 3000, host: "0.0.0.0" };
}
