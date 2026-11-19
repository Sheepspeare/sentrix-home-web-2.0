import { useEffect, useState } from "react";
import { api } from "../../api/client.ts";
import type { RunItemDetail } from "../../api/types.ts";
import { fmtMs } from "../../lib/format.ts";
import type { Dict } from "../../lib/agentTrace.ts";
import type { LazyTraceNode } from "../../lib/lazyTrace.ts";
import { RawTraceRecord } from "./TraceNodeDetails.tsx";

interface ContentChunk { text: string; offset: number; next_offset?: number | null; total_characters?: number }

/** One field and one text window stay mounted; switching nodes releases the previous body. */
function TraceField({ base, field, label, defaultOpen = false, messages = false, sourcePath }: {
  base: string; field: string; label: string; defaultOpen?: boolean; messages?: boolean; sourcePath?: string;
}) {
  const [open, setOpen] = useState(defaultOpen);
  const [offsets, setOffsets] = useState([0]);
  const [result, setResult] = useState<{ key: string; chunk?: ContentChunk; error?: string }>();
  const offset = offsets.at(-1) ?? 0;
  const query = sourcePath ? `path=${encodeURIComponent(sourcePath + (field ? `.${field}` : ""))}` : `field=${encodeURIComponent(field)}`;
  const key = `${base}:${query}:${offset}`;
  const chunk = result?.key === key ? result.chunk : undefined;
  const error = result?.key === key ? result.error : undefined;
  useEffect(() => {
    if (!open) return;
    const controller = new AbortController();
    void api<ContentChunk>(`${base}&${query}&offset=${offset}&limit=32768`, { signal: controller.signal })
      .then(value => { if (!controller.signal.aborted) setResult({ key, chunk: value }); })
      .catch(reason => { if (!controller.signal.aborted) setResult({ key, error: String(reason) }); });
    return () => controller.abort();
  }, [base, query, open, offset, key]);
  let value: unknown;
  if (chunk && chunk.offset === 0 && chunk.next_offset == null) {
    try { value = JSON.parse(chunk.text); } catch { value = chunk.text; }
  }
  return <details className="trace-raw-record trace-lazy-field" open={open} onToggle={event => {
    if (event.target === event.currentTarget) setOpen(event.currentTarget.open);
  }}>
    <summary>{label}</summary>
    {open ? error ? <p role="alert">内容读取失败：{error}</p> : !chunk ? <p className="trace-node-note">正在读取…</p> : <>
      {messages && Array.isArray(value) ? <div className="trace-saved-messages"><p>本次保存输入 · {value.length} 条消息</p>
        {value.map((message: Dict, index) => <RawTraceRecord key={index} label={`messages[${index}] · ${String(message?.role ?? "角色未记录")}`} value={message}/>)}</div> :
        <pre>{value !== undefined && typeof value !== "string" ? JSON.stringify(value, null, 2) : typeof value === "string" ? value : chunk.text}</pre>}
      {chunk.next_offset != null || offset > 0 ? <div className="trace-content-pagination">
        <button type="button" disabled={offset === 0} onClick={() => setOffsets(values => values.slice(0, -1))}>上一段</button>
        <span>第 {offsets.length} 段 · 每段最多 32K 字符</span>
        <button type="button" disabled={chunk.next_offset == null} onClick={() => setOffsets(values => [...values, chunk.next_offset!])}>下一段</button>
      </div> : null}
    </> : null}
  </details>;
}

