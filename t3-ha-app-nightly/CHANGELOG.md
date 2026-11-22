# Changelog

## 0.1.20261122.1 (2026-11-22)

- 🔧 Persist `extra_packages` directly to the add-on's Supervisor options (`POST /addons/self/options`), auto-migrate any legacy `/data/.t3/extra-packages.list`, and increment same-day nightly versions cleanly.

## 0.1.0

- Initial nightly add-on, sharing the complete data directory with the stable T3 Code add-on via `/share/t3-ha-app-data`.
