import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import type { ManifestSummary } from '../../api/types.ts';
import { ChatView } from './ChatView.tsx';

const { api } = vi.hoisted(() => ({ api: vi.fn() }));
vi.mock('../../api/client.ts', () => ({ api, post: vi.fn(), BASE: '/api/v1' }));
afterEach(cleanup);
beforeEach(() => { vi.resetAllMocks(); localStorage.clear(); });

const options = { profiles: [], targets: {}, managerUrl: '', modelEndpoint: '' };
function savedChats(status = 'ready') {
  const sessions = [
    { session_id: 'one', title: '第一段对话', status, scope_id: 'scope', album_id: 'album', turns: [], items: [] },
    { session_id: 'two', title: '第二段对话', status: 'cancelled', scope_id: 'scope', album_id: 'album', turns: [], items: [] },
  ];
  api.mockImplementation(async (path: string, init?: RequestInit) => {
    if (path.startsWith('/memory-spaces')) return { spaces: [{ id: 'scope', name: '相册' }], reuse_bases: [] };
    if (path === '/chat/sessions') return { items: sessions };
    if (init?.method === 'DELETE') return {};
    return sessions.find(s => path.endsWith('/' + s.session_id));
  });
  render(<ChatView sentrixUrl="http://product" manifests={[]} models={options} busy={false} onOpenMedia={vi.fn()}/>);
}

it('confirms deletion, clears the selected conversation and its saved selection', async () => {
  localStorage.setItem('photobench-chat-session', 'one');
  savedChats();
  await screen.findByRole('dialog');
  fireEvent.click(screen.getByLabelText('关闭'));
  await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull());
  fireEvent.click(await screen.findByLabelText('删除对话：第一段对话'));
  expect(api.mock.calls.some(([, init]) => init?.method === 'DELETE')).toBe(false);
  fireEvent.click(screen.getByRole('button', { name: '取消' }));
  expect(screen.queryByRole('button', { name: '确认删除' })).toBeNull();
  fireEvent.click(screen.getByLabelText('删除对话：第一段对话'));
  fireEvent.click(screen.getByRole('button', { name: '确认删除' }));
  await waitFor(() => expect(screen.queryByText('第一段对话')).toBeNull());
  expect(api).toHaveBeenCalledWith('/chat/sessions/one', { method: 'DELETE' });
  expect(localStorage.getItem('photobench-chat-session')).toBeNull();
  expect(screen.getByText('第二段对话')).toBeTruthy();
});

it('keeps the conversation and confirmation on delete failure', async () => {
  savedChats();
  fireEvent.click(await screen.findByLabelText('删除对话：第二段对话'));
  api.mockRejectedValueOnce(new Error('产品服务暂不可用'));
  fireEvent.click(screen.getByRole('button', { name: '确认删除' }));
  expect(await screen.findByRole('alert')).toBeTruthy();
  expect(screen.getByText('第二段对话')).toBeTruthy();
  expect(screen.getByRole('button', { name: '确认删除' })).toBeTruthy();
});

it('requires active conversations to stop before deleting them', async () => {
  savedChats('running');
  const button = await screen.findByLabelText('删除对话：第一段对话') as HTMLButtonElement;
  expect(button.disabled).toBe(true);
  expect((screen.getByLabelText('删除对话：第二段对话') as HTMLButtonElement).disabled).toBe(false);
});

it('opens the scope dataset with its valid question set before fetching and searches its rows', async () => {
  localStorage.clear();
  const manifests = [
    { album_id: 'album3', qa_sets: ['old-set'] },
    { album_id: 'mixed', qa_sets: ['mixed-set'] },
  ] as ManifestSummary[];
  api.mockImplementation(async (path: string) => {
    if (path === '/current-deployed-model') return { served_models: ['test-model'] };
    if (path.startsWith('/memory-spaces')) return { spaces: [{ id: 'scope', name: '相册' }], reuse_bases: [{ scope_id: 'scope', album_id: 'mixed' }] };
    if (path === '/chat/sessions') return { items: [] };
    if (path.startsWith('/qa-dataset?album_id=mixed&qa_set=mixed-set&')) return { items: [{ qa_id: 'mixed1', question: '婚礼照片', answer: '舞台' }] };
    throw new Error('invalid album/question-set pair');
  });
  render(<ChatView sentrixUrl="http://product" manifests={manifests} models={{ profiles: [], targets: {}, managerUrl: '', modelEndpoint: '' }} busy={false} onOpenMedia={vi.fn()}/>);
  await screen.findByRole('option', { name: '相册' });
  fireEvent.click(screen.getByText('从题集选题'));
  await screen.findByRole('button', { name: 'mixed1 婚礼照片' });
  await waitFor(() => expect(api.mock.calls.filter(([path]) => path.startsWith('/qa-dataset'))).toHaveLength(1));
  expect(screen.queryByRole('alert')).toBeNull();
  fireEvent.change(screen.getByLabelText('搜索聊天题目'), { target: { value: '没有的题' } });
  expect(screen.queryByRole('button', { name: 'mixed1 婚礼照片' })).toBeNull();
  fireEvent.change(screen.getByLabelText('搜索聊天题目'), { target: { value: 'mixed1' } });
  fireEvent.click(screen.getByRole('button', { name: 'mixed1 婚礼照片' }));
  expect(screen.getByText('标准答案：舞台')).toBeTruthy();
});
