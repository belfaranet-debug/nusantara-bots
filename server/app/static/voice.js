/* Grok Bot own build — Voice push-to-talk (client-side only).
 *
 * VoicePTT: idle -> recording -> transcribing -> answering ->
 * speaking -> idle (plus error). One press records, one release
 * (or second press) transcribes, asks the model, and speaks the
 * reply. This is PUSH-TO-TALK, not full-duplex / live voice.
 *
 * BYO-API invariant (same as app.js): the credential lives ONLY in
 * this browser's localStorage (key "grokbot_byo", written by the
 * deck's BYO panel). Voice reads it from there and sends it ONLY
 * to the provider base URL (OpenAI-compatible
 * /audio/transcriptions and /audio/speech). It is NEVER sent to
 * the local server origin, and this file makes NO fetch to /api/*
 * at all. Voice config (base URL, models, voice name) is stored
 * separately under "grokbot_voice" — config, never a credential.
 *
 * The model answer itself is NOT this module's job: the deck
 * injects askModel(transcript) — normally the deck's own
 * send-message path (BYO chat/completions + persist to the local
 * server). See VOICE-INTEGRATION.md for the wiring snippet.
 * If no askModel is injected, the transcript is reported via
 * onTranscript and the turn ends after transcription (no reply,
 * no speech) — it never invents an answer.
 */
