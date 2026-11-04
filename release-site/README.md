# DataMax-LSI release site

This directory contains the static release page, the full usage manual and the
release packaging tool.

## Build

```bash
python3 build_release.py --source .. --notes "release notes"
```

The generated `datamax-lsi-<version>.tar.gz` stays in this directory and is
ignored by Git. `releases.json` is updated with its SHA-256 and byte size.

## Publish

The target directory is `/opt/lsi-full` on the hub server. The static service
listens on TCP 9004.

```bash
DATAMAX_RELEASE_TARGET=root@hub.amax.xyz:/opt/lsi-full \
DATAMAX_RELEASE_SSH_KEY=/root/.ssh/datamax-release_ed25519 \
bash publish.sh
```

The public package intentionally does not contain Broadcom `storcli64`.
Provide a licensed copy through `install.sh --storcli-source`.
