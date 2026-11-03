/** Controlled scoring settings and run actions. */
import { GtImageToggle } from "../../components/GtImageToggle.tsx";
import { runModeMeta, startDisabledReason, type StartDisabledInput } from "./rules.ts";

export interface StartActionsProps extends StartDisabledInput {
  deleteScopeAfterRun: boolean;
  onDeleteScopeAfterRunChange: (next: boolean) => void;
  onStart: () => void;
  onStop: () => void;
  stopping?: boolean;
  openScale?: "ternary" | "binary";
  onOpenScaleChange?: (value: "ternary" | "binary") => void;
  choiceSemanticAux?: boolean;
  onChoiceSemanticAuxChange?: (value: boolean) => void;
  judgeUseGtImages?: boolean;
  onJudgeUseGtImagesChange?: (value: boolean) => void;
}

export function StartActions(props: StartActionsProps) {
  const {
    runMode,
    hasRunning,
    suiteRunning,
    deleteScopeAfterRun,
    onDeleteScopeAfterRunChange,
    onStart,
    onStop,
  } = props;

  const reason = startDisabledReason(props);
  const buttonLabel = hasRunning ? "已有任务运行中" : runModeMeta(runMode).button;

  const locked = suiteRunning || hasRunning;
  return (
    <div className="start-settings">
      {runMode !== "build" && (
        <div className="start-scoring-grid" aria-label="评分设置">
          <div className="start-setting">
            <span className="field-label">评分参考</span>
            <GtImageToggle checked={props.judgeUseGtImages ?? false} disabled={locked}
              onChange={value => props.onJudgeUseGtImagesChange?.(value)} />
          </div>
          <label className="start-setting">
            <span className="field-label">开放题量表</span>
            <select aria-label="开放题量表" value={props.openScale ?? "ternary"} disabled={locked}
              onChange={event => props.onOpenScaleChange?.(event.target.value as "ternary" | "binary")}>
              <option value="ternary">PhotoBench 0/1/2</option>
              <option value="binary">0/1（ATM对齐）</option>
            </select>
            <span className="config-help">PhotoBench 开放题适用；ATM 使用官方量表</span>
          </label>
          <div className="start-setting">
            <span className="field-label">选择题辅分</span>
            <label className={`start-check${props.choiceSemanticAux ? " active" : ""}`}>
              <input type="checkbox" checked={props.choiceSemanticAux ?? false} disabled={locked}
                onChange={event => props.onChoiceSemanticAuxChange?.(event.target.checked)} />
              选择题开放语义辅分
            </label>
            <span className="config-help">额外比较开放回答与参考答案，主分仍按选项匹配</span>
          </div>
        </div>
      )}
      <div className="start-action-bar">
        <div className="start-primary-actions">
          <button className="btn" type="button" disabled={Boolean(reason)} title={reason || undefined} onClick={onStart}>
            {buttonLabel}
          </button>
          <button className="btn warn" type="button" disabled={Boolean(props.stopping)} onClick={onStop}>停止全部</button>
        </div>
        {runMode === "full" && (
          <label className={`delete-scope-toggle${deleteScopeAfterRun ? " active" : ""}${locked ? " disabled" : ""}`}
            title="只在完整链路测试结束后删除本次生成的相册，谨慎开启">
            <input type="checkbox" checked={deleteScopeAfterRun} disabled={locked}
              onChange={event => onDeleteScopeAfterRunChange(event.target.checked)} />
            完成后删除相册
          </label>
        )}
      </div>
      {reason && reason !== buttonLabel && <span className="config-help start-disabled-reason">{reason}</span>}
    </div>
  );
}
