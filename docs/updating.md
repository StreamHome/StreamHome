# Updating StreamHome

`v0.1.0-alpha.1` installations are pinned to their release tag. Do not silently convert a release installation to the moving `main` branch.

Before updating:

1. create and verify a database backup;
2. stop ingestion and media-processing work;
3. run `./stop.sh` and confirm it reports a clean shutdown;
4. read the target release notes;
5. confirm the checkout has no local changes.

To install a later tagged release:

```bash
cd ~/StreamHome
./stop.sh
git fetch --tags origin
git checkout --detach vNEXT
./setup.sh --no-start
./test.sh
./start.sh
```

Replace `vNEXT` with the exact published tag. Setup also stops an owned runtime defensively before replacing dependencies or production assets. Release checks require the runtime to remain stopped because they use the canonical database and rebuild `web/dist`. Perform login, ingestion, and playback smoke tests after startup.

Automatic updates are experimental during the alpha and should remain disabled on installations that require controlled maintenance windows.
