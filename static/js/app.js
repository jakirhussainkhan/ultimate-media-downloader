/* ==========================================================================
   Ultimate Media Downloader v1.0.0 — front-end controller
   No frameworks, no inline handlers, no untrusted HTML injection.
   ========================================================================== */

(() => {
    "use strict";

    const elements = {
        form: document.getElementById("mediaForm"),
        input: document.getElementById("mediaUrl"),
        pasteBtn: document.getElementById("pasteBtn"),
        extractBtn: document.getElementById("extractBtn"),
        extractLabel: document.querySelector("#extractBtn .primary-button__label"),
        extractSpinner: document.getElementById("extractSpinner"),
        statusPanel: document.getElementById("statusPanel"),
        statusText: document.getElementById("statusText"),
        resultPanel: document.getElementById("resultPanel"),
        previewFrame: document.getElementById("previewFrame"),
        platformBadge: document.getElementById("platformBadge"),
        formatBadge: document.getElementById("formatBadge"),
        qualityBadge: document.getElementById("qualityBadge"),
        mediaTitle: document.getElementById("mediaTitle"),
        mediaSubtitle: document.getElementById("mediaSubtitle"),
        downloadBtn: document.getElementById("downloadBtn"),
        downloadLabel: document.querySelector("#downloadBtn .primary-button__label"),
        mp3Btn: document.getElementById("mp3Btn"),
        mp3Label: document.querySelector("#mp3Btn .secondary-button__label"),
        mp3Spinner: document.getElementById("mp3Spinner"),
        copyBtn: document.getElementById("copyBtn"),
        copyLabel: document.getElementById("copyLabel"),
        toastRegion: document.getElementById("toastRegion"),
    };

    const state = {
        busy: false,
        audioBusy: false,
        lastResult: null,
        statusTimer: null,
        statusIndex: 0,
        previewToken: 0,
    };

    const ffmpegAvailable = document.body.dataset.ffmpeg === "true";
    const reduceMotion = window.matchMedia("(prefers-reduced-motion: reduce)");

    const STATUS_MESSAGES = [
        "Validating the link…",
        "Resolving the source…",
        "Selecting the best quality…",
        "Preparing your preview…",
    ];

    class AppError extends Error {
        constructor(message, status = 0) {
            super(message);
            this.name = "AppError";
            this.status = status;
        }
    }

    /* ----------------------------------------------------------------------
       Toasts
       ---------------------------------------------------------------------- */

    const TOAST_DEFAULTS = {
        success: { title: "Success", duration: 5200 },
        error: { title: "Something went wrong", duration: 8500 },
        info: { title: "Heads up", duration: 6000 },
    };

    function dismissToast(toast) {
        if (!toast || toast.classList.contains("is-leaving")) return;
        toast.classList.add("is-leaving");
        window.setTimeout(() => toast.remove(), 230);
    }

    function clearToasts() {
        // Starting a new extraction should not leave stale success/error
        // notifications competing with the current request.
        for (const toast of [...elements.toastRegion.children]) {
            toast.remove();
        }
    }

    function showToast(message, type = "info", options = {}) {
        const safeType = Object.prototype.hasOwnProperty.call(TOAST_DEFAULTS, type)
            ? type
            : "info";
        const defaults = TOAST_DEFAULTS[safeType];
        const duration =
            options.duration === undefined ? defaults.duration : options.duration;
        const persistent = duration === 0;

        while (elements.toastRegion.children.length >= 3) {
            dismissToast(elements.toastRegion.firstElementChild);
            elements.toastRegion.firstElementChild?.remove();
        }

        const toast = document.createElement("div");
        toast.className = `toast toast--${safeType}${persistent ? " toast--persistent" : ""}`;
        toast.setAttribute("role", safeType === "error" ? "alert" : "status");

        const icon = document.createElement("span");
        icon.className = "toast__icon";
        icon.setAttribute("aria-hidden", "true");
        icon.textContent = safeType === "success" ? "✓" : safeType === "error" ? "!" : "i";

        const body = document.createElement("div");
        body.className = "toast__body";

        const title = document.createElement("strong");
        title.className = "toast__title";
        title.textContent = options.title || defaults.title;

        const copy = document.createElement("span");
        copy.className = "toast__message";
        copy.textContent = String(message || "");

        body.append(title, copy);

        const close = document.createElement("button");
        close.type = "button";
        close.className = "toast__close";
        close.setAttribute("aria-label", "Dismiss notification");
        close.textContent = "×";
        close.addEventListener("click", () => dismissToast(toast));

        toast.append(icon, body, close);

        if (!persistent) {
            const progress = document.createElement("span");
            progress.className = "toast__progress";
            progress.setAttribute("aria-hidden", "true");
            toast.append(progress);
        }

        elements.toastRegion.append(toast);

        let timer = null;
        if (!persistent) {
            timer = window.setTimeout(() => dismissToast(toast), duration);
        }

        return {
            dismiss() {
                if (timer) window.clearTimeout(timer);
                dismissToast(toast);
            },
            setMessage(text) {
                copy.textContent = String(text || "");
            },
        };
    }

    /* ----------------------------------------------------------------------
       Busy / loading state
       ---------------------------------------------------------------------- */

    function setStatusMessage(text) {
        elements.statusText.textContent = text;
    }

    function startStatusCycle() {
        window.clearInterval(state.statusTimer);
        state.statusIndex = 0;
        setStatusMessage(STATUS_MESSAGES[0]);
        elements.statusPanel.hidden = false;

        state.statusTimer = window.setInterval(() => {
            state.statusIndex = (state.statusIndex + 1) % STATUS_MESSAGES.length;
            setStatusMessage(STATUS_MESSAGES[state.statusIndex]);
        }, 2700);
    }

    function stopStatusCycle() {
        window.clearInterval(state.statusTimer);
        state.statusTimer = null;
        elements.statusPanel.hidden = true;
    }

    function setBusy(busy) {
        state.busy = busy;
        elements.extractBtn.disabled = busy;
        elements.extractBtn.setAttribute("aria-busy", String(busy));
        elements.input.readOnly = busy;
        elements.pasteBtn.disabled = busy;
        elements.extractSpinner.hidden = !busy;
        elements.extractLabel.textContent = busy ? "Extracting…" : "Extract media";

        if (busy) {
            startStatusCycle();
        } else {
            stopStatusCycle();
        }
    }

    function resetResult() {
        state.previewToken += 1;
        state.lastResult = null;
        elements.resultPanel.hidden = true;
        elements.previewFrame.replaceChildren();
        elements.mp3Btn.hidden = true;
        elements.copyLabel.textContent = "Copy direct link";
    }

    /* ----------------------------------------------------------------------
       Validation / formatting helpers
       ---------------------------------------------------------------------- */

    function parseHttpUrl(value) {
        if (typeof value !== "string") return null;
        try {
            const parsed = new URL(value);
            if (parsed.protocol !== "http:" && parsed.protocol !== "https:") return null;
            return parsed;
        } catch {
            return null;
        }
    }

    function formatDuration(totalSeconds) {
        const seconds = Number(totalSeconds);
        if (!Number.isFinite(seconds) || seconds <= 0) return "";
        const rounded = Math.round(seconds);
        const hours = Math.floor(rounded / 3600);
        const minutes = Math.floor((rounded % 3600) / 60);
        const secs = rounded % 60;
        if (hours > 0) {
            return `${hours}:${String(minutes).padStart(2, "0")}:${String(secs).padStart(2, "0")}`;
        }
        return `${minutes}:${String(secs).padStart(2, "0")}`;
    }

    function hostLabel(value) {
        const parsed = parseHttpUrl(value);
        if (!parsed) return "";
        return parsed.hostname.replace(/^www\./i, "");
    }

    function validateResult(data) {
        if (!data || typeof data !== "object") {
            throw new AppError("The server returned an empty response.");
        }
        const downloadUrl = parseHttpUrl(data.download_url);
        if (!downloadUrl) {
            throw new AppError("The source returned an invalid media URL.");
        }
        const mediaType = ["video", "image", "audio"].includes(data.type)
            ? data.type
            : "video";
        const sourceUrl = parseHttpUrl(data.source_url);

        return {
            type: mediaType,
            downloadUrl: downloadUrl.href,
            sourceUrl: sourceUrl ? sourceUrl.href : null,
            title: typeof data.title === "string" && data.title.trim()
                ? data.title.trim()
                : "Untitled media",
            platform: typeof data.platform === "string" ? data.platform : "Media",
            quality: typeof data.quality === "string" ? data.quality : "Best available",
            format: typeof data.format === "string" ? data.format : mediaType.toUpperCase(),
            duration: Number(data.duration) || null,
            hasAudio: data.has_audio !== false,
        };
    }

    /* ----------------------------------------------------------------------
       Preview rendering
       ---------------------------------------------------------------------- */

    function showPreviewFallback(message) {
        elements.previewFrame.replaceChildren();

        const fallback = document.createElement("div");
        fallback.className = "preview-fallback";

        const icon = document.createElement("span");
        icon.className = "preview-fallback__icon";
        icon.setAttribute("aria-hidden", "true");
        icon.textContent = "▶";

        const heading = document.createElement("strong");
        heading.textContent = "Preview unavailable";

        const copy = document.createElement("span");
        copy.textContent = message;

        fallback.append(icon, heading, copy);
        elements.previewFrame.append(fallback);
    }

    function renderPreview(result) {
        state.previewToken += 1;
        const token = state.previewToken;
        const isCurrent = () => token === state.previewToken;

        elements.previewFrame.replaceChildren();

        if (result.type === "image") {
            const image = document.createElement("img");
            image.src = result.downloadUrl;
            image.alt = result.title;
            image.decoding = "async";
            image.addEventListener(
                "error",
                () => {
                    if (!isCurrent()) return;
                    showPreviewFallback(
                        "The source blocks in-browser previews. The download button still works."
                    );
                },
                { once: true }
            );
            elements.previewFrame.append(image);
            return;
        }

        if (result.type === "audio") {
            const wrapper = document.createElement("div");
            wrapper.className = "audio-preview";

            const art = document.createElement("div");
            art.className = "audio-preview__art";
            art.setAttribute("aria-hidden", "true");

            const rings = document.createElement("span");
            rings.className = "audio-preview__rings";

            const glyph = document.createElement("span");
            glyph.className = "audio-preview__glyph";
            glyph.textContent = "MP3";

            art.append(rings, glyph);

            const audio = document.createElement("audio");
            audio.controls = true;
            audio.preload = "metadata";
            audio.src = result.downloadUrl;
            audio.addEventListener(
                "error",
                () => {
                    if (!isCurrent()) return;
                    showPreviewFallback(
                        "The audio preview could not be loaded. Try downloading it."
                    );
                },
                { once: true }
            );

            wrapper.append(art, audio);
            elements.previewFrame.append(wrapper);
            return;
        }

        const loading = document.createElement("div");
        loading.className = "preview-loading";
        const spinner = document.createElement("span");
        spinner.className = "spinner spinner--preview";
        spinner.setAttribute("aria-hidden", "true");
        loading.append(spinner);

        const video = document.createElement("video");
        video.controls = true;
        video.playsInline = true;
        video.preload = "metadata";
        video.src = result.downloadUrl;
        video.setAttribute("aria-label", `Video preview: ${result.title}`);

        const removeLoading = () => loading.remove();
        video.addEventListener("loadeddata", removeLoading, { once: true });
        video.addEventListener("canplay", removeLoading, { once: true });
        video.addEventListener(
            "error",
            () => {
                removeLoading();
                if (!isCurrent()) return;
                showPreviewFallback(
                    "The source blocks in-browser playback. Use the download button to save the original file."
                );
            },
            { once: true }
        );

        elements.previewFrame.append(loading, video);
    }

    function renderResult(result) {
        state.lastResult = result;

        elements.platformBadge.textContent = result.platform;
        elements.formatBadge.textContent = result.format;
        elements.qualityBadge.textContent = result.quality;
        elements.mediaTitle.textContent = result.title;

        const details = [];
        if (result.duration) details.push(formatDuration(result.duration));
        if (result.type === "video" && !result.hasAudio) details.push("Video only");
        const sourceHost = hostLabel(result.sourceUrl);
        if (sourceHost) details.push(sourceHost);
        elements.mediaSubtitle.textContent =
            details.length > 0 ? details.join("  ·  ") : "Ready to download.";

        const downloadLabel = {
            video: "Download video",
            image: "Download image",
            audio: "Download audio",
        }[result.type];
        elements.downloadLabel.textContent = downloadLabel;
        elements.downloadBtn.href = `/download?url=${encodeURIComponent(
            result.downloadUrl
        )}&type=${encodeURIComponent(result.type)}`;
        elements.downloadBtn.setAttribute("download", "");
        elements.downloadBtn.setAttribute(
            "aria-label",
            `${downloadLabel} in original quality`
        );

        const canExtractAudio =
            result.type === "video" &&
            Boolean(result.sourceUrl) &&
            ffmpegAvailable &&
            result.hasAudio !== false;
        elements.mp3Btn.hidden = !canExtractAudio;
        elements.mp3Btn.disabled = false;
        elements.mp3Spinner.hidden = true;
        elements.mp3Label.textContent = "Extract MP3 audio";
        elements.copyLabel.textContent = "Copy direct link";

        renderPreview(result);

        elements.resultPanel.hidden = false;
        elements.mediaTitle.focus({ preventScroll: true });

        if (window.matchMedia("(max-width: 860px)").matches) {
            elements.resultPanel.scrollIntoView({
                behavior: reduceMotion.matches ? "auto" : "smooth",
                block: "start",
            });
        }

        if (result.type === "video" && !result.hasAudio) {
            showToast(
                "This source only exposes a video-only stream. The original download may have no audio.",
                "info",
                { title: "Video-only source" }
            );
        }
    }

    /* ----------------------------------------------------------------------
       Extract
       ---------------------------------------------------------------------- */

    async function fetchMedia() {
        if (state.busy) return;
        if (state.audioBusy) {
            showToast("Please wait for the current MP3 to finish before extracting another link.", "info");
            return;
        }

        const rawUrl = elements.input.value.trim();
        if (!rawUrl) {
            showToast("Paste a public media link first.", "error", {
                title: "A link is required",
            });
            elements.input.focus();
            return;
        }

        if (!/\.[a-z]{2,}/i.test(rawUrl) && !/^https?:\/\//i.test(rawUrl)) {
            showToast(
                "That does not look like a link. Paste a full https:// URL.",
                "error",
                { title: "Invalid link" }
            );
            elements.input.focus();
            return;
        }

        clearToasts();
        resetResult();
        setBusy(true);

        const controller = new AbortController();
        const timeoutId = window.setTimeout(() => controller.abort(), 120000);

        try {
            const response = await fetch("/fetch", {
                method: "POST",
                headers: {
                    "Content-Type": "application/json",
                    Accept: "application/json",
                },
                body: JSON.stringify({ url: rawUrl }),
                signal: controller.signal,
            });

            const text = await response.text();
            let data = null;
            if (text) {
                try {
                    data = JSON.parse(text);
                } catch {
                    data = null;
                }
            }

            if (!response.ok) {
                const message =
                    data && typeof data.error === "string"
                        ? data.error
                        : `The server could not process this link (HTTP ${response.status}).`;
                throw new AppError(message, response.status);
            }

            const result = validateResult(data);
            renderResult(result);
            showToast("Media extracted. Choose a download option.", "success", {
                title: "Ready to save",
            });
        } catch (error) {
            const message =
                error.name === "AbortError"
                    ? "The request took too long. The source may be slow — please try again."
                    : error instanceof TypeError
                      ? "Could not reach the server. Check your connection and try again."
                      : error.message || "An unexpected error occurred.";
            showToast(message, "error", { title: "Extraction failed" });
        } finally {
            window.clearTimeout(timeoutId);
            setBusy(false);
        }
    }

    /* ----------------------------------------------------------------------
       Clipboard
       ---------------------------------------------------------------------- */

    async function copyText(text) {
        if (navigator.clipboard && window.isSecureContext) {
            await navigator.clipboard.writeText(text);
            return;
        }
        const textarea = document.createElement("textarea");
        textarea.value = text;
        textarea.setAttribute("readonly", "");
        textarea.className = "sr-only";
        document.body.append(textarea);
        textarea.select();
        const copied = document.execCommand("copy");
        textarea.remove();
        if (!copied) throw new Error("Clipboard copy was blocked.");
    }

    async function copyDirectLink() {
        if (!state.lastResult) return;
        try {
            await copyText(state.lastResult.downloadUrl);
            elements.copyLabel.textContent = "Copied";
            showToast(
                "Direct media link copied. Signed links can expire, so download soon.",
                "success",
                { title: "Link copied" }
            );
            window.setTimeout(() => {
                elements.copyLabel.textContent = "Copy direct link";
            }, 2400);
        } catch {
            showToast("Clipboard access was blocked by the browser.", "error", {
                title: "Could not copy",
            });
        }
    }

    async function pasteFromClipboard() {
        if (!navigator.clipboard || !navigator.clipboard.readText) {
            elements.input.focus();
            showToast("Press Ctrl+V (or ⌘V) to paste your link.", "info", {
                title: "Clipboard unavailable",
            });
            return;
        }
        try {
            const text = await navigator.clipboard.readText();
            if (!text.trim()) {
                showToast("Your clipboard is empty.", "info");
                return;
            }
            elements.input.value = text.trim();
            elements.input.focus();
        } catch {
            elements.input.focus();
            showToast("Clipboard permission was denied. Use Ctrl+V (or ⌘V).", "info", {
                title: "Clipboard unavailable",
            });
        }
    }

    /* ----------------------------------------------------------------------
       MP3 extraction (fetch-to-blob so errors stay in-app)
       ---------------------------------------------------------------------- */

    async function downloadAudio() {
        if (state.audioBusy || !state.lastResult || !state.lastResult.sourceUrl) return;

        state.audioBusy = true;
        elements.mp3Btn.disabled = true;
        elements.mp3Btn.setAttribute("aria-busy", "true");
        elements.mp3Spinner.hidden = false;
        elements.mp3Label.textContent = "Encoding MP3…";

        const progressToast = showToast(
            "Extracting audio on the server and preparing your MP3. This can take a moment.",
            "info",
            { title: "Preparing MP3", duration: 0 }
        );

        const controller = new AbortController();
        const timeoutId = window.setTimeout(() => controller.abort(), 300000);

        try {
            const response = await fetch(
                `/download_mp3?url=${encodeURIComponent(state.lastResult.sourceUrl)}`,
                {
                    headers: { Accept: "audio/mpeg, application/json" },
                    signal: controller.signal,
                }
            );

            if (!response.ok) {
                let message = `The MP3 could not be created (HTTP ${response.status}).`;
                try {
                    const payload = await response.json();
                    if (payload && typeof payload.error === "string") {
                        message = payload.error;
                    }
                } catch {
                    /* Keep the generic message. */
                }
                throw new AppError(message, response.status);
            }

            const blob = await response.blob();
            if (!blob.size) {
                throw new AppError("The server returned an empty audio file.");
            }

            const disposition = response.headers.get("Content-Disposition") || "";
            const match = /filename="?([^";]+)"?/i.exec(disposition);
            const filename = match
                ? match[1].replace(/[^a-zA-Z0-9._-]/g, "_")
                : `ultimate-media-audio-${Date.now()}.mp3`;

            const objectUrl = URL.createObjectURL(blob);
            const anchor = document.createElement("a");
            anchor.href = objectUrl;
            anchor.download = filename.endsWith(".mp3") ? filename : `${filename}.mp3`;
            document.body.append(anchor);
            anchor.click();
            anchor.remove();
            window.setTimeout(() => URL.revokeObjectURL(objectUrl), 60000);

            progressToast.dismiss();
            showToast("Your MP3 is encoded and the download has started.", "success", {
                title: "MP3 ready",
            });
        } catch (error) {
            progressToast.dismiss();
            const message =
                error.name === "AbortError"
                    ? "MP3 extraction timed out. Try a shorter video or try again."
                    : error instanceof TypeError
                      ? "Could not reach the server while preparing the MP3."
                      : error.message || "Audio extraction failed.";
            showToast(message, "error", { title: "MP3 failed" });
        } finally {
            window.clearTimeout(timeoutId);
            state.audioBusy = false;
            elements.mp3Btn.disabled = false;
            elements.mp3Btn.removeAttribute("aria-busy");
            elements.mp3Spinner.hidden = true;
            elements.mp3Label.textContent = "Extract MP3 audio";
        }
    }

    /* ----------------------------------------------------------------------
       Wiring
       ---------------------------------------------------------------------- */

    elements.form.addEventListener("submit", (event) => {
        event.preventDefault();
        fetchMedia();
    });

    elements.pasteBtn.addEventListener("click", pasteFromClipboard);
    elements.mp3Btn.addEventListener("click", downloadAudio);
    elements.copyBtn.addEventListener("click", copyDirectLink);

    elements.downloadBtn.addEventListener("click", () => {
        if (!state.lastResult) return;
        showToast("Your download is starting.", "info", { duration: 3200 });
    });

    elements.input.addEventListener("input", () => {
        // Remove accidental whitespace/newlines from pasted text.
        const cleaned = elements.input.value.replace(/[\r\n]+/g, " ").trimStart();
        if (cleaned !== elements.input.value) {
            elements.input.value = cleaned;
        }
    });

    window.addEventListener("offline", () => {
        showToast("You are offline. Reconnect before extracting media.", "error", {
            title: "No connection",
        });
    });

    window.addEventListener("online", () => {
        showToast("Connection restored.", "success", { duration: 3200 });
    });
})();
