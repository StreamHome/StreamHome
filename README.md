<div align="center">
  <img src="https://github.com/user-attachments/assets/4e188ccb-a419-444c-951c-391fc17c2a38" alt="StreamHome Media Server" width="100%">

<div align="center">
  <br>
  <img src="https://readme-typing-svg.demolab.com?font=Oswald&weight=700&size=50&color=E25822&center=true&vCenter=true&width=500&height=80&lines=StreamHome&repeat=false" alt="StreamHome">
</div>

<p><b>A modern, self-hosted VOD (Video on Demand) platform designed for seamless performance, ultimate personalization, and zero maintenance.</b></p>
</div>

---

StreamHome bridges the gap between premium VOD streaming experiences and the freedom of self-hosting. Built with a high-performance ASGI Python backend and a cutting-edge React 19 frontend, it brings your personal media library to life with intelligent TMDB cataloging, adaptive HLS playback, and a purely agnostic ingestion API. All without holding your media hostage—your files stay strictly in your control, backed up locally and synced natively with Google Drive.
## ✨ Core Features

1. ☁️ **Google Drive & Rclone Integration:** Built-in cloud storage integration powered by Rclone and Google OAuth, working seamlessly without disrupting your local media folder structure.
2. 🎨 **Multi-Theme UI Matrix:** A premium, dynamic React 19 web interface offering instant switching between high-end themes like Ember, Aurora, Cinema, and Gemini.
3. 🧠 **Hybrid Recommendation Engine:** An advanced recommendation system that meticulously tracks and analyzes *every single step and interaction* across the platform (searches, clicks, watch times, and explicit preferences) to deliver highly personalized feeds.
4. 🎬 **Automated TMDB Cataloging:** Intelligently fetches media metadata, posters, cast details, and plot summaries from TMDB, automatically saving them directly to your local directories.
5. 🌊 **Adaptive HLS Streaming:** A robust fMP4 playback pipeline that segments media on the fly and dynamically adjusts resolution (from 1080p down to 240p) based on network speed for a buffer-free experience.
6. 🔌 **Agnostic Ingestion API:** A powerful, extensible `/api/add-movie` endpoint that instantly accepts independent raw video/audio URLs from external sources (which can be easily captured via our official [Chrome](https://github.com/WaqSea/StreamHome-Extension-Chrome) and [Firefox](https://github.com/WaqSea/StreamHome-Extension-Firefox) extensions) and queues them for background processing via FFmpeg.
7. 🚀 **Sleek Web Setup Wizard & Security:** A complete, user-friendly `/setup` web wizard that eliminates the need for Linux terminal configurations, coupled with robust TOTP/2FA-backed administrator security.
8. 🗜️ **Automated HEVC Optimization:** A smart background transcoding system that converts media to HEVC (H.265) to save server storage space. It strictly runs only when the system is idle and automatically pauses the moment a user connects to the web interface, ensuring zero performance impact.
9. 🛡️ **Failsafe Recovery Architecture:** Guarantees zero data loss even in the event of total database corruption. Built around localized `metadata.json` records kept alongside media files, and an automated backup of the `database.db` file to both the local disk and Google Drive.

## 🖥️ Server Recommendations

StreamHome is designed to be highly optimized, but running real-time FFmpeg processing and background HEVC compression performs best with the following specifications:

| Resource | Minimum Requirements | Recommended (Ideal Experience) |
| :--- | :--- | :--- |
| **OS** | Ubuntu 22.04 / Windows 10 | Ubuntu 24.04 / Debian 12 |
| **CPU** | 2 vCores | 4+ vCores *(Crucial for fast HEVC encoding)* |
| **RAM** | 2 GB | 4+ GB |
| **Network** | 20 Mbps *(Lower is no problem)* | 1 Gbps *(For smooth fMP4 HLS & Rclone sync)* |
| **Storage** | 20 GB *(For Database & Meta)* | 50+ GB SSD *(Media hosted on Google Drive)* |

## 📦 Quick Installation

StreamHome comes with one-click installation scripts that handle all dependencies (Python, FFmpeg, Node, Rclone) automatically. 

**For Linux (Ubuntu/Debian):**
```bash
not available
```

**For Windows (PowerShell run as Administrator):**
```powershell
not available
```

## 📚 Documentation & Support

Facing any issues, need to configure a reverse proxy, or want to dive deeper into the API architecture? 
Please check out our comprehensive documentation folder. Almost every problem and configuration step is solved there!

👉 **[not available](docs/README.md)**

## 📫 Contact

Created and maintained by **waqsea**. 

If you have any questions, business inquiries, or just want to suggest a feature:
* **Email:** [contact@waqsea.com](mailto:contact@waqsea.com)
* **GitHub:** [@waqsea](https://github.com/waqsea)
