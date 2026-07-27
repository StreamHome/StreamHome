# Installing StreamHome v0.1.0-alpha.1

The supported alpha server target is a current Linux distribution with `systemd`-style administration and one of the package managers recognized by `setup.sh`.

## Install the tagged release

```bash
curl -fsSL https://raw.githubusercontent.com/WaqSea/StreamHome/v0.1.0-alpha.1/install.sh | bash
```

The installer:

1. installs Git when necessary;
2. checks out the immutable `v0.1.0-alpha.1` tag into `~/StreamHome`;
3. runs `setup.sh`;
4. installs pinned Python and npm dependencies;
5. builds the production web client.

Use a different installation directory only when needed:

```bash
STREAMHOME_INSTALL_DIR=/srv/streamhome \
curl -fsSL https://raw.githubusercontent.com/WaqSea/StreamHome/v0.1.0-alpha.1/install.sh | bash
```

After installation:

```bash
cd ~/StreamHome
./start.sh
```

Open the printed setup URL and enter the bootstrap code printed by `start.sh`.

## Required ports

- TCP 3000: browser client by default
- TCP 8000: loopback API used by the local web proxy

Do not expose port 8000 directly. Put HTTPS in front of the web port when the server is reachable from the public internet.

## Unsupported alpha environments

Windows server operation, containers, Kubernetes, NAS application stores, and automatic multi-node deployment are not release-gated for `v0.1.0-alpha.1`.
