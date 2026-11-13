import { act, cleanup, renderHook } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const mocks = vi.hoisted(() => ({
  load: vi.fn(), transcribe: vi.fn(), disposeModel: vi.fn(),
  start: vi.fn(), stop: vi.fn(), disposeMic: vi.fn(), unavailable: vi.fn(),
}));
vi.mock("./recognizer", () => ({
  MAX_RECORDING_SECONDS: 60,
  SherpaRecognizer: class {
    load = mocks.load; transcribe = mocks.transcribe; dispose = mocks.disposeModel;
  },
}));
vi.mock("./recorder", () => ({
  speechUnavailableReason: mocks.unavailable,
  MicrophoneRecorder: class { start = mocks.start; stop = mocks.stop; dispose = mocks.disposeMic; },
}));

import { useSpeechInput } from "./useSpeechInput";

function deferred<T>() {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((done) => { resolve = done; });
  return { promise, resolve };
}

beforeEach(() => {
  vi.resetAllMocks();
  mocks.unavailable.mockReturnValue(null);
  mocks.load.mockResolvedValue(undefined);
  mocks.start.mockResolvedValue(undefined);
  mocks.stop.mockResolvedValue({ samples: new Float32Array(16000), sampleRate: 16000 });
  mocks.transcribe.mockResolvedValue("给我看看海边的照片。");
});
afterEach(() => { cleanup(); vi.useRealTimers(); });

describe("本机语音输入生命周期", () => {
  it("停止后填回文字，不自动发起问答，并复用已加载模型", async () => {
    const onTranscript = vi.fn();
    const { result } = renderHook(() => useSpeechInput({ contextKey: "a", enabled: true, onTranscript }));
    await act(() => result.current.start());
    expect(result.current.phase).toBe("recording");
    expect(onTranscript).not.toHaveBeenCalled();
    await act(() => result.current.stop());
    expect(result.current.phase).toBe("idle");
    expect(onTranscript).toHaveBeenCalledExactlyOnceWith("给我看看海边的照片。");
    expect(mocks.transcribe).toHaveBeenCalledWith(expect.any(Float32Array), 16000);
  });

  it("取消模型加载后晚到的完成不会打开麦克风", async () => {
    const loaded = deferred<void>(); mocks.load.mockReturnValue(loaded.promise);
    const { result } = renderHook(() => useSpeechInput({ contextKey: "a", enabled: true, onTranscript: vi.fn() }));
    let started!: Promise<void>;
    act(() => { started = result.current.start(); });
    act(() => result.current.cancel());
    await act(async () => { loaded.resolve(); await started; });
    expect(mocks.start).not.toHaveBeenCalled();
    expect(result.current.active).toBe(false);
    expect(mocks.disposeModel).toHaveBeenCalled();
    expect(mocks.disposeMic).toHaveBeenCalled();
  });

  it("切换会话时丢弃晚到的识别结果并释放模型", async () => {
    const decoded = deferred<string>(); mocks.transcribe.mockReturnValue(decoded.promise);
    const onTranscript = vi.fn();
    const { result, rerender } = renderHook(({ contextKey }) => useSpeechInput({ contextKey, enabled: true, onTranscript }),
      { initialProps: { contextKey: "a" } });
    await act(() => result.current.start());
    let stopped!: Promise<void>;
    await act(async () => { stopped = result.current.stop(); });
    rerender({ contextKey: "b" });
    await act(async () => { decoded.resolve("旧会话的语音"); await stopped; });
    expect(onTranscript).not.toHaveBeenCalled();
    expect(result.current.phase).toBe("idle");
    expect(mocks.disposeModel).toHaveBeenCalled();
  });

  it("卸载时释放正在录音的麦克风", async () => {
    const { result, unmount } = renderHook(() => useSpeechInput({ contextKey: "a", enabled: true, onTranscript: vi.fn() }));
    await act(() => result.current.start());
    unmount();
    expect(mocks.disposeMic).toHaveBeenCalled();
    expect(mocks.disposeModel).toHaveBeenCalled();
  });

  it("麦克风拒绝授权有明确提示且允许重试", async () => {
    mocks.start.mockRejectedValueOnce(new DOMException("denied", "NotAllowedError"));
    const { result } = renderHook(() => useSpeechInput({ contextKey: "a", enabled: true, onTranscript: vi.fn() }));
    await act(() => result.current.start());
    expect(result.current.error).toContain("麦克风权限未开启");
    expect(result.current.phase).toBe("idle");
    await act(() => result.current.start());
    expect(result.current.phase).toBe("recording");
    expect(result.current.error).toBeNull();
  });

  it("60秒自动停止，重复点击不会重复识别", async () => {
    vi.useFakeTimers();
    const onTranscript = vi.fn();
    const { result } = renderHook(() => useSpeechInput({ contextKey: "a", enabled: true, onTranscript }));
    await act(() => result.current.start());
    await act(() => vi.advanceTimersByTimeAsync(60_000));
    await act(() => result.current.stop());
    expect(mocks.stop).toHaveBeenCalledTimes(1);
    expect(onTranscript).toHaveBeenCalledTimes(1);
  });

  it("空识别结果保留草稿并提示重试", async () => {
    mocks.transcribe.mockResolvedValue("  ");
    const onTranscript = vi.fn();
    const { result } = renderHook(() => useSpeechInput({ contextKey: "a", enabled: true, onTranscript }));
    await act(() => result.current.start());
    await act(() => result.current.stop());
    expect(onTranscript).not.toHaveBeenCalled();
    expect(result.current.error).toContain("未识别到语音");
  });

  it("不支持的浏览器和回答忙碌时不启动模型或录音", async () => {
    mocks.unavailable.mockReturnValue("需要 HTTPS");
    const { result } = renderHook(() => useSpeechInput({ contextKey: "a", enabled: false, onTranscript: vi.fn() }));
    await act(() => result.current.start());
    expect(mocks.load).not.toHaveBeenCalled();
    expect(mocks.start).not.toHaveBeenCalled();
  });
});
