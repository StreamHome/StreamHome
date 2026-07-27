# Updating StreamHome

`v0.1.0-alpha.1` installations are pinned to their release tag. Do not silently convert a release installation to the moving `main` branch.

Before updating:

1. create and verify a database backup;
2. stop ingestion and media-processing work;
3. read the target release notes;
4. confirm the checkout has no local changes.

To install a later tagged release:

```bash
cd ~/StreamHome
git fetch --tags origin
git checkout --detach vNEXT
./setup.sh --no-start
./start.sh
```

Replace `vNEXT` with the exact published tag. Run the database checker and perform login, ingestion, and playback smoke tests after updating.

Automatic updates are experimental during the alpha and should remain disabled on installations that require controlled maintenance windows.