(function (global) {
  "use strict";

  var VOICE_CONFIG_KEY = "grokbot_voice";
  var BYO_KEY = "grokbot_byo";
  var DEFAULT_BASE_URL = "https://api.openai.com/v1";
  var DEFAULT_STT_MODEL = "whisper-1";
  var DEFAULT_TTS_MODEL = "tts-1";
  var DEFAULT_VOICE = "alloy";

  var STATES = ["idle", "recording", "transcribing", "answering", "speaking", "error"];

  var ERROR_MESSAGES = {
    "no-credential": "No BYO-API credential saved — save one in the BYO panel first (voice never asks for a key itself).",
    "no-mic": "Microphone is not available in this browser.",
    "mic-denied": "Microphone permission was denied — allow mic access and try again.",
    "mic-failed": "Could not start the microphone.",
    "recorder-failed": "Recording failed to start.",
    "empty-audio": "No audio was captured — hold the button while you speak.",
    "stt-failed": "Speech-to-text failed (provider error or model unavailable).",
    "empty-transcript": "Nothing was transcribed — try speaking more clearly.",
    "no-answer": "The model returned no reply text.",
    "answer-failed": "Getting the model reply failed.",
    "tts-failed": "Text-to-speech failed (provider error or voice unavailable).",
    "playback-failed": "Audio playback failed.",
    "cancelled": "Voice turn cancelled."
  };

  function defaultStorage() {
    try { return global.localStorage || null; } catch (e) { return null; }
  }

  function readJson(storage, key) {
    if (!storage) return null;
    try {
      var raw = storage.getItem(key);
      return raw ? JSON.parse(raw) : null;
    } catch (e) { return null; }
  }

  function loadVoiceConfig(storage) {
    var cfg = readJson(storage, VOICE_CONFIG_KEY) || {};
    var byo = readJson(storage, BYO_KEY) || {};
    return {
      baseUrl: (cfg.baseUrl || byo.baseUrl || DEFAULT_BASE_URL).replace(/\/$/, ""),
      sttModel: cfg.sttModel || DEFAULT_STT_MODEL,
      ttsModel: cfg.ttsModel || DEFAULT_TTS_MODEL,
      voice: cfg.voice || DEFAULT_VOICE
    };
  }

  function saveVoiceConfig(storage, cfg) {
    if (!storage) return false;
    try {
      storage.setItem(VOICE_CONFIG_KEY, JSON.stringify({
        baseUrl: cfg.baseUrl || DEFAULT_BASE_URL,
        sttModel: cfg.sttModel || DEFAULT_STT_MODEL,
        ttsModel: cfg.ttsModel || DEFAULT_TTS_MODEL,
        voice: cfg.voice || DEFAULT_VOICE
      }));
      return true;
    } catch (e) { return false; }
  }

  function readCredential(storage) {
    var byo = readJson(storage, BYO_KEY) || {};
    return (typeof byo.credential === "string" && byo.credential.trim())
      ? byo.credential.trim() : "";
  }

  function parseSttResponse(data) {
    if (!data || typeof data !== "object") return "";
    if (typeof data.text === "string") return data.text.trim();
    return "";
  }

  /* VoicePTT
   * opts:
   *   askModel(transcript) -> Promise<string>   // injected by the deck; optional
   *   storage                                  // default: localStorage
   *   fetchImpl                                // default: global fetch
   *   getUserMedia                             // default: navigator.mediaDevices.getUserMedia
   *   MediaRecorderClass                       // default: global MediaRecorder
   *   AudioClass                               // default: global Audio
   *   createObjectURL / revokeObjectURL        // default: URL.createObjectURL / revokeObjectURL
   *   FormDataClass                            // default: global FormData
   *   BlobClass                                // default: global Blob
   *   config                                   // overrides stored config for this instance
   *   onStateChange(state, detail)             // state machine observer
   *   onTranscript(text) / onReply(text) / onError(code, message)
   */
  function VoicePTT(opts) {
    opts = opts || {};
    this.storage = opts.storage !== undefined ? opts.storage : defaultStorage();
    this.fetchImpl = opts.fetchImpl || (typeof global.fetch === "function" ? global.fetch.bind(global) : null);
    this.getUserMedia = opts.getUserMedia || null;
    this.MediaRecorderClass = opts.MediaRecorderClass || global.MediaRecorder || null;
    this.AudioClass = opts.AudioClass || global.Audio || null;
    this.createObjectURL = opts.createObjectURL ||
      (global.URL && global.URL.createObjectURL ? global.URL.createObjectURL.bind(global.URL) : null);
    this.revokeObjectURL = opts.revokeObjectURL ||
      (global.URL && global.URL.revokeObjectURL ? global.URL.revokeObjectURL.bind(global.URL) : null);
    this.FormDataClass = opts.FormDataClass || global.FormData || null;
    this.BlobClass = opts.BlobClass || global.Blob || null;
    this.askModel = typeof opts.askModel === "function" ? opts.askModel : null;
    this.onStateChange = typeof opts.onStateChange === "function" ? opts.onStateChange : null;
    this.onTranscript = typeof opts.onTranscript === "function" ? opts.onTranscript : null;
    this.onReply = typeof opts.onReply === "function" ? opts.onReply : null;
    this.onError = typeof opts.onError === "function" ? opts.onError : null;
    var stored = loadVoiceConfig(this.storage);
    this.config = Object.assign({}, stored, opts.config || {});
    this.state = "idle";
    this.errorCode = null;
    this.errorMessage = null;
    this.transcript = null;
    this.reply = null;
    this._recorder = null;
    this._stream = null;
    this._chunks = [];
    this._audio = null;
    this._audioUrl = null;
  }

  VoicePTT.STATES = STATES;
  VoicePTT.ERROR_MESSAGES = ERROR_MESSAGES;
  VoicePTT.DEFAULTS = {
    baseUrl: DEFAULT_BASE_URL, sttModel: DEFAULT_STT_MODEL,
    ttsModel: DEFAULT_TTS_MODEL, voice: DEFAULT_VOICE
  };
  VoicePTT.loadVoiceConfig = loadVoiceConfig;
  VoicePTT.saveVoiceConfig = saveVoiceConfig;
  VoicePTT.readCredential = readCredential;
  VoicePTT.parseSttResponse = parseSttResponse;

  VoicePTT.prototype._setState = function (state, detail) {
    this.state = state;
    if (this.onStateChange) {
      try { this.onStateChange(state, detail || null); } catch (e) { /* observer must never break the machine */ }
    }
  };

  VoicePTT.prototype._fail = function (code) {
    this._cleanupRecording();
    this._stopAudio();
    this.errorCode = code;
    this.errorMessage = ERROR_MESSAGES[code] || "Voice failed.";
    this._setState("error", { code: code, message: this.errorMessage });
    if (this.onError) {
      try { this.onError(code, this.errorMessage); } catch (e) { /* ignore */ }
    }
    return this.errorMessage;
  };

  VoicePTT.prototype._cleanupRecording = function () {
    if (this._recorder) {
      try {
        if (this._recorder.state && this._recorder.state !== "inactive") this._recorder.stop();
      } catch (e) { /* already stopped */ }
      this._recorder = null;
    }
    if (this._stream) {
      try {
        var tracks = this._stream.getTracks ? this._stream.getTracks() : [];
        tracks.forEach(function (t) { try { t.stop(); } catch (e) { /* ignore */ } });
      } catch (e) { /* ignore */ }
      this._stream = null;
    }
    this._chunks = [];
  };

  VoicePTT.prototype._stopAudio = function () {
    if (this._audio) {
      try { this._audio.pause(); } catch (e) { /* ignore */ }
      this._audio = null;
    }
    if (this._audioUrl && this.revokeObjectURL) {
      try { this.revokeObjectURL(this._audioUrl); } catch (e) { /* ignore */ }
      this._audioUrl = null;
    }
  };

  VoicePTT.prototype.getConfig = function () {
    return Object.assign({}, this.config);
  };

  VoicePTT.prototype.setConfig = function (patch) {
    this.config = Object.assign({}, this.config, patch || {});
    saveVoiceConfig(this.storage, this.config);
    return this.getConfig();
  };

  VoicePTT.prototype.reset = function () {
    this._cleanupRecording();
    this._stopAudio();
    this.errorCode = null;
    this.errorMessage = null;
    this.transcript = null;
    this.reply = null;
    this._setState("idle");
  };

  VoicePTT.prototype.cancel = function () {
    if (this.state === "idle") return;
    this._cleanupRecording();
    this._stopAudio();
    this.errorCode = "cancelled";
    this.errorMessage = ERROR_MESSAGES["cancelled"];
    this._setState("idle", { cancelled: true });
  };

  VoicePTT.prototype.isBusy = function () {
    return this.state !== "idle" && this.state !== "error";
  };

  /* Toggle: idle -> start recording; recording -> stop & process.
   * Returns a promise that resolves when the whole turn finishes
   * (or rejects with the error code on failure). */
  VoicePTT.prototype.toggle = function () {
    if (this.state === "recording") return this.stopAndProcess();
    if (this.state === "idle" || this.state === "error") return this.startRecording();
    return Promise.resolve(false);
  };

  VoicePTT.prototype.startRecording = async function () {
    if (this.state === "recording") return true;
    if (this.state !== "idle" && this.state !== "error") return false;
    this.errorCode = null;
    this.errorMessage = null;
    this.transcript = null;
    this.reply = null;

    var credential = readCredential(this.storage);
    if (!credential) { this._fail("no-credential"); return false; }
    if (!this.fetchImpl) { this._fail("stt-failed"); return false; }

    var gum = this.getUserMedia;
    if (!gum) {
      try {
        if (global.navigator && global.navigator.mediaDevices && global.navigator.mediaDevices.getUserMedia) {
          gum = global.navigator.mediaDevices.getUserMedia.bind(global.navigator.mediaDevices);
        }
      } catch (e) { gum = null; }
    }
    if (!gum) { this._fail("no-mic"); return false; }
    if (!this.MediaRecorderClass) { this._fail("no-mic"); return false; }

    var stream;
    try {
      stream = await gum({ audio: true });
    } catch (err) {
      var name = err && err.name ? err.name : "";
      if (name === "NotAllowedError" || name === "SecurityError") this._fail("mic-denied");
      else if (name === "NotFoundError" || name === "NotReadableError") this._fail("no-mic");
      else this._fail("mic-failed");
      return false;
    }
    this._stream = stream;
    this._chunks = [];
    var self = this;
    var recorder;
    try {
      recorder = new this.MediaRecorderClass(stream);
    } catch (e) {
      this._cleanupRecording();
      this._fail("recorder-failed");
      return false;
    }
    recorder.ondataavailable = function (ev) {
      if (ev && ev.data) self._chunks.push(ev.data);
    };
    recorder.onerror = function () { self._fail("recorder-failed"); };
    this._recorder = recorder;
    try {
      recorder.start();
    } catch (e) {
      this._cleanupRecording();
      this._fail("recorder-failed");
      return false;
    }
    this._setState("recording");
    return true;
  };

  /* Stop the recorder, then run transcribe -> answer -> speak.
   * Resolves true when the turn completes (idle), false on error
   * (state "error" with errorCode set). */
  VoicePTT.prototype.stopAndProcess = function () {
    var self = this;
    if (this.state !== "recording") return Promise.resolve(false);
    return new Promise(function (resolve) {
      var recorder = self._recorder;
      var done = false;
      var timer = null;
      function finish() {
        if (done) return;
        done = true;
        if (timer) clearTimeout(timer);
        self._processCaptured().then(resolve);
      }
      if (!recorder) { finish(); return; }
      var prevOnStop = recorder.onstop;
      recorder.onstop = function () {
        if (typeof prevOnStop === "function") { try { prevOnStop(); } catch (e) { /* ignore */ } }
        finish();
      };
      try { recorder.stop(); } catch (e) { finish(); }
      /* Safety net: a recorder that never fires onstop must not
       * hang the turn forever. */
      timer = setTimeout(function () { finish(); }, 5000);
    });
  };

  VoicePTT.prototype._processCaptured = async function () {
    var stream = this._stream;
    this._recorder = null;
    if (stream) {
      try {
        var tracks = stream.getTracks ? stream.getTracks() : [];
        tracks.forEach(function (t) { try { t.stop(); } catch (e) { /* ignore */ } });
      } catch (e) { /* ignore */ }
      this._stream = null;
    }
    var chunks = this._chunks;
    this._chunks = [];
    if (!chunks.length) { this._fail("empty-audio"); return false; }
    var blob;
    try {
      blob = new this.BlobClass(chunks, { type: "audio/webm" });
    } catch (e) {
      blob = chunks[0];
    }
    return this._runTurn(blob);
  };

  VoicePTT.prototype._runTurn = async function (audioBlob) {
    var credential = readCredential(this.storage);
    if (!credential) { this._fail("no-credential"); return false; }

    /* --- transcribe --- */
    this._setState("transcribing");
    var transcript;
    try {
      transcript = await this.transcribe(audioBlob, credential);
    } catch (e) {
      this._fail("stt-failed");
      return false;
    }
    if (!transcript) { this._fail("empty-transcript"); return false; }
    this.transcript = transcript;
    if (this.onTranscript) {
      try { this.onTranscript(transcript); } catch (e) { /* ignore */ }
    }

    /* No askModel injected: the turn ends here, honestly — the
     * transcript is delivered, nothing is invented. */
    if (!this.askModel) {
      this._setState("idle", { transcript: transcript, noAnswer: true });
      return true;
    }

    /* --- answer --- */
    this._setState("answering");
    var reply;
    try {
      reply = await this.askModel(transcript);
    } catch (e) {
      this._fail("answer-failed");
      return false;
    }
    if (!reply || !String(reply).trim()) { this._fail("no-answer"); return false; }
    this.reply = String(reply);
    if (this.onReply) {
      try { this.onReply(this.reply); } catch (e) { /* ignore */ }
    }

    /* --- speak --- */
    this._setState("speaking");
    var ok;
    try {
      ok = await this.speak(this.reply, credential);
    } catch (e) {
      this._fail("tts-failed");
      return false;
    }
    if (!ok) { this._fail("tts-failed"); return false; }
    this._setState("idle", { transcript: transcript, reply: this.reply });
    return true;
  };

  /* STT: POST {baseUrl}/audio/transcriptions (multipart).
   * The credential goes ONLY to this provider URL. */
  VoicePTT.prototype.transcribe = async function (audioBlob, credential) {
    credential = credential || readCredential(this.storage);
    if (!credential) throw new Error("no-credential");
    if (!this.fetchImpl) throw new Error("no-fetch");
    var url = this.config.baseUrl.replace(/\/$/, "") + "/audio/transcriptions";
    var form = new this.FormDataClass();
    form.append("file", audioBlob, "voice.webm");
    form.append("model", this.config.sttModel);
    var resp = await this.fetchImpl(url, {
      method: "POST",
      headers: { "Authorization": "Bearer " + credential },
      body: form
    });
    if (!resp.ok) throw new Error("stt-http-" + resp.status);
    var data;
    try { data = await resp.json(); } catch (e) { throw new Error("stt-bad-json"); }
    return parseSttResponse(data);
  };

  /* TTS: POST {baseUrl}/audio/speech -> play the returned audio.
   * The credential goes ONLY to this provider URL. Resolves true
   * when playback finishes, false when playback cannot start. */
  VoicePTT.prototype.speak = async function (text, credential) {
    var self = this;
    credential = credential || readCredential(this.storage);
    if (!credential) throw new Error("no-credential");
    if (!this.fetchImpl) throw new Error("no-fetch");
    if (!this.AudioClass || !this.createObjectURL) throw new Error("no-audio");
    var url = this.config.baseUrl.replace(/\/$/, "") + "/audio/speech";
    var resp = await this.fetchImpl(url, {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        "Authorization": "Bearer " + credential
      },
      body: JSON.stringify({
        model: this.config.ttsModel,
        input: text,
        voice: this.config.voice
      })
    });
    if (!resp.ok) throw new Error("tts-http-" + resp.status);
    var audioBlob;
    if (typeof resp.blob === "function") audioBlob = await resp.blob();
    else if (typeof resp.arrayBuffer === "function") audioBlob = new this.BlobClass([await resp.arrayBuffer()], { type: "audio/mpeg" });
    else throw new Error("tts-no-audio");
    this._stopAudio();
    this._audioUrl = this.createObjectURL(audioBlob);
    var audio = new this.AudioClass(this._audioUrl);
    this._audio = audio;
    return new Promise(function (resolve) {
      var settled = false;
      var timer = null;
      function done(ok) {
        if (settled) return;
        settled = true;
        if (timer) clearTimeout(timer);
        resolve(ok);
      }
      audio.onended = function () { done(true); };
      audio.onerror = function () { done(false); };
      var played;
      try {
        played = audio.play();
      } catch (e) { done(false); return; }
      if (played && typeof played.catch === "function") {
        played.catch(function () { done(false); });
      }
      /* Safety net: an Audio that never fires onended/onerror
       * must not hang the turn forever. */
      timer = setTimeout(function () { done(true); }, 120000);
    });
  };

  /* Expose for the browser (classic script) and for Node tests. */
  global.VoicePTT = VoicePTT;
  if (typeof module !== "undefined" && module.exports) {
    module.exports = {
      VoicePTT: VoicePTT,
      loadVoiceConfig: loadVoiceConfig,
      saveVoiceConfig: saveVoiceConfig,
      readCredential: readCredential,
      parseSttResponse: parseSttResponse,
      ERROR_MESSAGES: ERROR_MESSAGES,
      DEFAULTS: VoicePTT.DEFAULTS
    };
  }
})(typeof window !== "undefined" ? window : globalThis);
