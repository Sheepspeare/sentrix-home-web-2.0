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
          <details className="start-scoring-help">
            <summary>评分设置说明</summary>
            <p>这三项控制 Judge（评分模型）的评分方式。答题模型仍按原问题作答。设置随新任务保存，运行中的任务继续使用启动时的设置。</p>
            <p><strong>GT 图片辅助评分</strong>：把标准答案关联的图片交给 Judge 核对，需使用支持图片输入的评分模型。目前适用于 PhotoBench 0/1/2 开放题；0/1 开放题、选择题和 ATM 专用评分不附图。视频也不通过此开关发送。</p>
            <p><strong>开放题量表</strong>：PhotoBench 0/1/2 中，0 表示错误或未有效回答，1 表示部分正确，2 表示完整正确。改为 0/1 后，完整正确得 1，部分正确或错误得 0。此项仅影响 PhotoBench 开放题；ATM 题保留官方评分规则。</p>
            <p><strong>选择题开放语义辅分</strong>：选择题选项仅供评测端使用，Judge 将开放回答逐项匹配，再由程序核对正确项；全部不匹配归为“其他”，主分为 0。开启辅分会增加一次 Judge 调用，单独比较开放回答与参考答案，给出 0/1 分，主分保持按选项匹配计算。</p>
            <p>选择题回答若缺少区分选项的关键信息，也归“其他”；例如只回答“赵县的一座石桥”，不能在“永通桥”和“赵州桥”之间任选一个。选项本身语义重叠会标为待复核。Judge 格式错误、返回矛盾或图片读取失败均保留失败记录，不直接计 0 分。</p>
          </details>
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
