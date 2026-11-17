import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, expect, it, vi } from 'vitest';
import type { ManifestSummary } from '../../api/types.ts';
import { ChatView } from './ChatView.tsx';

const { api } = vi.hoisted(() => ({ api: vi.fn() }));
vi.mock('../../api/client.ts', () => ({ api, post: vi.fn(), BASE: '/api/v1' }));
afterEach(cleanup);

it('opens the scope dataset with its valid question set before fetching and searches its rows', async () => {
  localStorage.clear();
  const manifests = [
    { album_id: 'album3', qa_sets: ['old-set'] },
    { album_id: 'mixed', qa_sets: ['mixed-set'] },
  ] as ManifestSummary[];
  api.mockImplementation(async (path: string) => {
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
