# Original project brief (historical)

> Preserved for context. The implemented architecture, security model, and
> feature set are documented in the root `README.md`.

1. Project: "Ultimate Media Downloader" (New Project).
2. Scope: Support Pinterest, Facebook, and Instagram.
3. Feature: View full-screen original video preview before downloading.
4. Feature: Direct download of original source video/image.
5. Feature: Server-side audio extraction to standard MP3 (powered by `ffmpeg`).
6. UI: Clean, simple, no fake resolution buttons.
7. Port: 5000.

CURRENT STATE
Confirmed `ffmpeg` is available on the host system. This allows us to use `yt-dlp`'s post-processors to cleanly extract and encode MP3s on the server before sending them to the user.

PROPOSED ARCHITECTURE
1.  **Backend (`app.py`)**: 
    -   `/fetch`: A unified endpoint. It detects the domain. For FB/Insta, it uses `yt-dlp` to grab the single best `.mp4` file (video+audio combined) for native browser preview. For Pinterest, it uses our previously perfected Schema.org JSON scraper.
    -   `/download`: Proxies the direct `.mp4` or `.jpg` stream to the user (hiding their IP and enforcing file headers).
    -   `/download_mp3`: A dedicated endpoint that takes the original social media post URL, downloads the audio stream temporarily to the server using `yt-dlp` + `ffmpeg`, streams the MP3 to the user, and immediately deletes the temp file to save disk space.
2.  **Frontend (`index.html`)**: