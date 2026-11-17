import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import { useState } from 'react';
import type { InteractiveModel } from '../../api/types.ts';
import { ModelSelect } from './ModelSelect.tsx';
import { RetestDialog } from './RetestDialog.tsx';
import { ChatView } from '../chat/ChatView.tsx';

const { api, post } = vi.hoisted(() => ({ api: vi.fn(), post: vi.fn() }));
vi.mock('../../api/client.ts', () => ({ api, post, BASE: '/api/v1' }));
vi.mock('../../hooks/useQaBrowser.ts', () => ({ useQaBrowser: () => ({
  qaBrowserAlbum: 'album', qaBrowserSet: 'qa', qaBrowserOptions: ['qa'],
  qaBrowserSearch: '', setQaBrowserAlbum: vi.fn(), setQaBrowserSet: vi.fn(),
  setQaBrowserSearch: vi.fn(), loadQaBrowser: vi.fn(),
  visibleQaBrowserItems: [{ qa_id: 'q1', question: '选中的问题', answer: '标准答案' }],
}) }));

const models = { profiles: [{ id: 'other', label: '另一个模型' }], targets: { cloud: { label: '云模型' } }, managerUrl: 'http://manager', modelEndpoint: 'http://model/v1' };
afterEach(cleanup);
beforeEach(() => {
  localStorage.clear(); api.mockReset(); post.mockReset();
  api.mockImplementation((path: string) => Promise.resolve(path.startsWith('/memory-spaces')
    ? { spaces: [{ id: 'scope', name: '相册' }], reuse_bases: [{ scope_id: 'scope', album_id: 'album', ready: true }] }
    : { items: [] }));
});

it('selects Manager, configured target and named custom endpoint', () => {
  const selected = vi.fn();
  function Harness() {
    const [value, setValue] = useState<InteractiveModel>({});
    return <ModelSelect options={models} value={value} onChange={v => { selected(v); setValue(v); }}/>;
  }
  render(<Harness/>);
  fireEvent.change(screen.getByLabelText('问答模型'), { target: { value: 'profile:other' } });
  expect(selected).toHaveBeenLastCalledWith({ profile: 'other', base_url: 'http://model/v1', manager_url: 'http://manager' });
  fireEvent.change(screen.getByLabelText('问答模型'), { target: { value: 'target:cloud' } });
  expect(selected).toHaveBeenLastCalledWith({ target_id: 'cloud' });
  fireEvent.change(screen.getByLabelText('问答模型'), { target: { value: 'custom' } });
  fireEvent.change(screen.getByLabelText('问答模型名'), { target: { value: 'test-model' } });
  expect(selected).toHaveBeenLastCalledWith({ base_url: 'http://model/v1', endpoint_model: 'test-model' });
});

it('retests only the selected question and scoring batch with a different model', async () => {
  post.mockResolvedValue({ run_id: 'single' });
  const close = vi.fn();
  render(<RetestDialog run={{ run_id: 'original', selected_scoring_profile: 'atm', selected_scoring_batch: 'batch' }} question={{ index: 3, qa_id: 'q4', question: '题目' }} models={models} busy={false} onClose={close}/>);
  expect((screen.getByLabelText('重测推理模式') as HTMLInputElement).checked).toBe(true);
  fireEvent.change(screen.getByLabelText('问答模型'), { target: { value: 'profile:other' } });
  fireEvent.click(screen.getByText('开始重测'));
  await screen.findByRole('region', { name: '实时回答与轨迹' });
  expect(close).not.toHaveBeenCalled();
  expect(screen.getByRole('dialog')).toBeTruthy();
  expect(post).toHaveBeenCalledWith('/runs/original/retest', {
    qa_id: 'q4', reasoning: true, model: { profile: 'other', base_url: 'http://model/v1', manager_url: 'http://manager' }, scoring_profile: 'atm', scoring_batch: 'batch',
  });
});

it('sends free questions and continues the same conversation', async () => {
  post.mockResolvedValue({ session_id: 'chat1', scope_id: 'scope', status: 'ready', turns: [], items: [], album_id: 'album' });
  render(<ChatView sentrixUrl="http://product" manifests={[]} models={models} busy={false} onOpenMedia={vi.fn()}/>);
  await screen.findByRole('option', { name: '相册' });
  fireEvent.change(screen.getByLabelText('聊天问题'), { target: { value: '第一问' } });
  fireEvent.click(screen.getByText('发送'));
  await waitFor(() => expect(post).toHaveBeenCalledWith('/chat/sessions', { scope_id: 'scope', model: {}, message: '第一问', reasoning: true }));
  await waitFor(() => expect((screen.getByLabelText('聊天问题') as HTMLTextAreaElement).disabled).toBe(false));
  fireEvent.change(screen.getByLabelText('聊天问题'), { target: { value: '再问' } });
  fireEvent.click(screen.getByLabelText('聊天推理模式'));
  fireEvent.click(screen.getByText('发送'));
  await waitFor(() => expect(post).toHaveBeenLastCalledWith('/chat/sessions/chat1/turns', { message: '再问', reasoning: false }));
});

it('previews GT but sends only the selected question identity', async () => {
  post.mockResolvedValue({ session_id: 'chat2', scope_id: 'scope', status: 'ready', turns: [], items: [], album_id: 'album' });
  render(<ChatView sentrixUrl="http://product" manifests={[]} models={models} busy={false} onOpenMedia={vi.fn()}/>);
  await screen.findByRole('option', { name: '相册' });
  fireEvent.click(screen.getByText('从题集选题'));
  fireEvent.click(screen.getByText('选中的问题'));
  expect(screen.getByText('标准答案：标准答案')).toBeTruthy();
  fireEvent.click(screen.getByText('运行此题（新对话）'));
  await waitFor(() => expect(post).toHaveBeenCalledWith('/chat/sessions', { scope_id: 'scope', model: {}, reasoning: true, album_id: 'album', qa_set: 'qa', qa_id: 'q1' }));
});

it('can disable reasoning for a new retest', async () => {
  post.mockResolvedValue({ run_id: 'plain' });
  render(<RetestDialog run={{ run_id: 'original' }} question={{ index: 0, qa_id: 'q1' }} models={models} busy={false} onClose={vi.fn()}/>);
  fireEvent.click(screen.getByLabelText('重测推理模式'));
  fireEvent.click(screen.getByText('开始重测'));
  await waitFor(() => expect(post).toHaveBeenCalledWith('/runs/original/retest', expect.objectContaining({ reasoning: false })));
});

it('only closes via × and cancels a creation that is still returning its ID', async () => {
  let resolveStart!: (value: { run_id: string }) => void;
  post.mockImplementation((path: string) => path.endsWith('/retest')
    ? new Promise(resolve => { resolveStart = resolve; }) : Promise.resolve({}));
  const close = vi.fn();
  render(<RetestDialog run={{ run_id: 'original' }} question={{ index: 0, qa_id: 'q1' }} models={models} busy={false} onClose={close}/>);
  fireEvent.click(screen.getByTestId('interactive-modal-backdrop'));
  fireEvent.keyDown(screen.getByRole('dialog'), { key: 'Escape' });
  expect(close).not.toHaveBeenCalled();
  fireEvent.click(screen.getByText('开始重测'));
  fireEvent.click(screen.getByLabelText('关闭'));
  expect(close).not.toHaveBeenCalled();
  resolveStart({ run_id: 'created' });
  await waitFor(() => expect(post).toHaveBeenCalledWith('/runs/created/cancel', {}));
  await waitFor(() => expect(close).toHaveBeenCalledTimes(1));
});

it('keeps the retest open on completion and does not cancel a completed run', async () => {
  post.mockResolvedValue({ run_id: 'done' });
  api.mockImplementation((path: string) => Promise.resolve(path.endsWith('/progress')
    ? { status: 'completed', item: { index: 0, final_answer: '完成回答', runtime_turns: [{ index: 0, trace: [{ text: '真实事件' }] }] } }
    : { items: [] }));
  const close = vi.fn();
  render(<RetestDialog run={{ run_id: 'original' }} question={{ index: 0, qa_id: 'q1' }} models={models} busy={false} onClose={close}/>);
  fireEvent.click(screen.getByText('开始重测'));
  await screen.findByText('完成回答');
  expect(screen.getByRole('dialog')).toBeTruthy();
  expect(close).not.toHaveBeenCalled();
  fireEvent.click(screen.getByLabelText('关闭'));
  await waitFor(() => expect(close).toHaveBeenCalledTimes(1));
  expect(post.mock.calls.filter(([path]) => String(path).endsWith('/cancel'))).toHaveLength(0);
});

