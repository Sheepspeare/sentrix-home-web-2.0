import { useEffect, useRef, useState } from 'react';
import { api, post } from '../../api/client.ts';
import { mediaRefs } from '../../api/media.ts';
import type { ChatSession, InteractiveModel, ManifestSummary, QaRow, RunItemDetail } from '../../api/types.ts';
import { useQaBrowser } from '../../hooks/useQaBrowser.ts';
import { fmtMs } from '../../lib/format.ts';
import { toolRecallMedia, itemEvidenceMedia } from '../../lib/evidence.ts';
import { DebugTracePanel } from '../qa/DebugTracePanel.tsx';
import { ModelSelect, type ModelOptions } from '../interactive/ModelSelect.tsx';
import { MediaGrid } from '../interactive/MediaGrid.tsx';

type Space = { id: string; name?: string; created_at?: string };
type Base = { scope_id: string; album_id: string; ready?: boolean };
const active = (status: string) => ['preparing', 'running', 'cancelling'].includes(status);
const stateLabel = (status: string) => ({ preparing: '准备模型', running: '回答中', cancelling: '正在取消', ready: '可继续提问', failed: '执行失败', cancelled: '已取消', outcome_unknown: '等待确认请求状态' }[status] || status);

export function ChatView({ sentrixUrl, manifests, models, busy, onOpenMedia }: {
  sentrixUrl: string; manifests: ManifestSummary[]; models: ModelOptions; busy: boolean;
  onOpenMedia: (target: { url: string; name: string }) => void;
}) {
  const [spaces, setSpaces] = useState<Space[]>([]), [bases, setBases] = useState<Base[]>([]);
  const [scope, setScope] = useState(''), [model, setModel] = useState<InteractiveModel>({});
  const [sessions, setSessions] = useState<ChatSession[]>([]), [session, setSession] = useState<ChatSession | null>(null);
  const [message, setMessage] = useState(''), [error, setError] = useState(''), [sending, setSending] = useState(false);
  const [picker, setPicker] = useState(false), [selectedQa, setSelectedQa] = useState<QaRow | null>(null);
  const browser = useQaBrowser({ sentrixUrl, manifests });
  const selectedId = useRef('');
  const loadSessions = async () => { const data = await api<{ items: ChatSession[] }>('/chat/sessions'); setSessions(data.items); return data.items; };
  const select = async (id: string) => {
    selectedId.current = id;
    try {
      const data = await api<ChatSession>(`/chat/sessions/${encodeURIComponent(id)}`);
      if (selectedId.current !== id) return;
      setSession(data); setScope(data.scope_id); setModel({ profile: data.model_profile, base_url: String(data.runtime_snapshot?.model_base_url || ''), manager_url: String(data.runtime_snapshot?.vllm_manager_url || '') }); localStorage.setItem('photobench-chat-session', id);
    } catch (e) { setError((e as Error).message); }
  };
  useEffect(() => {
    let disposed = false;
    void (async () => {
      try {
        const [data, history] = await Promise.all([
          api<{ spaces: Space[]; reuse_bases: Base[] }>(`/memory-spaces?sentrix_url=${encodeURIComponent(sentrixUrl)}`), loadSessions(),
        ]);
        if (disposed) return;
        setSpaces(data.spaces); setBases(data.reuse_bases); setScope(data.spaces[0]?.id || '');
        const remembered = localStorage.getItem('photobench-chat-session');
        if (remembered && history.some(s => s.session_id === remembered)) void select(remembered);
      } catch (e) { if (!disposed) setError((e as Error).message); }
    })();
    return () => { disposed = true; selectedId.current = ''; };
  }, [sentrixUrl]); // oxlint-disable-line react-hooks/exhaustive-deps
  const id = session?.session_id;
  const status = session?.status || '';
  useEffect(() => {
    if (!id || !active(status)) return;
    let pending = false, disposed = false;
    const timer = window.setInterval(() => {
      if (pending) return;
      pending = true;
      void api<ChatSession>(`/chat/sessions/${encodeURIComponent(id)}`).then(data => {
        if (!disposed && selectedId.current === id) setSession(data);
        if (!active(data.status)) void loadSessions();
      }).catch((e: Error) => { if (!disposed) setError(e.message); }).finally(() => { pending = false; });
    }, 1200);
    return () => { disposed = true; window.clearInterval(timer); };
  }, [id, status]); // oxlint-disable-line react-hooks/exhaustive-deps
  useEffect(() => {
    if (picker && browser.qaBrowserAlbum && browser.qaBrowserOptions.includes(browser.qaBrowserSet)) void browser.loadQaBrowser();
  }, [picker, browser.qaBrowserAlbum, browser.qaBrowserSet]); // oxlint-disable-line react-hooks/exhaustive-deps
  const reset = () => { selectedId.current = ''; setSession(null); setSelectedQa(null); localStorage.removeItem('photobench-chat-session'); };
  const send = async (row?: QaRow) => {
    setSending(true); setError('');
    try {
      const result = session && !row
        ? await post<ChatSession>(`/chat/sessions/${encodeURIComponent(session.session_id)}/turns`, { message })
        : await post<ChatSession>('/chat/sessions', { scope_id: scope, model, ...(row ? {
          album_id: browser.qaBrowserAlbum, qa_set: browser.qaBrowserSet, qa_id: row.qa_id,
        } : { message }) });
      selectedId.current = result.session_id; setSession(result); setMessage(''); setPicker(false); setSelectedQa(null);
      localStorage.setItem('photobench-chat-session', result.session_id); await loadSessions();
    } catch (e) { setError((e as Error).message); }
    finally { setSending(false); }
  };
  const sourceAlbum = bases.find(b => b.scope_id === scope)?.album_id;
  const source = session?.source;
  const openPicker = () => {
    const album = sourceAlbum || browser.qaBrowserAlbum;
    const sets = manifests.find(m => m.album_id === album)?.qa_sets || browser.qaBrowserOptions;
    const preferred = source?.album_id === album ? source.qa_set : browser.qaBrowserSet;
    browser.setQaBrowserAlbum(album);
    browser.setQaBrowserSet(sets.includes(preferred) ? preferred : sets[0] || '');
    setSelectedQa(null); setPicker(true);
  };
  const ownActive = Boolean(session && active(session.status));
  return <section className="chat-view">
    <div className="section-head"><div><h2>聊天对话</h2><p className="muted">选择相册与模型，自由提问或运行题集中的一道题。</p></div><button className="btn ghost" disabled={ownActive || sending} onClick={reset}>新建对话</button></div>
    <div className="chat-controls">
      <label>相册<select aria-label="聊天相册" value={scope} disabled={ownActive || sending} onChange={e => { reset(); setScope(e.target.value); }}><option value="">选择相册</option>{spaces.map(s => <option key={s.id} value={s.id}>{s.name || s.id}{s.created_at ? ` · ${s.created_at.slice(0, 16)}` : ''}</option>)}</select></label>
      <ModelSelect key={session ? session.session_id : 'new'} options={models} value={model} onChange={value => { reset(); setModel(value); }} disabled={ownActive || sending}/>
    </div>
    <div className="chat-layout"><aside className="chat-history"><h3>会话记录</h3>{sessions.map(s => <button key={s.session_id} className={'chat-history-item' + (s.session_id === id ? ' selected' : '')} disabled={sending} onClick={() => void select(s.session_id)}><strong>{s.title}</strong><small>{s.model_name || '当前模型'} · {stateLabel(s.status)}</small></button>)}{!sessions.length && <p className="muted">暂无记录</p>}</aside>
      <div className="chat-main">
        {error && <p className="error" role="alert">{error}</p>}
        {session && <div className="chat-state"><strong>{session.model_name || '模型准备中'}</strong><span>{stateLabel(session.status)}</span><details><summary>运行配置</summary><pre>{JSON.stringify({ model: session.model_identity, configuration: session.runtime_snapshot, preparation: session.model_preparation }, null, 2)}</pre></details></div>}
        {session?.error && <p role="alert" className="error">{session.error}</p>}
        {source && <details className="chat-source" open><summary>题集对照 · {source.row.qa_id} · {source.qa_set}</summary><p>{source.row.question}</p><h4>标准答案</h4><p>{source.row.answer}</p><h4>检索 GT 媒体</h4><MediaGrid items={mediaRefs(source.row, 'retrieval')} albumId={source.album_id} onOpenMedia={onOpenMedia}/><h4>答案证据 GT 媒体</h4><MediaGrid items={mediaRefs(source.row, 'answer_evidence')} albumId={source.album_id} onOpenMedia={onOpenMedia}/></details>}
        <div className="chat-messages">{session?.turns?.map((turn, i) => {
          const item = session.items?.[i];
          return <article className="chat-exchange" key={turn.turn_id}><div className="chat-question"><small>提问 {i + 1}</small><p>{turn.message}</p></div><div className="chat-answer"><small>{session.model_name || '模型'} · {fmtMs(item?.wall_clock_ms)}</small><p>{String(item?.final_answer || item?.answer || item?.error || (['pending', 'running'].includes(turn.status) ? '正在回答…' : turn.status))}</p>
            {item && <ChatResult item={item} albumId={session.album_id} modelLabel={session.model_name || ''} onOpenMedia={onOpenMedia}/>}</div></article>;
        })}{!session && <p className="chat-empty">输入问题开始对话，也可以从题集中选择一道题。</p>}</div>
        <form className="chat-composer" onSubmit={e => { e.preventDefault(); void send(); }}>
          <textarea aria-label="聊天问题" placeholder="输入问题，支持连续追问…" maxLength={2000} value={message} disabled={ownActive || sending} onChange={e => setMessage(e.target.value)}/>
          <div className="chat-composer-actions"><button className="btn ghost" type="button" disabled={!scope || ownActive || sending || busy} onClick={openPicker}>从题集选题</button>
            {ownActive ? <button type="button" className="btn ghost" onClick={() => { void post(`/chat/sessions/${encodeURIComponent(id!)}/cancel`, {}).then(() => select(id!)).catch(e => setError((e as Error).message)); }}>停止回答</button> : <button className="btn" disabled={!scope || !message.trim() || sending || busy}>{sending ? '正在发送…' : '发送'}</button>}
          </div>
        </form>
      </div>
    </div>
    {picker && <div className="modal-backdrop" onClick={() => setPicker(false)}><section className="interactive-dialog" role="dialog" aria-modal="true" aria-label="从题集选题" onClick={e => e.stopPropagation()}><div className="section-head"><h3>从题集选题</h3><button className="btn ghost compact" onClick={() => setPicker(false)}>关闭</button></div>
      <div className="chat-controls"><label>数据集<select value={browser.qaBrowserAlbum} onChange={e => { browser.setQaBrowserAlbum(e.target.value); setSelectedQa(null); }}>{manifests.map(m => <option key={m.album_id} value={m.album_id}>{m.album_name || m.album_id}</option>)}</select></label><label>题集<select value={browser.qaBrowserSet} onChange={e => { browser.setQaBrowserSet(e.target.value); setSelectedQa(null); }}>{browser.qaBrowserOptions.map(s => <option key={s} value={s}>{s}</option>)}</select></label></div>
      <input aria-label="搜索聊天题目" placeholder="搜索题号、问题、答案或标签" value={browser.qaBrowserSearch} onChange={e => browser.setQaBrowserSearch(e.target.value)}/>
      {browser.qaBrowserError && <p role="alert">{browser.qaBrowserError}</p>}
      {sourceAlbum !== browser.qaBrowserAlbum && <p>请先选择与该数据集对应的相册。</p>}
      <div className="chat-qa-list">{browser.qaBrowserLoading ? <p>加载题集…</p> : browser.visibleQaBrowserItems.map(row => <button className={'chat-qa-option' + (selectedQa?.qa_id === row.qa_id ? ' selected' : '')} key={row.qa_id} onClick={() => setSelectedQa(row)}><strong>{row.qa_id}</strong> {row.question}</button>)}</div>
      {selectedQa && <div className="chat-qa-preview"><h4>{selectedQa.question}</h4>{selectedQa.conversation && <pre>{JSON.stringify(selectedQa.conversation, null, 2)}</pre>}<p>标准答案：{selectedQa.answer}</p><h4>检索 GT</h4><MediaGrid items={mediaRefs(selectedQa, 'retrieval')} albumId={browser.qaBrowserAlbum} onOpenMedia={onOpenMedia}/><h4>答案证据 GT</h4><MediaGrid items={mediaRefs(selectedQa, 'answer_evidence')} albumId={browser.qaBrowserAlbum} onOpenMedia={onOpenMedia}/></div>}
      {error && <p className="error" role="alert">{error}</p>}<button className="btn" disabled={!selectedQa || sourceAlbum !== browser.qaBrowserAlbum || sending || busy} onClick={() => void send(selectedQa!)}>运行此题（新对话）</button>
    </section></div>}
  </section>;
}

function ChatResult({ item, albumId, modelLabel, onOpenMedia }: {
  item: RunItemDetail; albumId: string; modelLabel: string; onOpenMedia: (target: { url: string; name: string }) => void;
}) {
  return <><h4>召回候选媒体</h4><MediaGrid items={toolRecallMedia(item, albumId)} albumId={albumId} onOpenMedia={onOpenMedia}/><h4>最终交付媒体</h4><MediaGrid items={itemEvidenceMedia(item, albumId)} albumId={albumId} onOpenMedia={onOpenMedia}/><DebugTracePanel detail={item} modelLabel={modelLabel}/></>;
}