export function LazyTraceNodeDetails({ detail, node, modelLabel }: { detail: RunItemDetail; node: LazyTraceNode; modelLabel?: string }) {
  const [tab, setTab] = useState("input");
  const metadata = node.metadata ?? {};
  const fields = node.fields ?? [];
  const revision = String((detail.trace_storage as Dict)?.revision ?? "");
  const sourcePath = node.source_path?.length && node.source_path[0] !== "scoring_results" ? node.source_path.join(".") : undefined;
  const route = sourcePath ? "content" : `trace/${encodeURIComponent(node.node_id)}`;
  const base = `/runs/${encodeURIComponent(String(detail.run_id))}/items/${detail.index}/${route}?revision=${encodeURIComponent(revision)}`;
  const field = (name: string, label: string, defaultOpen = false) => fields.includes(name) ?
    <TraceField key={name} base={base} sourcePath={sourcePath} field={name} label={label} defaultOpen={defaultOpen} messages={name === "messages"}/> : null;
  const model = ["model", "planner", "judge", "metrics"].includes(node.kind);
  const input = model ? ["messages", "tools", "prompt", "request", "request_body", "input"] : ["arguments", "args", "input"];
  const output = model ? ["response", "response_body", "output", "error"] : ["result", "observation", "text", "output", "error"];
  return <div className="trace-node-details">
    {model ? <p className="trace-node-note">{metadata.model ? `模型 ${String(metadata.model)}` : modelLabel ? `任务配置模型 ${modelLabel}` : ""}</p> : null}
    <div className="trace-detail-tabs" role="tablist" aria-label="节点记录">
      {[["input", "输入"], ["output", "输出"], ["metrics", "耗时与指标"], ["raw", "原始记录"]].map(([key, label]) =>
        <button key={key} type="button" role="tab" aria-selected={tab === key} onClick={() => setTab(key)}>{label}</button>)}
    </div>
    <div className="trace-detail-panel" key={tab}>
      {tab === "input" ? <>
        {node.message_roles ? <section className="trace-saved-messages"><p>本次保存输入 · {node.message_count} 条消息</p>
          {node.message_roles.map(message => <TraceField key={message.index} base={base} sourcePath={sourcePath} field={`messages.${message.index}`} label={`messages[${message.index}] · ${message.role}`}/>)}
          {Number(node.message_count) > node.message_roles.length ? field("messages", "其余消息 · 分段查看") : null}
        </section> : null}
        {input.filter(name => name !== "messages" || !node.message_roles).map(name => field(name, name === "messages" ? "本次保存输入 · messages" : name === "tools" ? "本次可见 tools" : `${model ? "模型输入" : "工具参数"} · ${name}`))}
        {!input.some(name => fields.includes(name)) ? <p className="trace-node-note">此节点未保存独立输入；可在原始记录中核对。</p> : null}</> : null}
      {tab === "output" ? <>{output.map(name => field(name, `已保存输出 · ${name}`, true))}
        {!output.some(name => fields.includes(name)) ? <p className="trace-node-note">此节点未保存独立输出。</p> : null}</> : null}
      {tab === "metrics" ? <><div className="trace-node-metrics">
        <span>请求耗时 {fmtMs(metadata.duration_ms ?? metadata.total_ms ?? (metadata.latency_s == null ? null : Number(metadata.latency_s) * 1000))}</span>
        {model ? <><span>TTFT {metadata.ttft_ms != null ? fmtMs(metadata.ttft_ms) : metadata.streamed === false ? "不适用（非流式）" : "未记录"}</span>
          <span>Token 输入 {String(metadata.prompt_tokens ?? "未记录")} / 输出 {String(metadata.completion_tokens ?? "未记录")}</span>
          {metadata.tokens_per_second != null ? <span>{String(metadata.tokens_per_second)} token/s</span> : null}</> : null}
        {metadata.attempt != null ? <span>attempt {String(metadata.attempt)}</span> : null}
        {metadata.parent_step_id != null ? <span>父步骤 {String(metadata.parent_step_id)}</span> : null}
      </div>{["retrieval_timing", "agent_loop_timing", "call_observation", "diagnostics"].map(name => field(name, name))}</> : null}
      {tab === "raw" ? <><code className="trace-source-path">{node.source_path?.join(".")}</code><TraceField base={base} sourcePath={sourcePath} field="" label="本节点完整记录" defaultOpen/></> : null}
    </div>
  </div>;
}
