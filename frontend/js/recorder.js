/* MeetMind browser recorder.
   Records a meeting in the browser and uploads it piece by piece while recording, so a long meeting
   never depends on one giant upload, and a dropped network or closed tab loses (almost) nothing.

   Modes
     room   : the laptop/USB microphone. Noise suppression is OFF by default so quieter voices and
              questions from the audience are kept; automatic gain lifts distant speakers.
     online : the meeting app's sound (shared tab/screen audio) + your microphone, mixed together.
     screen : like online, plus the screen as video.
*/
(function () {
  "use strict";
  const M = window.MeetMind;
  const PIECE_MS = 10000; // one piece every 10 seconds

  /* ---------- tiny IndexedDB store: backup of pieces until the server confirms them ---------- */
  const idb = {
    db: null,
    async open() {
      if (this.db || !window.indexedDB) return this.db;
      this.db = await new Promise((resolve) => {
        const req = indexedDB.open("meetmind-recordings", 1);
        req.onupgradeneeded = () => req.result.createObjectStore("pieces");
        req.onsuccess = () => resolve(req.result);
        req.onerror = () => resolve(null); // private mode etc.: memory only
      });
      return this.db;
    },
    async tx(mode, fn) {
      const db = await this.open();
      if (!db) return undefined;
      return new Promise((resolve) => {
        const t = db.transaction("pieces", mode);
        const r = fn(t.objectStore("pieces"));
        t.oncomplete = () => resolve(r && r.result);
        t.onerror = () => resolve(undefined);
      });
    },
    put(key, blob) { return this.tx("readwrite", (s) => s.put(blob, key)); },
    del(key) { return this.tx("readwrite", (s) => s.delete(key)); },
    get(key) { return this.tx("readonly", (s) => s.get(key)); },
    keys() { return this.tx("readonly", (s) => s.getAllKeys()); },
  };

  function pickMime(video) {
    const options = video
      ? ["video/webm;codecs=vp9,opus", "video/webm;codecs=vp8,opus", "video/webm", "video/mp4"]
      : ["audio/webm;codecs=opus", "audio/webm", "audio/ogg;codecs=opus", "audio/mp4"];
    return options.find((t) => window.MediaRecorder && MediaRecorder.isTypeSupported(t)) || "";
  }

  async function uploadPiece(recordingId, seq, blob) {
    const res = await fetch(`/api/recordings/${recordingId}/chunks/${seq}`, {
      method: "PUT",
      headers: { Authorization: "Bearer " + M.token(), "Content-Type": "application/octet-stream" },
      body: blob,
    });
    if (!res.ok && res.status !== 204) {
      const data = await res.json().catch(() => ({}));
      const err = new Error((data.error && data.error.message) || "Upload failed");
      err.status = res.status;
      throw err;
    }
  }

  class MeetingRecorder {
    constructor(meetingId, events) {
      this.meetingId = meetingId;
      this.on = events || {};
      this.state = "idle"; // idle | recording | paused | finishing | done
      this.seq = 0;
      this.queue = []; // [{seq, blob}] waiting for upload
      this.uploading = false;
      this.elapsedBefore = 0; // seconds recorded before the last resume
      this.startedAt = 0;
      this.streams = [];
    }

    static supported() {
      return !!(navigator.mediaDevices && window.MediaRecorder);
    }

    static canCaptureOtherApps() {
      return !!(navigator.mediaDevices && navigator.mediaDevices.getDisplayMedia);
    }

    static async microphones() {
      try {
        const devices = await navigator.mediaDevices.enumerateDevices();
        return devices.filter((d) => d.kind === "audioinput");
      } catch (e) { return []; }
    }

    /* seconds of audio recorded so far (pauses excluded): used to time-stamp notes */
    elapsed() {
      if (this.state === "recording") return this.elapsedBefore + (performance.now() - this.startedAt) / 1000;
      return this.elapsedBefore;
    }

    async start({ mode, deviceId, noiseReduction }) {
      const micConstraints = {
        deviceId: deviceId ? { exact: deviceId } : undefined,
        channelCount: 1,
        sampleRate: 48000,
        autoGainControl: true,
        // in the room: keep every voice (suppression can cut quiet or distant speakers)
        noiseSuppression: mode === "room" ? !!noiseReduction : true,
        // online: avoid recording the meeting audio twice through the speakers
        echoCancellation: mode !== "room",
      };

      let display = null;
      if (mode !== "room") {
        display = await navigator.mediaDevices.getDisplayMedia({
          video: true,
          audio: { echoCancellation: false, noiseSuppression: false, autoGainControl: false },
          systemAudio: "include",
          selfBrowserSurface: "exclude",
        });
        this.streams.push(display);
        if (!display.getAudioTracks().length) {
          this.cleanup();
          throw new Error("No meeting sound was shared. Share again and tick “Share tab audio” (or “Share system audio”).");
        }
        // if the user clicks "Stop sharing", finish the recording cleanly
        display.getTracks()[0].addEventListener("ended", () => { if (this.state === "recording" || this.state === "paused") this.stop(); });
      }
      const mic = await navigator.mediaDevices.getUserMedia({ audio: micConstraints });
      this.streams.push(mic);

      // Mix everything into one audio track (and measure the level for the meter)
      this.ctx = new (window.AudioContext || window.webkitAudioContext)();
      if (this.ctx.state === "suspended") await this.ctx.resume().catch(() => {}); // a suspended mixer records silence
      const dest = this.ctx.createMediaStreamDestination();
      this.analyser = this.ctx.createAnalyser();
      this.analyser.fftSize = 1024;
      for (const s of [mic, display]) {
        if (s && s.getAudioTracks().length) {
          const src = this.ctx.createMediaStreamSource(new MediaStream(s.getAudioTracks()));
          src.connect(dest);
          src.connect(this.analyser);
        }
      }
      const tracks = [...dest.stream.getAudioTracks()];
      const withVideo = mode === "screen";
      if (withVideo) tracks.unshift(display.getVideoTracks()[0]);
      else if (display) display.getVideoTracks().forEach((t) => t.stop()); // audio only: drop the video
      const stream = new MediaStream(tracks);

      const mimeType = pickMime(withVideo);
      const { recording } = await M.api.post(`/meetings/${this.meetingId}/live-recordings`, {
        mode, mime_type: mimeType.split(";")[0] || (withVideo ? "video/webm" : "audio/webm"),
      });
      this.recordingId = recording.id;
      try { localStorage.setItem("meetmind.liveRecording", JSON.stringify({ id: recording.id, meetingId: this.meetingId })); } catch (e) { /* ignore */ }

      this.recorder = new MediaRecorder(stream, {
        mimeType: mimeType || undefined,
        audioBitsPerSecond: 128000,
        videoBitsPerSecond: withVideo ? 2500000 : undefined,
      });
      this.recorder.addEventListener("dataavailable", (e) => {
        if (e.data && e.data.size) this.enqueue(e.data);
      });
      this.stopped = new Promise((resolve) => this.recorder.addEventListener("stop", resolve, { once: true }));
      this.recorder.start(PIECE_MS);

      this.state = "recording";
      this.startedAt = performance.now();
      this.keepAwake();
      this.meter();
      this.beforeUnload = (e) => { e.preventDefault(); e.returnValue = ""; };
      window.addEventListener("beforeunload", this.beforeUnload);
      this.emit("state");
      return recording;
    }

    enqueue(blob) {
      const piece = { seq: this.seq++, blob };
      this.queue.push(piece);
      idb.put(`${this.recordingId}:${piece.seq}`, blob);
      this.pump();
    }

    async pump() {
      if (this.uploading) return;
      this.uploading = true;
      while (this.queue.length) {
        const piece = this.queue[0];
        try {
          await uploadPiece(this.recordingId, piece.seq, piece.blob);
          this.queue.shift();
          idb.del(`${this.recordingId}:${piece.seq}`);
          this.emit("upload", { waiting: this.queue.length, ok: true });
        } catch (err) {
          if (err.status === 409 || err.status === 413) { this.emit("upload", { error: err.message }); this.queue = []; break; }
          this.emit("upload", { waiting: this.queue.length, ok: false }); // offline: try again shortly
          await new Promise((r) => setTimeout(r, 5000));
        }
      }
      this.uploading = false;
      if (this.drained && !this.queue.length) this.drained();
    }

    pause() {
      if (this.state !== "recording") return;
      this.recorder.pause();
      this.elapsedBefore = this.elapsed();
      this.state = "paused";
      this.emit("state");
    }

    resume() {
      if (this.state !== "paused") return;
      this.recorder.resume();
      this.startedAt = performance.now();
      this.state = "recording";
      this.emit("state");
    }

    async stop() {
      if (!["recording", "paused"].includes(this.state)) return;
      const duration = this.elapsed();
      this.elapsedBefore = duration;
      this.state = "finishing";
      this.emit("state");
      this.recorder.stop();
      await this.stopped;
      this.cleanup();
      await new Promise((resolve) => {
        if (!this.queue.length && !this.uploading) resolve();
        else { this.drained = resolve; this.pump(); }
      });
      const total = this.seq;
      for (let attempt = 0; attempt < 5; attempt++) {
        try {
          const res = await M.api.post(`/recordings/${this.recordingId}/finish`, { chunks: total, duration_seconds: Math.round(duration) });
          window.removeEventListener("beforeunload", this.beforeUnload);
          try { localStorage.removeItem("meetmind.liveRecording"); } catch (e) { /* ignore */ }
          this.state = "done";
          this.emit("state");
          return res.recording;
        } catch (err) {
          if (err.code === "missing_chunks" && err.details && err.details.missing) {
            for (const seq of err.details.missing) {
              const blob = await idb.get(`${this.recordingId}:${seq}`);
              if (blob) await uploadPiece(this.recordingId, seq, blob).catch(() => {});
            }
          } else if (attempt === 4) {
            throw err;
          }
          await new Promise((r) => setTimeout(r, 2000));
        }
      }
      throw new Error("Some pieces could not be uploaded. Keep this page open and try again.");
    }

    cleanup() {
      cancelAnimationFrame(this.raf);
      this.streams.forEach((s) => s.getTracks().forEach((t) => t.stop()));
      this.streams = [];
      if (this.ctx) this.ctx.close().catch(() => {});
      if (this.wakeLock) this.wakeLock.release().catch(() => {});
      document.removeEventListener("visibilitychange", this.onVisible);
    }

    async keepAwake() {
      if (!("wakeLock" in navigator)) return;
      const lock = async () => {
        try { this.wakeLock = await navigator.wakeLock.request("screen"); } catch (e) { /* not allowed */ }
      };
      this.onVisible = () => { if (document.visibilityState === "visible" && ["recording", "paused"].includes(this.state)) lock(); };
      document.addEventListener("visibilitychange", this.onVisible);
      lock();
    }

    meter() {
      const data = new Uint8Array(this.analyser.fftSize);
      let quietSince = null;
      const loop = () => {
        this.analyser.getByteTimeDomainData(data);
        let sum = 0;
        for (const v of data) { const x = (v - 128) / 128; sum += x * x; }
        const level = Math.min(1, Math.sqrt(sum / data.length) * 4);
        if (this.state === "recording") {
          if (level < 0.04) quietSince = quietSince || performance.now();
          else quietSince = null;
        }
        const tooQuiet = quietSince && performance.now() - quietSince > 8000;
        this.emit("level", { level, tooQuiet });
        this.emit("time", { seconds: this.elapsed() });
        this.raf = requestAnimationFrame(loop);
      };
      loop();
    }

    emit(name, data) {
      const fn = this.on[name];
      if (fn) fn(Object.assign({ state: this.state }, data || {}));
    }
  }

  /* Upload pieces left in the browser by an interrupted recording (e.g. the tab was closed). */
  async function recoverInterrupted(meetingId) {
    let saved;
    try { saved = JSON.parse(localStorage.getItem("meetmind.liveRecording") || "null"); } catch (e) { saved = null; }
    if (!saved || saved.meetingId !== meetingId) return null;
    const keys = ((await idb.keys()) || []).filter((k) => String(k).startsWith(saved.id + ":"));
    let uploaded = 0;
    for (const key of keys) {
      const seq = Number(String(key).split(":")[1]);
      const blob = await idb.get(key);
      try { await uploadPiece(saved.id, seq, blob); await idb.del(key); uploaded++; } catch (e) { /* server will rescue later */ }
    }
    return { recordingId: saved.id, uploaded };
  }

  M.MeetingRecorder = MeetingRecorder;
  M.recoverInterrupted = recoverInterrupted;
})();
