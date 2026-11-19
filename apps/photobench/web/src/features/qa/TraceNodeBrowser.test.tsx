import { cleanup, fireEvent, render, screen, within, waitFor } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";
import type { RunItemDetail } from "../../api/types.ts";
import { TraceNodeBrowser } from "./TraceNodeBrowser.tsx";
import * as client from "../../api/client.ts";

afterEach(() => { cleanup(); vi.restoreAllMocks(); });
const context=[{role:"system",content:"系统原文",extension:{zero:0,no:false,nil:null,empty:""}},{role:"user",content:"问题"}];
const first={call_id:"c1",call_type:"agent",step_id:"step_1",seq:0,status:"complete",duration_ms:1200,streamed:false,model:"model-current",messages:context,tools:[{function:{name:"search"},future:null}],response:{content:"",thought:"原思考",tool_calls:[{arguments:"{broken"}],future:false}};
const second={...first,call_id:"c2",step_id:"step_2",seq:1,attempt:2,duration_ms:0,messages:[...structuredClone(context),{role:"tool",content:"新增观察"}],response:{content:"答案"}};
const item:RunItemDetail={index:0,runtime_turns:[{index:0,answer:"答案",trace_bundle:{debug_calls:[first,second]},trace:[{kind:"terminal",payload:{new_field:false}}]}],tool_trace:[]};

async function expand(label:string|RegExp){const summary=screen.getByText(label,{selector:"summary"});if(!summary.parentElement?.hasAttribute("open")) fireEvent.click(summary);await waitFor(()=>expect(screen.getByText(label,{selector:"summary"}).parentElement?.querySelector("pre")).toBeTruthy());}
function choose(name:RegExp){fireEvent.click(within(screen.getByRole("navigation",{name:"调用节点"})).getByRole("button",{name}));}

it("节点显示自身耗时；指标保留TTFT、模型、Token与拆分，切换不累计原文DOM", async()=>{
  const {container}=render(<TraceNodeBrowser detail={item} turnIndex={0}/>);
  expect(container.querySelector("pre")).toBeNull();
  expect(within(screen.getByRole("navigation")).getByRole("button",{name:/step_1.*1.2s/})).toBeTruthy();
  await expand("messages[0] · system");
  expect(JSON.parse(container.querySelector("pre")!.textContent!)).toEqual(context[0]);
  choose(/step_2/);
  expect(container.querySelector("pre")).toBeNull();
  fireEvent.click(screen.getByRole("tab",{name:"耗时与指标"}));
  expect(screen.getByText("请求耗时 0ms")).toBeTruthy();
  expect(screen.getByText("TTFT 不适用（非流式）")).toBeTruthy();
  expect(screen.getByText("模型 model-current")).toBeTruthy();
  expect(screen.getByText("Token 输入 未记录 / 输出 未记录")).toBeTruthy();
});
it("重复前缀只折叠本次原文，新增消息保留原索引，输出未知字段和无效参数串不变",async()=>{
  const {container}=render(<TraceNodeBrowser detail={item} turnIndex={0}/>);
  choose(/step_2/);
  expect(screen.getByText(/前 2 条与 step_1 完全一致 · 本次仍携带/)).toBeTruthy();
  expect(screen.getByText("messages[2] · tool")).toBeTruthy();
  await expand("messages[0] · system");
  expect(JSON.parse(container.querySelector("pre")!.textContent!)).toEqual(context[0]);
  choose(/step_1/);
  fireEvent.click(screen.getByRole("tab",{name:"输出"}));
  await expand("已保存 response · 全部字段");
  expect(JSON.parse(container.querySelector("pre")!.textContent!)).toEqual(first.response);
});
it("完整记录可还原全部字段和值，新增数据不抢走用户选择", async()=>{
  const {container,rerender}=render(<TraceNodeBrowser detail={item} turnIndex={0}/>);
  choose(/step_2/);
  rerender(<TraceNodeBrowser detail={{...item,tool_trace:[{tool:"new-tool",status:"running"}]}} turnIndex={0}/>);
  expect(within(screen.getByRole("navigation")).getByRole("button",{name:/step_2/}).getAttribute("aria-pressed")).toBe("true");
  fireEvent.click(screen.getByRole("tab",{name:"完整记录"}));
  await expand("本题完整已保存对象 · 全部字段");
  expect(JSON.parse(container.querySelector("pre")!.textContent!).runtime_turns).toEqual(item.runtime_turns);
});
it("每个用户轮次的相同步骤与重复前缀独立",()=>{
  const copy:RunItemDetail={...item,runtime_turns:[...item.runtime_turns!,{index:1,trace_bundle:{debug_calls:[{...second,messages:[{role:"user",content:"第二轮"}]}]}}]};
  const {container}=render(<TraceNodeBrowser detail={copy} turnIndex={1}/>);
  expect(container.textContent).not.toContain("完全一致");
  expect(within(screen.getByRole("navigation")).getAllByRole("button")).toHaveLength(2);
});


it("分块轨迹先取摘要，点击才取内容；按页获取更多节点", async () => {
  const request = vi.spyOn(client, "api").mockImplementation(async (path) => {
    const url = String(path);
    if (url.includes("/trace/t0_gcalls_i0")) return { text: "完整输入", offset: 0, next_offset: null };
    if (url.includes("offset=50")) return { nodes: [{ node_id: "t0_gcalls_i50", kind: "tool", turn_index: 0 }], total: 51, next_offset: null };
    return { nodes: [{ node_id: "t0_gcalls_i0", kind: "model", turn_index: 0, metadata: { step_id: "step-1" } }], total: 51, next_offset: 50 };
  });
  const detail = { index: 0, run_id: "split-run", trace_storage: { version: 3, revision: "abc" }, runtime_turns: [{ index: 0 }] } as RunItemDetail;
  render(<TraceNodeBrowser detail={detail} turnIndex={0} />);
  await screen.findByRole("button", { name: /step-1/ });
  expect(request).toHaveBeenCalledTimes(1);
  expect(screen.queryByText("完整输入")).toBeNull();
  fireEvent.click(screen.getByRole("button", { name: /step-1/ }));
  await waitFor(() => expect(request).toHaveBeenCalledTimes(2));
  fireEvent.click(screen.getByRole("button", { name: /加载更多节点/ }));
  await screen.findByRole("button", { name: /t0_gcalls_i50/ });
  expect(request).toHaveBeenCalledTimes(3);
});
