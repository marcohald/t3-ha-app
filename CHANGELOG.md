# Changelog

## 1.0.1 - Automatic FQDN Server Naming & Custom Server Name

- 🏷️ **Automatic Server Naming via HA FQDN**: Automatically discovers and configures the Home Assistant FQDN (from `external_url` or `internal_url`) as the T3 Code server display name and environment host ID.
- ⚙️ **Configurable Server Name**: Optional `server_name` setting in add-on configuration allows overriding the server name.
- 🔗 **Persistent Host Identifiers**: Directly manages `/etc/machine-info`, `/data/.t3/usage-host-id`, `/etc/hostname`, and persistent overlays.
- 🐙 **Enhanced GitHub CLI Symlink**: Guaranteed `/usr/bin/gh` binary path compatibility.


## 1.0.0 - Initial Lightweight T3 Code Release

- 🚀 **Dedicated T3 Code Server**: Powered by `t3 serve --lan --port 3773` with auto-project bootstrapping at `/homeassistant`.
- 📱 **1-Click Mobile App Pairing**: Instant pairing QR code and deep links for the official T3 Code mobile apps (iOS & Android).
- 🌐 **T3 Connect Remote Access**: Full integration with T3 Connect and relay tunnel service (`relay.t3.codes`) with status dashboard.
- 🐙 **GitHub CLI (`gh`) Integration**: Pre-installed standalone stripped GitHub CLI binary for direct git and repository management.
- 🪶 **Ultra Lightweight Footprint**: Stripped unnecessary heavy runtimes (saving >500 MB), base image built on minimal Debian Slim.
- 🔌 **Home Assistant Domotics MCP**: Native Model Context Protocol (MCP) server integration for Home Assistant Core and Zigbee2MQTT.
- 🖥️ **Ingress Web Terminal**: Fully-featured terminal UI with multi-session tabs, responsive D-Pad, theme toggles, and config backups.
