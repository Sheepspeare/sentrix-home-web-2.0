import { useEffect, useState } from 'react';
import { api, post } from '../../api/client.ts';
import type { InteractiveModel, Run, RunItemSummary } from '../../api/types.ts';
import { fmtMs, fmtPct, modelName } from '../../lib/format.ts';
import { ModelSelect, type ModelOptions } from './ModelSelect.tsx';

function scoringLabel(run: Run): string {
  const summaries = (run.summary?.scoring as { profiles?: Record<string, unknown> } | undefined)?.profiles;
  const rows = Array.isArray(summaries) ? summaries : Object.values((summaries || {}) as Record<string, unknown>);
  return rows.map(value => {
    const row = value as Record<string, unknown>;
    return row.max_score === 2 && typeof row.scored_mean === 'number' ? `${row.scored_mean.toFixed(2)} / 2` : typeof row.total_score === 'number' ? `${(row.total_score * 100).toFixed(1)}%` : '待评分';
  }).join(' / ') || '未评分';
}

export function RetestDialog({ run, question, models, busy, onClose, onSelectRun }: {
  run: Run; question: RunItemSummary; models: ModelOptions; busy: boolean;
  onClose: () => void; onSelectRun: (id: string) => void;
}) {
  const [model, setModel] = useState<InteractiveModel>({});
  const [history, setHistory] = useState<Run[]>([]);
  const [error, setError] = useState('');
  const [starting, setStarting] = useState(false);
  const load = async () => {
    try { const data = await api<{ items: Run[] }>(`/runs/${encodeURIComponent(run.run_id!)}/retests?qa_id=${encodeURIComponent(String(question.qa_id))}`); setHistory(data.items); }
    catch (e) { setError((e as Error).message); }
  };
  useEffect(() => { void load(); }, [run.run_id, question.qa_id]); // oxlint-disable-line react-hooks/exhaustive-deps
  const start = async () => {
    setStarting(true); setError('');
    try {
      const result = await post<{ run_id: string }>(`/runs/${encodeURIComponent(run.run_id!)}/retest`, {
        qa_id: question.qa_id, model,
        scoring_profile: run.selected_scoring_profile || undefined,
        scoring_batch: run.selected_scoring_batch || undefined,
      });
      onSelectRun(result.run_id);
    } catch (e) { setError((e as Error).message); }
    finally { setStarting(false); }
  };
  return <div className="modal-backdrop" onClick={onClose}><section className="interactive-dialog" role="dialog" aria-modal="true" aria-label="单题重测与模型比较" onClick={e => e.stopPropagation()}>
    <div className="section-head"><h3>单题重测与模型比较</h3><button className="btn ghost compact" onClick={onClose}>关闭</button></div>
    <p>第 {Number(question.index) + 1} 题 · {question.qa_id}</p><p>{String(question.display_question || question.question || '')}</p>
    <p className="muted small">复用原相册；多轮题从第一轮开始。评分沿用当前所选批次，原答卷及总分保留。</p>
    <ModelSelect options={models} value={model} onChange={setModel} disabled={starting}/>
    {error && <p role="alert" className="error">{error}</p>}
    <button className="btn" disabled={busy || starting} onClick={() => void start()}>{starting ? '正在启动…' : busy ? '等待当前任务结束' : '开始重测'}</button>
    <h4>比较记录</h4>
    <div className="interactive-table"><table><thead><tr><th>记录</th><th>模型</th><th>得分</th><th>交付召回</th><th>耗时</th><th>状态</th><th></th></tr></thead><tbody>
      <tr><td>原结果</td><td>{modelName(run)}</td><td>{Object.values(question.profile_scores || {}).map(s => typeof s.score === 'number' ? String(s.score) : '未评分').join(' / ') || String(question.judge?.score ?? '未评分')}</td><td>{fmtPct(question.retrieval_recall)}</td><td>{fmtMs(question.wall_clock_ms)}</td><td>{String(question.execution_status || '')}</td><td></td></tr>
      {history.map(row => <tr key={row.run_id}><td>{String(row.created_at || row.run_id)}</td><td>{modelName(row)}</td><td>{scoringLabel(row)}</td><td>{fmtPct(row.retest_summary?.delivery_recall)}</td><td>{fmtMs(row.retest_summary?.wall_clock_ms)}</td><td>{row.status}</td><td><button className="btn ghost compact" onClick={() => onSelectRun(row.run_id!)}>查看回答与轨迹</button></td></tr>)}
    </tbody></table></div>
    <p className="muted small">原结果耗时 {fmtMs(question.wall_clock_ms)}；每次重测可独立查看实际模型、评分版本与耗时。</p>
  </section></div>;
}
