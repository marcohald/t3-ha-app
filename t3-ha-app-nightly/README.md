# T3 Code (Nightly)

Nightly build of the T3 Code CLI, packaged as a companion to the stable **T3 Code** add-on.

## One shared data directory

This add-on does **not** have its own private data. Both add-ons store all persistent state in `/share/t3-ha-app-data` and link it into `/data`. Sessions, credentials, the Home Assistant skill, persistent tools, overlays and the T3 Connect relay state are therefore identical in the stable and nightly add-ons. Only `options.json` is private, since each add-on has its own configuration.

Run **one add-on at a time** — they share the same T3 state database *and the same ports*, so the nightly is a drop-in replacement for the stable add-on: stop one, start the other. The nightly CLI binary comes from its own image, so switching does not replace the stable version pin.

## Ports

The nightly uses the **same defaults as the stable add-on**:

- LAN / companion app: `3774` (configurable)
- Ingress: `62901`
- Internal: API `62906`, ttyd `62905`

Images are built in CI only. The nightly is rebuilt daily with the newest `T3CODE_CHANNEL=nightly` release and published as `ghcr.io/marcohald/t3-ha-app-nightly`.
