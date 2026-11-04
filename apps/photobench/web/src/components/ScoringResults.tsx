import { PROFILE_LABELS, OPEN_PROFILE } from '../lib/scoring.ts';
import { ScoringRequestLog } from './ScoringRequestLog.tsx';
type Dict = Record<string, unknown>;
const obj = (v: unknown): Dict => v && typeof v === 'object' ? v as Dict : {};
const percent = (v:unknown) => typeof v === 'number' ? (v*100).toFixed(1)+'%' : '未完成';
export function ScoringResults({summary}: {summary:unknown}) {
  const s = obj(summary), scoring = obj(s.scoring), profiles = obj(scoring.profiles);
  return <section aria-label='两套评分结果'><h3>评分结果</h3><div className='phase-list key-metric-list'>
    {Object.entries(PROFILE_LABELS).map(([id,label]) => {const result=obj(profiles[id]), gt=obj(result.gt_config), judge=obj(result.judge);
      return <article className='phase-card scoring-metric-card' key={id}><b>{label}</b>
        {result.status === 'not_collected' || !result.status ? <p>未采集</p> : <>
          <p className='key-metric-value'>{percent(result.total_score)}</p>
          <p>已评分均值 {id === OPEN_PROFILE && typeof result.scored_mean === 'number' ? result.scored_mean.toFixed(3) + '/2 · ' : ''}{percent(result.normalized_mean)}</p>
          <p>覆盖率 {String(result.scored)}/{String(result.total)} · {percent(result.coverage)}</p>
          {Number(result.diagnostic_pending) > 0 && <p className="muted small">公共诊断待完成 {String(result.diagnostic_pending)} 项，评分覆盖率单独计算。</p>}
          <details className='small'><summary>评分版本与配置</summary><p>题集 {String(result.qa_set || '-')} · {String(result.rule_version)} · Judge {String(judge.model || '-')}</p><p>GT图片 {gt.use_gt_images ? '开启' : '关闭'} · GT元数据 {gt.use_gt_metadata ? '开启' : '关闭'}</p><p>批次 {String(result.batch_id)}</p><p>QA快照 {String(result.qa_definition_sha256 || '-')}</p><p>实际Judge请求 {String(result.judge_calls || 0)} 次</p></details>
        </>}
      </article>;
    })}
  </div><p className='muted small'>ATM-Bench式QS按QA等权平均；选择与数值题使用Judge匹配。跨测评比较需核对题集、规则版本、Judge和GT配置。</p></section>;
}
export function ItemScoringResults({results, diagnostics}: {results:unknown; diagnostics?:unknown}) {
  const rows = Array.isArray(results) ? results.map(obj) : [];
  return <section aria-label='分标准评分记录'><h4>分标准评分记录</h4>
    {Object.entries(PROFILE_LABELS).map(([id,label]) => {const selected=rows.filter(r => r.profile_id === id);return <div key={id}><b>{label}</b>{!selected.length ? <p>未采集</p> : selected.map(r => <details key={String(r.key)}><summary>{String(r.batch_id)} · 第{Number(r.turn_index)+1}轮 · {r.score == null ? String(r.status) : String(r.score)+'/'+String(r.max_score)}{r.is_evaluation_turn ? '（计入总分）' : '（诊断轮）'}</summary><p>{String(r.reason || '')}</p>{r.formula != null && <p>{String(r.formula)} · GT {JSON.stringify(r.target)} · 返回 {JSON.stringify(r.predicted)}</p>}<ScoringRequestLog requests={[...(Array.isArray(r.history) ? r.history : []).flatMap(h => Array.isArray(obj(h).requests) ? obj(h).requests as unknown[] : []),...(Array.isArray(r.requests) ? r.requests : [])]} /></details>)}</div>;})}
    <ScoringRequestLog requests={diagnostics} title='公共诊断记录（相同配置复用）' />
  </section>;
}
