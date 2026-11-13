import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, expect, it, vi } from "vitest";

const mocks = vi.hoisted(() => ({ ask: vi.fn(), start: vi.fn(), stop: vi.fn(), cancel: vi.fn(),
  phase: "idle", active: false, transcript: null as null | ((text: string) => void) }));
vi.mock("./useSpeechInput", () => ({ useSpeechInput: (options: { onTranscript: (text: string) => void }) => {
  mocks.transcript = options.onTranscript;
  return { ...mocks, message: "", error: null, unavailable: null };
} }));
vi.mock("../useTurnStream", () => ({ useTurnStream: () => ({
  progress: [], result: null, busy: false, error: null, ask: mocks.ask,
  cancel: vi.fn(), cancelled: false, executionState: "", attemptNo: 0, turnId: "",
}) }));
vi.mock("../../../shared/api/client", async (original) => ({
  ...await original<typeof import("../../../shared/api/client")>(),
  getConversation: vi.fn().mockResolvedValue({ messages: [], turns: [], has_more: false }),
}));

import { AssistantPanel } from "../AssistantPanel";

beforeEach(() => { vi.clearAllMocks(); mocks.phase = "idle"; mocks.active = false; });
afterEach(cleanup);

it("语音按钮不提交表单，识别文字追加到草稿，确认后才发送", async () => {
  render(<AssistantPanel conversationId="c1" scopeId="album1" />);
  const input = screen.getByRole("textbox", { name: "向助手提问" }) as HTMLInputElement;
  await waitFor(() => expect(input.disabled).toBe(false));
  fireEvent.change(input, { target: { value: "请帮我" } });
  fireEvent.click(screen.getByRole("button", { name: "语音输入" }));
  expect(mocks.start).toHaveBeenCalledTimes(1);
  expect(mocks.ask).not.toHaveBeenCalled();
  act(() => mocks.transcript!("找一下去年旅行的照片。"));
  expect(input.value).toBe("请帮我 找一下去年旅行的照片。");
  expect(mocks.ask).not.toHaveBeenCalled();
  fireEvent.click(screen.getByRole("button", { name: "发送" }));
  expect(mocks.ask).toHaveBeenCalledTimes(1);
});

it("录音时阻止 Enter/表单提交，支持停止和取消", async () => {
  mocks.phase = "recording"; mocks.active = true;
  render(<AssistantPanel conversationId="c1" scopeId="album1" />);
  await waitFor(() => expect(screen.queryByText("正在加载历史消息…")).toBeNull());
  const input = screen.getByRole("textbox", { name: "向助手提问" }) as HTMLInputElement;
  expect(input.disabled).toBe(true);
  fireEvent.submit(input.closest("form")!);
  expect(mocks.ask).not.toHaveBeenCalled();
  fireEvent.click(screen.getByRole("button", { name: "停止并识别" }));
  fireEvent.click(screen.getByRole("button", { name: "取消语音" }));
  expect(mocks.stop).toHaveBeenCalledTimes(1);
  expect(mocks.cancel).toHaveBeenCalledTimes(1);
});
