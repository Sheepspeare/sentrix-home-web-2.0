import type { InteractiveModel } from '../../api/types.ts';

export interface ModelOptions {
  profiles: Array<{ id: string; available?: boolean; label?: string }>;
  targets: Record<string, { label?: unknown; model_base_url?: unknown; manager_url?: unknown }>;
  managerUrl: string;
  modelEndpoint: string;
}

export function ModelSelect({ options, value, onChange, disabled = false }: {
  options: ModelOptions; value: InteractiveModel; onChange: (value: InteractiveModel) => void; disabled?: boolean;
}) {
  const choice = value.profile && value.profile !== '__current__' ? 'profile:' + value.profile : value.target_id ? 'target:' + value.target_id : value.base_url !== undefined ? 'custom' : 'current';
  return <div className="interactive-model">
    <label>模型<select aria-label="问答模型" value={choice} disabled={disabled} onChange={e => {
      const next = e.target.value;
      if (next.startsWith('profile:')) onChange({ profile: next.slice(8), base_url: options.modelEndpoint, manager_url: options.managerUrl });
      else if (next.startsWith('target:')) onChange({ target_id: next.slice(7) });
      else if (next === 'custom') onChange({ base_url: options.modelEndpoint, endpoint_model: '' });
      else onChange({});
    }}>
      <option value="current">当前部署模型</option>
      {options.profiles.map(p => <option key={p.id} value={'profile:' + p.id} disabled={p.available === false}>{p.label || p.id}</option>)}
      {Object.entries(options.targets).map(([id, target]) => <option key={id} value={'target:' + id}>{String(target.label || id)}</option>)}
      <option value="custom">其他模型端点</option>
    </select></label>
    {choice === 'custom' && <>
      <label>模型服务地址<input aria-label="问答模型地址" value={value.base_url || ''} placeholder="http://host:port/v1" disabled={disabled} onChange={e => onChange({ ...value, base_url: e.target.value })}/></label>
      <label>模型名（可选）<input aria-label="问答模型名" value={value.endpoint_model || ''} disabled={disabled} onChange={e => onChange({ ...value, endpoint_model: e.target.value })}/></label>
    </>}
  </div>;
}
