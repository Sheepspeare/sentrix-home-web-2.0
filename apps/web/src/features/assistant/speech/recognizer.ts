/** Browser-only ASR boundary. Audio never goes to an HTTP API. */
export interface SpeechRecognizer {
  load(onProgress: (message: string) => void): Promise<void>;
  transcribe(samples: Float32Array, sampleRate: number): Promise<string>;
  dispose(): void;
}

export const SPEECH_ASSET_BASE = "/speech/sensevoice-1.12.20";
export const MAX_RECORDING_SECONDS = 60;

export class SherpaRecognizer implements SpeechRecognizer {
  private worker: Worker | null = null;
  private ready = false;
  private loading: Promise<void> | null = null;
  private pending: { reject: (error: Error) => void; timer: ReturnType<typeof setTimeout> } | null = null;

  load(onProgress: (message: string) => void): Promise<void> {
    if (this.ready) return Promise.resolve();
    if (this.loading) return this.loading;
    this.worker = new Worker("/speech/recognizer-worker.js");
    const worker = this.worker;
    const loading = this.request<void>({ type: "load", base: SPEECH_ASSET_BASE }, 300_000, onProgress)
      .then(() => { if (this.worker === worker) this.ready = true; })
      .finally(() => { if (this.loading === loading) this.loading = null; });
    this.loading = loading;
    return loading;
  }

  transcribe(samples: Float32Array, sampleRate: number): Promise<string> {
    if (!this.ready) return Promise.reject(new Error("语音模型尚未就绪，请重试。"));
    return this.request<string>({ type: "transcribe", samples, sampleRate }, 120_000, undefined, [samples.buffer]);
  }

  private request<T>(
    message: object,
    timeout: number,
    progress?: (message: string) => void,
    transfer: Transferable[] = [],
  ): Promise<T> {
    return new Promise((resolve, reject) => {
      const worker = this.worker!;
      const finish = () => {
        if (this.pending) clearTimeout(this.pending.timer);
        this.pending = null;
        worker.onmessage = null;
        worker.onerror = null;
      };
      this.pending = {
        reject,
        timer: setTimeout(() => {
          finish();
          this.dispose();
          reject(new Error("语音处理超时，请重试或使用文字输入。"));
        }, timeout),
      };
      worker.onmessage = (event: MessageEvent) => {
        const data = event.data;
        if (data.type === "progress") { progress?.(data.message); return; }
        finish();
        if (data.type === "error") reject(new Error(data.message));
        else if (data.type === "ready") resolve(undefined as T);
        else if (data.type === "result") resolve(data.text as T);
        else reject(new Error("语音识别返回了无法读取的结果。"));
      };
      worker.onerror = (event) => {
        event.preventDefault();
        finish();
        reject(new Error("本机语音识别启动失败，请确认浏览器支持 WebAssembly SIMD，或使用文字输入。"));
      };
      worker.postMessage(message, transfer);
    });
  }

  dispose(): void {
    if (this.pending) {
      clearTimeout(this.pending.timer);
      this.pending.reject(new DOMException("语音输入已取消", "AbortError"));
      this.pending = null;
    }
    this.worker?.terminate();
    this.worker = null;
    this.ready = false;
    this.loading = null;
  }
}
