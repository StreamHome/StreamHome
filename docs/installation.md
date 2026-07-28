# Installing StreamHome

The supported alpha server target is a current Linux distribution with one of the package managers recognized by `setup.sh`. The repository lifecycle scripts own direct start and stop operations; Windows server entry points are not included.

## Install StreamHome

```bash
curl -fsSL https://raw.githubusercontent.com/StreamHome/StreamHome/main/install.sh | bash
```

The installer:

1. installs Git when necessary;
2. acquires a single-installation lock;
3. clones the current `main` branch into a temporary same-parent directory and promotes a complete checkout atomically;
4. verifies the checkout resolves exactly to the fetched commit;
5. runs `setup.sh`;
6. installs pinned Python, npm, and application-owned Rclone dependencies;
7. builds the production web client and starts it only after health checks pass.

Use a different installation directory only when needed:

```bash
STREAMHOME_INSTALL_DIR=/srv/streamhome \
curl -fsSL https://raw.githubusercontent.com/StreamHome/StreamHome/main/install.sh | bash
```

To install dependencies and assets without starting the services:

```bash
curl -fsSL https://raw.githubusercontent.com/StreamHome/StreamHome/main/install.sh \
  | bash -s -- --no-start
```

The default installer starts StreamHome. With `--no-start`, run `./start.sh` later. Startup prints the setup URL and one-time bootstrap code only after both the loopback API and public web service are healthy.

## Required ports

- TCP 3000: browser client by default
- TCP 8000: loopback API used by the local web proxy

`start.sh` binds port 8000 explicitly to `127.0.0.1`; only the configured web port listens publicly. Put HTTPS in front of the web port when the server is reachable from the public internet.

## Unsupported alpha environments

Windows server operation, containers, Kubernetes, NAS application stores, and automatic multi-node deployment are not release-gated for the current alpha.
