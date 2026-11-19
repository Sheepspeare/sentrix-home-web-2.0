import { useEffect, useId, useRef, useState } from "react";
import type { RunItemDetail } from "../../api/types.ts";
import { api } from "../../api/client.ts";
import { turnTimeline, turnProgressEvents, runtimeTurns } from "../../lib/agentTrace.ts";
import { callStatus } from "../../lib/format.ts";
import { traceDurationLabel, traceNodeTitle } from "../../lib/traceNodes.ts";
import { toolStatusLabel } from "./qaShared.ts";
import { RawTraceRecord, TraceNodeDetails } from "./TraceNodeDetails.tsx";

interface LazyTraceNode {
  node_id: string;
  kind: string;
  turn_index: number;
  metadata?: Record<string, unknown>;
  source_path?: (string | number)[];
  fields?: string[];
}

interface LazyTracePage {
  nodes: LazyTraceNode[];
  total: number;
  offset?: number;
  next_offset?: number | null;
  read_revision?: string;
}

interface LazyTraceContent {
  text: string;
  offset: number;
  next_offset?: number | null;
  source_path?: (string | number)[];
}

export function LazyTraceNodeBrowser({ detail, turnIndex, initialKind = "calls" }: { detail: RunItemDetail; turnIndex: number; initialKind?: string }) {
  const storage = (detail.trace_storage ?? {}) as Record<string, unknown>;
  const runId = String(detail.run_id ?? "");
  const itemIndex = Number(detail.index);
  const revision = String(storage.revision ?? "");
  const [page, setPage] = useState<LazyTracePage | null>(null);
  const [selected, setSelected] = useState<string | null>(null);
  const [content, setContent] = useState<LazyTraceContent | null>(null);
  const [loading, setLoading] = useState(false);
  const [chunkOffsets, setChunkOffsets] = useState<number[]>([]);
  const [loadingMore, setLoadingMore] = useState(false);
  const [error, setError] = useState("");
  const [kind, setKind] = useState(initialKind);
  const epoch = useRef(0);
  const base = `/runs/${encodeURIComponent(runId)}/items/${itemIndex}/trace?turn=${turnIndex}&revision=${encodeURIComponent(revision)}`;
  const listUrl = `${base}&kind=${kind}`;

  useEffect(() => {
    let alive = true;
    const controller = new AbortController();
    epoch.current += 1;
    setPage(null); setSelected(null); setContent(null); setError(""); setLoading(false); setLoadingMore(false); setChunkOffsets([]);
    void api<LazyTracePage>(listUrl, { signal: controller.signal }).then(value => { if (alive) setPage(value); })
      .catch(reason => { if (alive) setError(String(reason)); });
    return () => { alive = false; epoch.current += 1; controller.abort(); };
  }, [listUrl]);

  async function choose(nodeId: string): Promise<void> {
    const token = ++epoch.current;
    setSelected(nodeId); setContent(null); setLoading(true); setError(""); setChunkOffsets([]);
    try {
      const value = await api<LazyTraceContent>(
        `/runs/${encodeURIComponent(runId)}/items/${itemIndex}/trace/${encodeURIComponent(nodeId)}?revision=${encodeURIComponent(revision)}`,
      );
      if (token === epoch.current) setContent(value);
    } catch (reason) {
      if (token === epoch.current) setError(String(reason));
    } finally {
      if (token === epoch.current) setLoading(false);
    }
  }

  async function loadNodePage(offset: number): Promise<void> {
    const token = ++epoch.current;
    setLoadingMore(true); setLoading(false); setSelected(null); setContent(null); setError("");
    try {
      const value = await api<LazyTracePage>(`${listUrl}&offset=${offset}`);
      if (token === epoch.current) { setPage(value); setChunkOffsets([]); }
    } catch (reason) {
      if (token === epoch.current) setError(String(reason));
    } finally {
      if (token === epoch.current) setLoadingMore(false);
    }
  }

  async function loadContentChunk(offset: number, previous = false): Promise<void> {
    if (!selected) return;
    const token = ++epoch.current;
    setLoading(true); setError("");
    try {
      const value = await api<LazyTraceContent>(
        `/runs/${encodeURIComponent(runId)}/items/${itemIndex}/trace/${encodeURIComponent(selected)}?offset=${offset}&revision=${encodeURIComponent(revision)}`,
      );
      if (token === epoch.current) {
        setChunkOffsets(values => previous ? values.slice(0, -1) : [...values, content?.offset ?? 0]);
        setContent(value);
      }
    } catch (reason) {
      if (token === epoch.current) setError(String(reason));
    } finally {
      if (token === epoch.current) setLoading(false);
    }
  }

  if (error) return <p className="trace-node-note">轨迹索引读取失败：{error}</p>;
  if (!page) return <p className="trace-node-note">正在读取节点摘要…</p>;
  return <div className="trace-node-workspace">
    <div className="trace-view-tabs" role="tablist" aria-label="轨迹类型">
      {[ ["calls", "调用流程"], ["event", "原始事件"], ["sources", "性能来源"], ["judges", "评分请求"] ].map(([value, label]) =>
        <button key={value} type="button" role="tab" aria-selected={kind === value}
          onClick={() => { setKind(value); setSelected(null); setContent(null); }}>
          {label}
        </button>)}
    </div>
    <nav aria-label="调用节点" className="trace-node-route"><ol>
      {page.nodes.map((node, index) => <li key={node.node_id}><button type="button" aria-pressed={selected === node.node_id}
        className={`trace-route-node trace-route-${node.kind}`} onClick={() => void choose(node.node_id)}>
        <span className="trace-node-number">{(page.offset ?? 0) + index + 1}</span><span>
          <strong>{String(node.metadata?.tool ?? node.metadata?.tool_name ?? node.kind)}</strong>
          <small>{node.node_id}{node.metadata?.step_id != null ? ` · ${String(node.metadata.step_id)}` : ""}</small>
          <small>{node.metadata?.status != null ? String(node.metadata.status) : "摘要"}</small>
        </span>
      </button></li>)}
      {(page.offset ?? 0) > 0 ? <li><button type="button" disabled={loadingMore} onClick={() => void loadNodePage(Math.max(0, (page.offset ?? 0) - 50))}>上一页节点</button></li> : null}
      {page.next_offset != null ? <li><button type="button" disabled={loadingMore} onClick={() => void loadNodePage(page.next_offset ?? 0)}>
        {loadingMore ? "正在读取…" : `加载更多节点（${(page.offset ?? 0) + page.nodes.length}/${page.total}）`}
      </button></li> : null}
    </ol></nav>
    <section className="trace-node-inspector" aria-label="节点详情" aria-live="polite">
      {!selected ? <p>点击节点后读取完整输入输出。</p> : loading ? <p>正在读取节点内容…</p> : content ? <>
        <header><h5>{selected}</h5><span>{content.source_path?.join(".")}</span></header>
        <RawTraceRecord defaultOpen label={`节点内容 · 字符 ${content.offset + 1} 起`} value={content.text} path={content.source_path?.join(".")} />
        <div className="trace-node-note">
          <button type="button" disabled={loading || content.offset === 0} onClick={() => void loadContentChunk(chunkOffsets.at(-1) ?? 0, true)}>上一块</button>
          <button type="button" disabled={loading || content.next_offset == null} onClick={() => void loadContentChunk(content.next_offset ?? 0)}>下一块</button>
          <span>{content.next_offset == null ? "已到末尾" : "长内容按块读取"}</span>
        </div>
      </> : <p>节点内容未记录。</p>}
    </section>
  </div>;
}

export function TraceNodeBrowser({ detail, turnIndex, modelLabel }: { detail: RunItemDetail; turnIndex: number; modelLabel?: string }) {
  const [view, setView] = useState("flow");
  const [selection, setSelection] = useState<string | null>(null);
  const id = useId();
  const turn = runtimeTurns(detail).find((value, index) => Number(value.index ?? index) === turnIndex);
  const nodes = turnTimeline(detail, turnIndex).filter(entry => entry.kind !== "event");
  const selected = nodes.find(entry => entry.key === selection) ?? nodes[0];
  const events = turnProgressEvents(turn);
  const eventRecords = events.length ? events : Array.isArray(turn?.debug_trace) ? turn.debug_trace : [];
  if ((detail.trace_storage as Record<string, unknown> | undefined)?.version === 3) {
    return <section className="trace-node-browser" aria-label="执行轨迹节点视图">
      <p className="trace-node-note">已按节点懒加载：先读取摘要，点击节点后读取完整内容。</p>
      <LazyTraceNodeBrowser detail={detail} turnIndex={turnIndex} />
    </section>;
  }
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
