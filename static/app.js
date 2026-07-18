/* ytdl4me frontend — vanilla JS, no dependencies. */
(() => {
  "use strict";

  const STORAGE_KEY = "ytdl4me.accessKey";
  const POLL_INTERVAL = 800;

  // ------------------------------------------------------------- dom utils

  const $ = (sel) => document.querySelector(sel);

  function el(tag, className, text) {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined) node.textContent = text;
    return node;
  }

  function icon(id, className = "icon") {
    const NS = "http://www.w3.org/2000/svg";
    const svg = document.createElementNS(NS, "svg");
    svg.setAttribute("class", className);
    svg.setAttribute("aria-hidden", "true");
    const use = document.createElementNS(NS, "use");
    use.setAttribute("href", "#" + id);
    svg.appendChild(use);
    return svg;
  }

  // ------------------------------------------------------------ formatters

  function fmtSize(bytes) {
    if (bytes == null || !isFinite(bytes) || bytes < 0) return null;
    const units = ["B", "KB", "MB", "GB", "TB"];
    let v = bytes;
    let i = 0;
    while (v >= 1000 && i < units.length - 1) {
      v /= 1000;
      i += 1;
    }
    const num = i === 0 || v >= 10 ? String(Math.round(v)) : v.toFixed(1);
    return `${num} ${units[i]}`;
  }

  function fmtDuration(seconds) {
    if (seconds == null || !isFinite(seconds)) return null;
    const t = Math.max(0, Math.round(seconds));
    const h = Math.floor(t / 3600);
    const m = Math.floor((t % 3600) / 60);
    const s = String(t % 60).padStart(2, "0");
    return h > 0 ? `${h}:${String(m).padStart(2, "0")}:${s}` : `${m}:${s}`;
  }

  function fmtSpeed(bytesPerSec) {
    const size = fmtSize(bytesPerSec);
    return size ? `${size}/s` : null;
  }

  // ---------------------------------------------------- platform detection

  const PLATFORMS = {
    youtube: { name: "YouTube", icon: "icon-youtube" },
    vimeo: { name: "Vimeo", icon: "icon-vimeo" },
    soundcloud: { name: "SoundCloud", icon: "icon-soundcloud" },
    spotify: { name: "Spotify", icon: "icon-spotify" },
    deezer: { name: "Deezer", icon: "icon-deezer" },
    joox: { name: "JOOX", icon: "icon-joox" },
    tidal: { name: "TIDAL", icon: "icon-tidal" },
    applemusic: { name: "Apple Music", icon: "icon-applemusic" },
  };

  const PLATFORM_HOSTS = {
    "youtube.com": "youtube",
    "youtu.be": "youtube",
    "music.youtube.com": "youtube",
    "vimeo.com": "vimeo",
    "player.vimeo.com": "vimeo",
    "soundcloud.com": "soundcloud",
    "on.soundcloud.com": "soundcloud",
    "snd.sc": "soundcloud",
    "open.spotify.com": "spotify",
    "spotify.link": "spotify",
    "deezer.com": "deezer",
    "deezer.page.link": "deezer",
    "joox.com": "joox",
    "tidal.com": "tidal",
    "listen.tidal.com": "tidal",
    "embed.tidal.com": "tidal",
    "music.apple.com": "applemusic",
    "geo.music.apple.com": "applemusic",
    "embed.music.apple.com": "applemusic",
    "itunes.apple.com": "applemusic",
  };

  function detectPlatform(raw) {
    let url;
    try {
      url = new URL(String(raw).trim());
    } catch {
      return null;
    }
    if (url.protocol !== "http:" && url.protocol !== "https:") return null;
    // Mirror the backend: strip common subdomains.
    let host = url.hostname.toLowerCase()
      .replace(/^(www|m|listen|open|play|geo|embed)\./, "");
    // second strip for www.m.* style
    host = host.replace(/^(www|m)\./, "");
    if (PLATFORM_HOSTS[host]) return PLATFORM_HOSTS[host];
    if (host.endsWith(".deezer.com")) return "deezer";
    if (host.endsWith(".joox.com")) return "joox";
    if (host.endsWith(".tidal.com")) return "tidal";
    if (host.endsWith(".soundcloud.com")) return "soundcloud";
    if (host.includes("music.apple.") || host === "apple.com") return "applemusic";
    return "other";
  }

  // ------------------------------------------------- access key + 401 modal

  const keyStore = {
    get() {
      try {
        return localStorage.getItem(STORAGE_KEY) || "";
      } catch {
        return "";
      }
    },
    set(value) {
      try {
        localStorage.setItem(STORAGE_KEY, value);
      } catch {
        /* private mode — key lives only for this page load */
      }
    },
  };

  // "Unlisted link" access: a share link carries the access token in its
  // fragment (#key=…, not sent to the server / logs) or query (?key=). Store it
  // and strip it from the address bar so friends just click and go, while the
  // bare URL and crawlers get nothing usable.
  function consumeKeyFromUrl() {
    try {
      const fromHash = new URLSearchParams(location.hash.replace(/^#/, ""));
      const fromQuery = new URLSearchParams(location.search);
      const token =
        fromHash.get("key") || fromHash.get("k") ||
        fromQuery.get("key") || fromQuery.get("k") || "";
      if (token) {
        keyStore.set(token);
        history.replaceState(null, "", location.pathname);
      }
    } catch {
      /* malformed URL — ignore */
    }
  }

  class AuthCancelled extends Error {
    constructor() {
      super("Access key required.");
      this.name = "AuthCancelled";
    }
  }

  const keyModal = (() => {
    const dlg = $("#key-modal");
    const form = $("#key-form");
    const input = $("#key-input");
    const hint = $("#key-hint");
    let pending = null;

    form.addEventListener("submit", (event) => {
      event.preventDefault();
      const value = input.value.trim();
      if (!value) {
        input.focus();
        return;
      }
      keyStore.set(value);
      dlg.close("ok");
    });

    $("#key-cancel").addEventListener("click", () => dlg.close("cancel"));

    dlg.addEventListener("close", () => {
      const settled = pending;
      pending = null;
      if (!settled) return;
      if (dlg.returnValue === "ok") settled.resolve();
      else settled.reject(new AuthCancelled());
      dlg.returnValue = "";
    });

    return {
      // Resolves once the user saves a key; rejects with AuthCancelled on
      // dismiss. Concurrent 401s share a single prompt.
      request(keyWasSent) {
        if (pending) return pending.promise;
        pending = {};
        pending.promise = new Promise((resolve, reject) => {
          pending.resolve = resolve;
          pending.reject = reject;
        });
        hint.hidden = !keyWasSent;
        input.value = keyStore.get();
        if (!dlg.open) dlg.showModal();
        input.focus();
        input.select();
        return pending.promise;
      },
    };
  })();

  // ------------------------------------------------------------ api helper

  async function api(path, { method = "GET", body } = {}) {
    for (;;) {
      const key = keyStore.get();
      const headers = {};
      if (key) headers["X-Access-Key"] = key;
      const init = { method, headers };
      if (body !== undefined) {
        headers["Content-Type"] = "application/json";
        init.body = JSON.stringify(body);
      }

      let res;
      try {
        res = await fetch(path, init);
      } catch {
        throw new Error("Network error — could not reach the server.");
      }

      if (res.status === 401) {
        await keyModal.request(Boolean(key)); // throws AuthCancelled on dismiss
        continue; // retry with the new key
      }
      if (!res.ok) {
        let message = `Request failed (HTTP ${res.status}).`;
        try {
          const data = await res.json();
          if (data && typeof data.error === "string") message = data.error;
        } catch {
          /* non-JSON error body */
        }
        throw new Error(message);
      }
      return res.json();
    }
  }

  function fileUrl(jobId) {
    const key = keyStore.get();
    const base = `/api/jobs/${encodeURIComponent(jobId)}/file`;
    return key ? `${base}?key=${encodeURIComponent(key)}` : base;
  }

  // ------------------------------------------------------------ job poller

  class JobPoller {
    constructor(jobId, onUpdate, onFail) {
      this.jobId = jobId;
      this.onUpdate = onUpdate;
      this.onFail = onFail;
      this.timer = null;
      this.stopped = false;
      this.failures = 0;
    }

    start() {
      this.poll();
    }

    stop() {
      this.stopped = true;
      clearTimeout(this.timer);
    }

    async poll() {
      if (this.stopped) return;
      try {
        const job = await api(`/api/jobs/${encodeURIComponent(this.jobId)}`);
        this.failures = 0;
        this.onUpdate(job);
        if (job.status === "done" || job.status === "error") {
          this.stop();
          return;
        }
      } catch (err) {
        this.failures += 1;
        if (err instanceof AuthCancelled || this.failures >= 4) {
          this.stop();
          this.onFail(err);
          return;
        }
      }
      if (!this.stopped) {
        this.timer = setTimeout(() => this.poll(), POLL_INTERVAL);
      }
    }
  }

  // -------------------------------------------------------------- elements

  const fetchForm = $("#fetch-form");
  const urlInput = $("#url-input");
  const fetchBtn = $("#fetch-btn");
  const fetchError = $("#fetch-error");
  const platformBadge = $("#platform-badge");
  const probeSection = $("#probe-section");
  const probeCard = $("#probe-card");
  const downloadsSection = $("#downloads-section");
  const downloadsList = $("#downloads-list");

  let probeErrorEl = null;

  // -------------------------------------------------------- platform badge

  function updateBadge() {
    const platform = detectPlatform(urlInput.value);
    const meta = platform && PLATFORMS[platform];
    platformBadge.replaceChildren();
    if (!meta) {
      platformBadge.hidden = true;
      return;
    }
    platformBadge.hidden = false;
    platformBadge.appendChild(icon(meta.icon));
    platformBadge.appendChild(el("span", "badge-name", meta.name));
  }

  // ------------------------------------------------------------ probe flow

  function showError(node, err) {
    node.textContent = err && err.message ? err.message : "Something went wrong.";
    node.hidden = false;
  }

  function setFetching(busy) {
    fetchBtn.disabled = busy;
    fetchBtn.classList.toggle("busy", busy);
  }

  async function onFetchSubmit(event) {
    event.preventDefault();
    const url = urlInput.value.trim();
    if (!url) {
      urlInput.focus();
      return;
    }
    setFetching(true);
    fetchError.hidden = true;
    try {
      const probe = await api("/api/probe", { method: "POST", body: { url } });
      renderProbe(probe);
    } catch (err) {
      probeSection.hidden = true;
      if (!(err instanceof AuthCancelled)) showError(fetchError, err);
    } finally {
      setFetching(false);
    }
  }

  function renderProbe(probe) {
    probeCard.replaceChildren();

    // Thumbnail / placeholder
    const media = el("div", "probe-media");
    if (probe.kind === "audio") media.classList.add("audio");
    if (probe.thumbnail) {
      const img = el("img");
      img.src = probe.thumbnail;
      img.alt = "";
      img.loading = "lazy";
      img.referrerPolicy = "no-referrer";
      media.appendChild(img);
    } else {
      media.appendChild(
        icon(probe.kind === "audio" ? "icon-audio" : "icon-video", "icon media-icon")
      );
    }
    probeCard.appendChild(media);

    // Metadata
    const info = el("div", "probe-info");
    const platformRow = el("div", "probe-platform");
    const platformMeta = PLATFORMS[probe.platform];
    if (platformMeta) {
      platformRow.appendChild(icon(platformMeta.icon));
      platformRow.appendChild(el("span", null, platformMeta.name));
    } else {
      platformRow.appendChild(el("span", null, "Web"));
    }
    info.appendChild(platformRow);
    info.appendChild(el("h2", "probe-title", probe.title || "Untitled"));
    if (probe.uploader) info.appendChild(el("p", "probe-sub", probe.uploader));

    const chips = el("div", "probe-chips");
    const duration = fmtDuration(probe.duration);
    if (duration) chips.appendChild(el("span", "chip", duration));
    if (probe.original_quality) {
      chips.appendChild(el("span", "chip", probe.original_quality));
    }
    if (chips.childElementCount) info.appendChild(chips);
    probeCard.appendChild(info);

    // Option groups
    const options = el("div", "probe-options");
    if (probe.kind !== "audio" && probe.video_options && probe.video_options.length) {
      options.appendChild(optionGroup(probe, "Video", "icon-video", probe.video_options));
    }
    if (probe.audio_options && probe.audio_options.length) {
      options.appendChild(optionGroup(probe, "Audio", "icon-audio", probe.audio_options));
    }
    probeErrorEl = el("p", "error-text");
    probeErrorEl.hidden = true;
    options.appendChild(probeErrorEl);
    probeCard.appendChild(options);

    probeSection.hidden = false;
    probeSection.scrollIntoView({ behavior: "smooth", block: "nearest" });
  }

  function optionGroup(probe, label, iconId, opts) {
    const group = el("div", "opt-group");
    const heading = el("div", "opt-group-label");
    heading.appendChild(icon(iconId));
    heading.appendChild(el("span", null, label));
    group.appendChild(heading);

    const grid = el("div", "opt-grid");
    for (const opt of opts) grid.appendChild(optionButton(probe, opt));
    group.appendChild(grid);
    return group;
  }

  function optionButton(probe, opt) {
    const btn = el("button", "option");
    btn.type = "button";
    btn.appendChild(el("span", "option-label", opt.label));
    if (opt.detail) btn.appendChild(el("span", "option-detail", opt.detail));
    const size = fmtSize(opt.approx_size);
    if (size) btn.appendChild(el("span", "option-size", `~${size}`));
    btn.addEventListener("click", () => startDownload(probe, opt, btn));
    return btn;
  }

  // -------------------------------------------------------- downloads list

  async function startDownload(probe, opt, btn) {
    btn.disabled = true;
    if (probeErrorEl) probeErrorEl.hidden = true;
    try {
      const res = await api("/api/download", {
        method: "POST",
        body: { url: probe.url, option_id: opt.id },
      });
      addDownload(res.job_id, probe, opt);
    } catch (err) {
      if (probeErrorEl && !(err instanceof AuthCancelled)) showError(probeErrorEl, err);
    } finally {
      btn.disabled = false;
    }
  }

  const STATUS_LABELS = {
    queued: "Queued",
    downloading: "Downloading",
    processing: "Processing…",
    done: "Complete",
    error: "Failed",
  };

  function addDownload(jobId, probe, opt) {
    downloadsSection.hidden = false;

    const item = el("li", "dl");
    item.dataset.state = "queued";

    const head = el("div", "dl-head");
    const titles = el("div", "dl-titles");
    titles.appendChild(el("p", "dl-title", probe.title || "Untitled"));
    const platformMeta = PLATFORMS[probe.platform];
    const subParts = [platformMeta ? platformMeta.name : "Web", opt.label];
    titles.appendChild(el("p", "dl-sub", subParts.join(" · ")));
    head.appendChild(titles);
    const status = el("span", "dl-status", STATUS_LABELS.queued);
    head.appendChild(status);
    item.appendChild(head);

    const track = el("div", "dl-track");
    const fill = el("div", "dl-fill");
    track.appendChild(fill);
    item.appendChild(track);

    const meta = el("div", "dl-meta");
    const stats = el("span", "dl-stats", "Waiting in queue…");
    const pct = el("span", "dl-pct", "");
    meta.append(stats, pct);
    item.appendChild(meta);

    const errorLine = el("p", "dl-error");
    errorLine.hidden = true;
    item.appendChild(errorLine);

    const actions = el("div", "dl-actions");
    actions.hidden = true;
    const saveLink = el("a", "dl-save");
    saveLink.setAttribute("download", "");
    saveLink.appendChild(icon("icon-logo"));
    saveLink.appendChild(el("span", null, "Save file"));
    actions.appendChild(saveLink);
    item.appendChild(actions);

    downloadsList.prepend(item);

    let autoClicked = false;

    const applyUpdate = (job) => {
      item.dataset.state = job.status;
      status.textContent = STATUS_LABELS[job.status] || job.status;

      switch (job.status) {
        case "queued": {
          fill.style.removeProperty("width");
          stats.textContent = "Waiting in queue…";
          pct.textContent = "";
          break;
        }
        case "downloading": {
          const progress = Math.max(0, Math.min(100, Number(job.progress) || 0));
          fill.style.width = `${progress}%`;
          pct.textContent = `${progress.toFixed(0)}%`;
          const parts = [];
          const done = fmtSize(job.downloaded_bytes);
          const total = fmtSize(job.total_bytes);
          if (done && total) parts.push(`${done} of ${total}`);
          else if (done) parts.push(done);
          const speed = fmtSpeed(job.speed);
          if (speed) parts.push(speed);
          const eta = fmtDuration(job.eta);
          if (eta) parts.push(`ETA ${eta}`);
          stats.textContent = parts.join(" · ") || "Downloading…";
          break;
        }
        case "processing": {
          fill.style.removeProperty("width");
          stats.textContent = "Merging / converting with ffmpeg…";
          pct.textContent = "";
          break;
        }
        case "done": {
          fill.style.width = "100%";
          pct.textContent = "";
          const parts = [];
          if (job.filename) parts.push(job.filename);
          const size = fmtSize(job.filesize);
          if (size) parts.push(size);
          stats.textContent = parts.join(" · ") || "Ready";
          saveLink.href = fileUrl(jobId);
          if (job.filename) saveLink.setAttribute("download", job.filename);
          actions.hidden = false;
          if (!autoClicked) {
            autoClicked = true; // one-shot: never re-trigger for this job
            saveLink.click();
          }
          break;
        }
        case "error": {
          fill.style.width = "100%";
          pct.textContent = "";
          stats.textContent = "";
          errorLine.textContent = job.error || "Download failed.";
          errorLine.hidden = false;
          break;
        }
      }
    };

    const poller = new JobPoller(jobId, applyUpdate, (err) => {
      item.dataset.state = "error";
      status.textContent = STATUS_LABELS.error;
      stats.textContent = "";
      pct.textContent = "";
      errorLine.textContent =
        err && err.message ? err.message : "Lost contact with the server.";
      errorLine.hidden = false;
    });
    poller.start();
  }

  // ----------------------------------------------------------------- wire up

  consumeKeyFromUrl();
  fetchForm.addEventListener("submit", onFetchSubmit);
  urlInput.addEventListener("input", updateBadge);
  updateBadge();
})();
