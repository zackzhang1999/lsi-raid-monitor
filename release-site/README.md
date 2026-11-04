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

The default target is the write-only `/opt/lsi-full` publishing account on
`hub.amax.xyz`. The static service listens on TCP 9004.

```bash
bash publish.sh
```

`DATAMAX_RELEASE_TARGET` and `DATAMAX_RELEASE_SSH_KEY` may be overridden for
another host. The publishing key is restricted by `rrsync -wo` to the release
directory and cannot install system packages or start application services.

The public package intentionally does not contain Broadcom `storcli64`.
Provide a licensed copy through `install.sh --storcli-source`.
