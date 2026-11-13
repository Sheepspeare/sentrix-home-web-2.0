import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { SherpaRecognizer } from "./recognizer";

class FakeWorker {
  static instance: FakeWorker;
  onmessage: ((event: MessageEvent) => void) | null = null;
  onerror: ((event: ErrorEvent) => void) | null = null;
  postMessage = vi.fn();
  terminate = vi.fn();
  constructor() { FakeWorker.instance = this; }
  reply(data: object) { this.onmessage?.({ data } as MessageEvent); }
}
beforeEach(() => { vi.stubGlobal("Worker", FakeWorker); vi.useFakeTimers(); });
afterEach(() => { vi.unstubAllGlobals(); vi.useRealTimers(); });

it("仅向本机 Worker 传送 PCM，模型加载成功后才允许识别", async () => {
  const recognizer = new SherpaRecognizer();
  await expect(recognizer.transcribe(new Float32Array(1), 16000)).rejects.toThrow("尚未就绪");
  const progress = vi.fn();
  const load = recognizer.load(progress);
  const worker = FakeWorker.instance;
  worker.reply({ type: "progress", message: "50%" });
  worker.reply({ type: "ready" }); await load;
  expect(progress).toHaveBeenCalledWith("50%");
  const samples = new Float32Array([0.1, 0.2]);
  const decoded = recognizer.transcribe(samples, 48000);
  expect(worker.postMessage).toHaveBeenLastCalledWith({ type: "transcribe", samples, sampleRate: 48000 }, [samples.buffer]);
  worker.reply({ type: "result", text: "你好" });
  expect(await decoded).toBe("你好");
  recognizer.dispose();
  expect(worker.terminate).toHaveBeenCalledTimes(1);
});

it("加载超时终止 Worker 并允许重新加载", async () => {
  const recognizer = new SherpaRecognizer();
  const load = recognizer.load(vi.fn());
  const rejection = expect(load).rejects.toThrow("超时");
  const worker = FakeWorker.instance;
  await vi.advanceTimersByTimeAsync(300_000);
  await rejection;
  expect(worker.terminate).toHaveBeenCalledTimes(1);
  const retry = recognizer.load(vi.fn());
  FakeWorker.instance.reply({ type: "ready" });
  await retry;
  recognizer.dispose();
});

it("取消加载拒绝未完成请求，释放资源", async () => {
  const recognizer = new SherpaRecognizer();
  const load = recognizer.load(vi.fn());
  const rejection = expect(load).rejects.toMatchObject({ name: "AbortError" });
  recognizer.dispose();
  await rejection;
  expect(FakeWorker.instance.terminate).toHaveBeenCalledTimes(1);
});
