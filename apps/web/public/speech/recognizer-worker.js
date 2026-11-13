/* Classic worker: the pinned Emscripten build and its JS binding share this realm. */
/* global OfflineRecognizer */
let recognizer;
let loading = false;

self.onmessage = (event) => {
  const data = event.data;
  try {
    if (data.type === "load") {
      if (recognizer) { self.postMessage({ type: "ready" }); return; }
      if (loading) return;
      loading = true;
      const base = data.base;
      self.Module = {
        locateFile: (name) => `${base}/${name}`,
        // Keep Emscripten/model diagnostics out of the user's transcript and logs.
        print: () => {},
        printErr: () => {},
        setStatus: (status) => {
          const match = /\((\d+)\/(\d+)\)/.exec(status);
          const percent = match && Number(match[2]) > 0
            ? Math.min(100, Math.round(Number(match[1]) / Number(match[2]) * 100)) : null;
          self.postMessage({ type: "progress", message: percent === null
            ? "正在初始化本机语音模型…"
            : `正在加载本机语音模型 ${percent}%（首次加载较慢）` });
        },
        onAbort: () => self.postMessage({ type: "error", message: "语音模型加载失败，请检查本站语音资源是否完整，或使用文字输入。" }),
        onRuntimeInitialized: () => {
          try {
            recognizer = new OfflineRecognizer({
              featConfig: { sampleRate: 16000, featureDim: 80 },
              modelConfig: {
                senseVoice: { model: "./sense-voice.onnx", language: "auto", useInverseTextNormalization: 1 },
                tokens: "./tokens.txt", numThreads: 1, provider: "cpu", debug: 0,
              },
              decodingMethod: "greedy_search",
            }, self.Module);
            if (!recognizer.handle) throw new Error("Invalid recognizer");
            self.postMessage({ type: "ready" });
          } catch {
            self.postMessage({ type: "error", message: "本机语音模型初始化失败，可能是可用内存不足。请重试或使用文字输入。" });
          }
        },
      };
      importScripts(`${base}/sherpa-onnx-asr.js`, `${base}/sherpa-onnx-wasm-main-vad-asr.js`);
    } else if (data.type === "transcribe") {
      if (!recognizer || !(data.samples instanceof Float32Array)
        || !Number.isFinite(data.sampleRate) || data.sampleRate < 8000 || data.sampleRate > 192000
        || !data.samples.length || data.samples.length > data.sampleRate * 60) {
        throw new Error("Invalid audio");
      }
      const stream = recognizer.createStream();
      try {
        // sherpa-onnx resamples the actual microphone rate to the model's 16 kHz.
        stream.acceptWaveform(data.sampleRate, data.samples);
        recognizer.decode(stream);
        const result = recognizer.getResult(stream);
        if (typeof result.text !== "string") throw new Error("Invalid result");
        self.postMessage({ type: "result", text: result.text });
      } finally { stream.free(); }
    }
  } catch {
    self.postMessage({ type: "error", message: "语音识别失败，请检查本站模型资源与浏览器支持情况后重试。" });
  }
};
