import { PROFILE_LABELS, OPEN_PROFILE } from '../lib/scoring.ts';
import { ScoringRequestLog } from './ScoringRequestLog.tsx';
type Dict = Record<string, unknown>;
const obj = (v: unknown): Dict => v && typeof v === 'object' ? v as Dict : {};
const percent = (v:unknown) => typeof v === 'number' ? (v*100).toFixed(1)+'%' : '未完成';
export function ScoringResults({summary}: {summary:unknown}) {
  const s = obj(summary), scoring = obj(s.scoring), profiles = obj(scoring.profiles);
  return <section className='scoring-results' aria-label='两套评分结果'><h3>评分结果</h3><div className='scoring-result-grid'>
    {Object.entries(PROFILE_LABELS).map(([id,label]) => {const result=obj(profiles[id]), gt=obj(result.gt_config), judge=obj(result.judge);
      const collected = Boolean(result.status && result.status !== 'not_collected');
      const complete = typeof result.total_score === 'number';
      const value = complete ? result.total_score : result.normalized_mean;
      return <article className='scoring-metric-card' key={id} aria-label={label}>
        <header className='scoring-result-head'><b>{label}</b><span className={'phase-status ' + (complete ? 'completed' : collected ? 'partial' : '')}>{!collected ? '未采集' : complete ? '已完成' : '未完成'}</span></header>
        {!collected ? <div className='scoring-result-empty'>选择该标准并补评后，将显示得分与覆盖率。</div> : <>
          <div className='scoring-value-row'><strong>{typeof value === 'number' ? percent(value) : '—'}</strong><span>{complete ? '总分' : '已评分均值'}{id === OPEN_PROFILE && typeof result.scored_mean === 'number' ? ' · '+result.scored_mean.toFixed(3)+'/2' : ''}</span></div>
          <div className='scoring-coverage'><span>覆盖率 {String(result.scored)}/{String(result.total)} · {percent(result.coverage)}</span><progress aria-label={label+'评分覆盖率'} max={1} value={typeof result.coverage === 'number' ? result.coverage : 0} /></div>
          {Number(result.diagnostic_pending) > 0 && <p className='scoring-pending'>公共诊断待完成 {String(result.diagnostic_pending)} 项，评分覆盖率单独计算。</p>}
          <details className='scoring-result-details'><summary>评分版本与配置</summary><dl>
            <div><dt>题集</dt><dd>{String(result.qa_set || '-')}</dd></div>
            <div><dt>规则版本</dt><dd>{String(result.rule_version || '-')}</dd></div>
            <div><dt>Judge</dt><dd>{String(judge.model || '-')}</dd></div>
            <div><dt>GT辅助</dt><dd>图片{gt.use_gt_images ? '开启' : '关闭'} · 元数据{gt.use_gt_metadata ? '开启' : '关闭'}</dd></div>
            <div><dt>实际Judge请求</dt><dd>{String(result.judge_calls || 0)} 次</dd></div>
            <div className='scoring-config-wide'><dt>批次</dt><dd><code>{String(result.batch_id)}</code></dd></div>
            <div className='scoring-config-wide'><dt>QA快照</dt><dd><code>{String(result.qa_definition_sha256 || '-')}</code></dd></div>
          </dl></details>
        </>}
      </article>;
    })}
  </div><p className='scoring-result-note'>未完成时展示已评分均值，完整总分需覆盖全部题目。ATM-Bench式QS按题等权平均；选择与数值题使用Judge匹配。跨测评比较需核对题集、规则版本、Judge和GT配置。</p></section>;
}
export function ItemScoringResults({results, diagnostics}: {results:unknown; diagnostics?:unknown}) {
  const rows = Array.isArray(results) ? results.map(obj) : [];
  return <section aria-label='分标准评分记录'><h4>分标准评分记录</h4>
    {Object.entries(PROFILE_LABELS).map(([id,label]) => {const selected=rows.filter(r => r.profile_id === id);return <div key={id}><b>{label}</b>{!selected.length ? <p>未采集</p> : selected.map(r => <details key={String(r.key)}><summary>{String(r.batch_id)} · 第{Number(r.turn_index)+1}轮 · {r.score == null ? String(r.status) : String(r.score)+'/'+String(r.max_score)}{r.is_evaluation_turn ? '（计入总分）' : '（诊断轮）'}</summary><p>{String(r.reason || '')}</p>{r.formula != null && <p>{String(r.formula)} · GT {JSON.stringify(r.target)} · 返回 {JSON.stringify(r.predicted)}</p>}<ScoringRequestLog requests={[...(Array.isArray(r.history) ? r.history : []).flatMap(h => Array.isArray(obj(h).requests) ? obj(h).requests as unknown[] : []),...(Array.isArray(r.requests) ? r.requests : [])]} /></details>)}</div>;})}
    <ScoringRequestLog requests={diagnostics} title='公共诊断记录（相同配置复用）' />
  </section>;
}
