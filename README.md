# T3 Code for Home Assistant

<div align="center">
  <img src="icon.png" width="96" height="96" alt="T3 Code Logo" style="border-radius: 20px; box-shadow: 0 4px 12px rgba(0,0,0,0.15);">
  <h2>T3 Code Home Assistant App</h2>
  <p><b>Lightweight AI Coding Terminal, T3 Code Server, T3 Connect Remote Access & GitHub CLI</b></p>
</div>

---

## ✨ Features

- 📱 **T3 Code Server with 1-Click Mobile Companion Pairing**:
  - Runs `t3 serve --lan --port 3773` out of the box with zero setup.
  - Pair directly with the official **T3 Code** companion app for **iOS** (App Store) and **Android** (Google Play).
  - Built-in Ingress pairing modal with live generated vector QR Code, direct app link, and token refresh.
- 🌐 **T3 Connect Remote Access**:
  - Full support for the **T3 Connect** relay service (`https://relay.t3.codes`) via Cloudflare tunnels.
  - Manage and monitor connection status with `t3 connect status` or directly inside the Ingress UI.
  - Accessible securely from any device across the internet without opening router ports.
- 🐙 **Integrated GitHub CLI (`gh`)**:
  - Official, high-performance GitHub CLI binary pre-installed.
  - Seamlessly clone repos, manage pull requests, create issues, and push code directly from the terminal (`gh auth login`).
- 🪶 **Ultra Lightweight Container**:
  - Built on minimal `debian:bookworm-slim`.
  - Removes redundant standalone agent binaries (saving over **500 MB** of container bloat) as T3 Code provisions its own runtime adapters on demand.
- 🔌 **Home Assistant Domotics & MCP Bridges**:
  - Pre-configured Model Context Protocol (MCP) server endpoints for **Home Assistant Core** and **Zigbee2MQTT**.
  - Direct access to `/config` and `/share` via `/homeassistant`.
- 🖥️ **Full-Featured Ingress Terminal UI**:
  - Embedded into your Home Assistant sidebar.
  - Multi-session tabs (`tmux`-backed persistence across browser reloads).
  - Virtual D-Pad (Up/Down/Left/Right/Enter) optimized for mobile touchscreens.
  - Seamless Dark, Light, and Auto themes matching your Home Assistant palette.
  - 1-Click file & photo uploads directly into `/tmp/uploads`.
  - Integrated backup export and import for `/data/.t3`.

---

## 🚀 Installation

### Option 1: Local Add-on (Recommended for Local Development)
1. Copy or clone this folder into your Home Assistant `/addons/` directory (e.g. `/addons/t3_code`).
2. Navigate in Home Assistant to **Settings** > **Add-ons** > **Add-on Store**.
3. Click the three dots (⋮) in the top-right corner and select **Check for updates**.
4. In the **Local add-ons** section, select **T3 Code** and click **Install**.
5. Once built and installed, toggle **Show in sidebar** and click **Start**.

### Option 2: Custom Repository
1. Add this repository URL to **Settings** > **Add-ons** > **Add-on Store** > **Repositories**.
2. Install **T3 Code** from the store.

---

## 📱 Mobile Companion App Pairing

1. Install **T3 Code** on your phone (iOS App Store or Google Play).
2. Connect your phone to your home Wi-Fi network.
3. Open **T3 Code** from the Home Assistant sidebar.
4. Click the **📱 Pair T3 Code Mobile App** button in the toolbar.
5. Scan the QR code or tap **Copy** to import the connection details into the mobile app.

---

## 🌐 Setting Up T3 Connect (Remote Access)

To use your Home Assistant T3 Code environment securely away from home:
1. Open the terminal tab inside the add-on.
2. Run:
   ```bash
   t3 connect
   # or
   t3 up --connect
   ```
3. Follow the one-time authentication URL to link your T3 account.
4. Check the status anytime with:
   ```bash
   t3 connect status
   ```
   Or click the **🌐 T3 Connect** button in the Ingress toolbar.

---

## ⚙️ Configuration Reference

In the Add-on **Configuration** tab:

| Option | Type | Default | Description |
|---|---|---|---|
| `ha_token` | string | *empty* | Long-lived access token for Home Assistant Core MCP access. |
| `mqtt_host` | string | *empty* | MQTT Broker hostname (e.g. `core-mosquitto`) for Zigbee2MQTT MCP. |
| `mqtt_port` | string | `1883` | MQTT Broker port. |
| `mqtt_user` | string | *empty* | MQTT username. |
| `mqtt_pass` | password | *empty* | MQTT password. |

---

## 📁 Repository Structure

```
├── config.yaml          # Home Assistant add-on specification & options
├── build.yaml           # Multi-architecture build metadata (amd64, aarch64)
├── Dockerfile           # Minimal Debian-slim container definition
├── run.sh               # Container entrypoint & daemon orchestration
├── attach.sh            # Multi-session tmux attachment handler
├── welcome.sh           # Clean terminal greeting & connection summary
├── tmux.conf            # Mobile-optimized terminal session settings
├── nginx.conf           # Ingress proxy & WebSocket configuration
├── upload.py            # API handler for pairing, backups, & T3 Connect
├── repository.yaml      # Home Assistant add-on repository manifest
├── CHANGELOG.md         # Release history
├── translations/        # English and German UI localized strings
│   ├── en.yaml
│   └── de.yaml
├── html/                # Responsive Ingress web client
│   ├── index.html
│   └── logo.png
├── icon.png             # Add-on store square icon
└── logo.png             # Add-on store header logo
```

---

<div align="center">
  <i>Built with ❤️ for Home Assistant & T3 Code</i>
</div>
