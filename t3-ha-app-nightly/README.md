# T3 Code (Nightly)

Nightly build of the T3 Code CLI, packaged as a companion to the stable **T3 Code** add-on.

## One shared data directory

This add-on does **not** have its own private data. Both add-ons store all persistent state in `/share/t3-ha-app-data` and link it into `/data`. Sessions, credentials, the Home Assistant skill, persistent tools, overlays and the T3 Connect relay state are therefore identical in the stable and nightly add-ons. Only `options.json` is private, since each add-on has different ports and settings.

Run **one add-on at a time** — they share the same T3 state database. The nightly CLI binary comes from its own image, so switching between the add-ons does not replace the stable version pin.

## Ports

- LAN / companion app: `3775` (configurable)
- Ingress: `62902`
- Internal: API `62907`, ttyd `62908`

Images are built in CI only. The nightly is rebuilt daily with the newest `T3CODE_CHANNEL=nightly` release and published as `ghcr.io/marcohald/t3-ha-app-nightly`.
