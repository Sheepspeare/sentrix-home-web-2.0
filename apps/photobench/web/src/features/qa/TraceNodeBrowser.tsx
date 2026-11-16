import { useId, useState } from "react";
import type { RunItemDetail } from "../../api/types.ts";
import { turnTimeline, turnProgressEvents, runtimeTurns } from "../../lib/agentTrace.ts";
import { callStatus } from "../../lib/format.ts";
import { traceDurationLabel, traceNodeTitle } from "../../lib/traceNodes.ts";
import { toolStatusLabel } from "./qaShared.ts";
import { RawTraceRecord, TraceNodeDetails } from "./TraceNodeDetails.tsx";

export function TraceNodeBrowser({ detail, turnIndex, modelLabel }: { detail: RunItemDetail; turnIndex: number; modelLabel?: string }) {
  const [view, setView] = useState("flow");
  const [selection, setSelection] = useState<string | null>(null);
  const id = useId();
  const turn = runtimeTurns(detail).find((value, index) => Number(value.index ?? index) === turnIndex);
  const nodes = turnTimeline(detail, turnIndex).filter(entry => entry.kind !== "event");
  const selected = nodes.find(entry => entry.key === selection) ?? nodes[0];
  const events = turnProgressEvents(turn);
  const eventRecords = events.length ? events : Array.isArray(turn?.debug_trace) ? turn.debug_trace : [];
  const views = [{ key: "flow", title: "调用流程" }, { key: "events", title: `原始事件 · ${eventRecords.length} 条（核对用）` }, { key: "raw", title: "完整记录" }];
  return <section className="trace-node-browser" aria-label="执行轨迹节点视图">
    <div className="trace-view-tabs" role="tablist" aria-label="轨迹视图">{views.map(v =>
      <button key={v.key} type="button" role="tab" id={`${id}-${v.key}`} aria-selected={view === v.key}
        aria-controls={`${id}-view`} onClick={() => setView(v.key)}>{v.title}</button>)}</div>
    <div role="tabpanel" id={`${id}-view`} aria-labelledby={`${id}-${view}`}>
      {view === "flow" && <>
        <p className="trace-node-note">连线表示记录顺序；父调用按已保存身份关联。节点耗时只计自身，关联工具合计见“耗时与指标”。</p>
        <div className="trace-node-workspace">
          <nav aria-label="调用节点" className="trace-node-route"><ol>{nodes.map((entry, index) => {
            const raw = entry.call ?? entry.tool ?? {};
            return <li key={entry.key}><button type="button" aria-pressed={selected?.key === entry.key}
              className={`trace-route-node trace-route-${entry.kind}`} onClick={() => setSelection(entry.key)}>
              <span className="trace-node-number">{index + 1}</span><span>
                <strong>{traceNodeTitle(entry)}</strong>
                {raw.step_id != null ? <small>{String(raw.step_id)}{raw.attempt != null ? ` · attempt ${String(raw.attempt)}` : ""}</small> : null}
                <small>{traceDurationLabel(entry)}{entry.kind !== "answer" ? ` · ${entry.tool ? toolStatusLabel(raw) : callStatus(raw)}` : ""}</small>
                {entry.unbound ? <small>父调用未确定</small> : entry.tool?.parent_step_id ? <small>父步骤 {String(entry.tool.parent_step_id)}</small> : null}
              </span>
            </button></li>;
          })}</ol></nav>
          <section className="trace-node-inspector" aria-label="节点详情" aria-live="polite">
            {selected ? <><header><h5>{traceNodeTitle(selected)}</h5><span>{traceDurationLabel(selected)}</span></header>
              <TraceNodeDetails key={selected.key} detail={detail} turnIndex={turnIndex} entry={selected} modelLabel={modelLabel}
                initialTab={selected.kind === "answer" ? "output" : "input"} /></> : <p>此存档没有可展示的调用轨迹。</p>}
          </section>
        </div>
      </>}
      {view === "events" && <div className="trace-node-events">{eventRecords.map((event, index) =>
        <RawTraceRecord key={index} label={`${index + 1}. ${String((event as Record<string, unknown>).kind ?? (event as Record<string, unknown>).type ?? "事件")}`} value={event} />)}</div>}
      {view === "raw" && <RawTraceRecord label="本题完整已保存对象 · 全部字段" value={detail} path="$" />}
    </div>
  </section>;
}