it('shows cancellation errors and keeps the dialog open for retry', async () => {
  post.mockImplementation((path: string) => path.endsWith('/cancel')
    ? Promise.reject(new Error('服务暂不可用')) : Promise.resolve({ run_id: 'active' }));
  api.mockImplementation((path: string) => Promise.resolve(path.endsWith('/progress') ? { status: 'running' } : { items: [] }));
  const close = vi.fn();
  render(<RetestDialog run={{ run_id: 'original' }} question={{ index: 0, qa_id: 'q1' }} models={models} busy={false} onClose={close}/>);
  fireEvent.click(screen.getByText('开始重测'));
  await screen.findByText('重测进行中…');
  fireEvent.click(screen.getByLabelText('关闭'));
  await screen.findByRole('alert');
  expect(screen.getByRole('alert').textContent).toContain('取消失败');
  expect(close).not.toHaveBeenCalled();
  post.mockResolvedValue({});
  fireEvent.click(screen.getByLabelText('关闭'));
  await waitFor(() => expect(close).toHaveBeenCalledTimes(1));
});

it('updates running events in place without collapsing the trace panel', async () => {
  post.mockResolvedValue({ run_id: 'live' });
  let count = 0;
  api.mockImplementation((path: string) => Promise.resolve(path.endsWith('/progress')
    ? { status: ++count === 1 ? 'running' : 'completed', item: { index: 0, runtime_turns: [{ index: 0, message: '提问', trace: [{ text: count === 1 ? '检索开始' : '检索完成' }] }] } }
    : { items: [] }));
  const close = vi.fn();
  const { container } = render(<RetestDialog run={{ run_id: 'original' }} question={{ index: 0, qa_id: 'q1' }} models={models} busy={false} onClose={close}/>);
  fireEvent.click(screen.getByText('开始重测'));
  await screen.findByText('检索开始');
  const trace = document.querySelector('.debug-trace-node') as HTMLDetailsElement;
  expect(trace.open).toBe(true);
  trace.open = false; fireEvent(trace, new Event('toggle'));
  await screen.findByText('检索完成', {}, { timeout: 3000 });
  expect(document.querySelector('.debug-trace-node')).toBe(trace);
  expect(trace.open).toBe(false);
  expect(close).not.toHaveBeenCalled();
  expect(container.childElementCount).toBe(0); // Rendered through a portal, outside the QA list.
});

it('cancels a running chat on × while backdrop and Escape keep it open', async () => {
  post.mockImplementation((path: string) => Promise.resolve(path.endsWith('/cancel') ? {} : {
    session_id: 'live-chat', scope_id: 'scope', status: 'running', turns: [], items: [], album_id: 'album',
  }));
  render(<ChatView sentrixUrl="http://product" manifests={[]} models={models} busy={false} onOpenMedia={vi.fn()}/>);
  await screen.findByRole('option', { name: '相册' });
  fireEvent.change(screen.getByLabelText('聊天问题'), { target: { value: '测试取消' } });
  fireEvent.click(screen.getByText('发送'));
  await screen.findByText('停止回答');
  fireEvent.click(screen.getByTestId('interactive-modal-backdrop'));
  fireEvent.keyDown(screen.getByRole('dialog'), { key: 'Escape' });
  expect(screen.getByRole('dialog')).toBeTruthy();
  fireEvent.click(screen.getByLabelText('关闭'));
  await waitFor(() => expect(post).toHaveBeenCalledWith('/chat/sessions/live-chat/cancel', {}));
  await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull());
  expect(screen.getByText('打开对话与轨迹')).toBeTruthy();
});
