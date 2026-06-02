# Release v1.0.0: Initial Major Release

This is the first stable release of Screen Stream, establishing a zero-configuration, high-performance pipeline to turn any mobile device into a fully interactive second monitor for Linux.

## 🚀 Core Features & Contributions

- **Hardware-Accelerated Capture:** I implemented ffmpeg with x11grab and automatic hardware encoder selection (NVENC/VAAPI) to ensure minimal CPU overhead.

- **Low-Latency Streaming:** I engineered the pipeline to encode video as H.264 MPEG-TS, delivering it over WebSockets to be rendered natively in the browser via mpegts.js for sub-500ms latency.

- **Zero-Install Client:** No application is required on the receiving tablet or phone. The stream runs purely through a local URL on any modern web browser.

- **Interactive Remote Control:** I built custom touch-to-mouse coordinate mapping utilizing xdotool to seamlessly control the Linux host directly from the client screen.

- **Clipboard Synchronization:** Integrated two-way clipboard syncing between the host and the client device using xclip.

## 🛠️ Deployment & Packaging

- **Systemd Integration:** Created install-service.sh to seamlessly run the application as a persistent background service.

- **Debian Packaging:** Developed automated build scripts (packaging/build-deb.sh) for generating .deb installers, streamlining distribution.

## ⚠️ Known Limitations

- **Display Server Compatibility:** The current architecture relies on x11grab and xdotool, which are optimized for X11 environments. Wayland users may need to run via Xwayland for full functionality. Native Wayland protocols are planned for future major releases.
