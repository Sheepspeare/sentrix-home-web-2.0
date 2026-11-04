import { useState } from 'react';
import { cleanup, fireEvent, render, screen, within } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { ScoringSettings } from './ScoringSettings.tsx';
import { ScoringResults, ItemScoringResults } from './ScoringResults.tsx';
import { ScoringPromptEditor } from './ScoringPromptEditor.tsx';
import { ATM_PROFILE, OPEN_PROFILE, PROFILE_LABELS, defaultScoring, scoringPayload } from '../lib/scoring.ts';

afterEach(cleanup);
describe('独立评分标准', () => {
  it('默认只选开放式，双选后GT独立，说明默认折叠，至少保留一个标准', () => {
    function Settings() {const [value, setValue] = useState(defaultScoring());return <ScoringSettings value={value} onChange={setValue} />;}
    const {container} = render(<Settings />);
    const open = screen.getByRole('checkbox', {name: PROFILE_LABELS[OPEN_PROFILE]}) as HTMLInputElement;
    const atm = screen.getByRole('checkbox', {name: PROFILE_LABELS[ATM_PROFILE]}) as HTMLInputElement;
    expect(open.checked).toBe(true); expect(atm.checked).toBe(false);
    expect(container.querySelector('details')?.open).toBe(false);
    fireEvent.click(open); expect(open.checked).toBe(true);
    fireEvent.click(atm); fireEvent.click(screen.getByLabelText(PROFILE_LABELS[OPEN_PROFILE]+' · GT图片辅助'));
    expect((screen.getByLabelText(PROFILE_LABELS[ATM_PROFILE]+' · GT图片辅助') as HTMLInputElement).checked).toBe(false);
    expect((screen.getByLabelText(PROFILE_LABELS[OPEN_PROFILE]+' · GT元数据辅助') as HTMLInputElement).checked).toBe(false);
  });
  it('提交保留不同标准的草稿，主动清空也交给后端校验', () => {
    const selection=defaultScoring(); selection.profiles.push(ATM_PROFILE);
    const body=scoringPayload(selection, {[OPEN_PROFILE+':answer_quality']:'open', [ATM_PROFILE+':choice']:'', [ATM_PROFILE+':number']:'number', task_decision:'diagnostic'});
    expect(body.scoring_configs[OPEN_PROFILE].prompts).toEqual({answer_quality:'open'});
    expect(body.scoring_configs[ATM_PROFILE].prompts).toEqual({choice:'', number:'number'});
  });
  it('未执行标准固定显示未采集，部分评分只展示均值和覆盖率', () => {
    render(<ScoringResults summary={{scoring:{profiles:{[OPEN_PROFILE]:{status:'incomplete',scored:1,total:2,coverage:.5,scored_mean:1,normalized_mean:.5,total_score:null,diagnostic_pending:1}}}}} />);
    expect(screen.getByText('未采集')).toBeTruthy();
    expect(screen.getByText('未完成')).toBeTruthy();
    expect(screen.getByText('已评分均值 1.000/2 · 50.0%')).toBeTruthy();
    expect(screen.getByText('覆盖率 1/2 · 50.0%')).toBeTruthy();
    expect(screen.getByText(/公共诊断待完成 1 项/)).toBeTruthy();
  });
  it('完整合法0分显示0%，不显示未采集', () => {
    render(<ScoringResults summary={{scoring:{profiles:Object.fromEntries([OPEN_PROFILE,ATM_PROFILE].map(p=>[p,{status:'completed',scored:2,total:2,coverage:1,scored_mean:0,normalized_mean:0,total_score:0}]))}}} />);
    expect(screen.queryByText('未采集')).toBeNull();
    expect(screen.getAllByText('0.0%')).toHaveLength(2);
  });
  it('提示词按标准切换用途，列表只读，恢复仅触发当前项', () => {
    const change=vi.fn(), reset=vi.fn();
    const kinds=[OPEN_PROFILE+':answer_quality', ATM_PROFILE+':choice',ATM_PROFILE+':number',ATM_PROFILE+':open','task_decision','evidence'].map(kind=>({kind}));
    render(<ScoringPromptEditor judgePromptKinds={kinds} activePromptKind={ATM_PROFILE+':number'} onSwitchPromptKind={change} rejudgePrompt='numeric' onRejudgePromptChange={()=>{}} onResetPrompt={reset}/>);
    expect(within(screen.getByRole('combobox',{name:'提示词用途'})).getAllByRole('option')).toHaveLength(3);
    expect(screen.getByText(/列表题固定使用 Jaccard/)).toBeTruthy();
    fireEvent.click(screen.getByRole('button',{name:'恢复默认'}));expect(reset).toHaveBeenCalledOnce();
    fireEvent.change(screen.getByRole('combobox',{name:'提示词所属标准'}),{target:{value:'shared'}});expect(change).toHaveBeenCalledWith('task_decision');
  });
  it('逐题记录显示所属标准、轮次和真实请求，不丢弃合法0分', () => {
    const {container}=render(<ItemScoringResults results={[{key:'k',profile_id:ATM_PROFILE,batch_id:'batch-test',turn_index:0,status:'completed',score:0,max_score:1,requests:[{payload:{messages:[{role:'user',content:'exact saved input'}]},raw_response:{score:0}}]}]}/>);
    const request=Array.from(container.querySelectorAll('details')).find(d=>d.querySelector('summary')?.textContent==='真实Judge输入与响应（逐次请求）');
    expect(request).toBeTruthy();
    request!.open=true;fireEvent(request!,new Event('toggle'));
    expect(screen.getByText(/exact saved input/)).toBeTruthy();
    expect(screen.getByText(/batch-test/)).toBeTruthy();
  });
});
