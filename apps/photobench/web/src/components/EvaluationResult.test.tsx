import { cleanup, render, screen, within } from "@testing-library/react";
import { afterEach, expect, it } from "vitest";
import { EvaluationResult, EvaluationSummary, EvaluationTypeBadge } from "./EvaluationResult.tsx";
import { scoreChoices } from "../features/analysis/presentation.ts";
afterEach(cleanup);
it("二分制满分与筛选类别一致", () => {
  expect(scoreChoices({evaluation_summary: {count: 1, scales: ["1"], by_type: {choice: {}}}}).map(s => s.value)).toEqual(["2", "0", "None"]);
  render(<EvaluationResult value={{scorer: "choice_judge", score: 1, max_score: 1, normalized_score: 1, matched_option: "B", comparisons: [{option_id: "B", matches: true, reason: "语义一致"}]}} definition={{correct_option: "B", options: [{id: "B", text: "无法确认"}]}} />);
  expect(screen.getByText(/主分 1\/1 · 归一化 100.0%/)).toBeTruthy();
  expect(screen.getByText(/匹配：B · 正确选项：B/)).toBeTruthy();
  expect(screen.getByText(/无法确认 · 匹配 · 语义一致/)).toBeTruthy();
});
it("集合保留连续分，未知工具轨迹保留未评分", () => {
  expect(scoreChoices({evaluation_summary: {count: 1, scales: ["1"], by_type: {media_list: {}}}}).find(s => s.value === "1")?.label).toContain("集合未满分");
  render(<EvaluationResult value={{scorer: "media_set", score: 0.25, max_score: 1, normalized_score: 0.25, media_metrics: {delivery: {recall: 0.5, jaccard: 0.25, precision: 0.333, f1: 0.4}, tools: {recall: null, jaccard: null, precision: null, f1: null}}}} />);
  expect(screen.getByText(/主分 0.25\/1 · 归一化 25.0%/)).toBeTruthy();
  expect(within(screen.getByText("工具累计召回").closest("tr")!).getAllByText("未评分")).toHaveLength(4);
});
it("待复核与评分失败可区分，汇总披露覆盖", () => {
  const {rerender} = render(<EvaluationResult value={{scorer: "choice_judge", score: null, judge_status: "needs_review"}} />);
  expect(screen.getByText(/题目待复核/)).toBeTruthy();
  rerender(<EvaluationResult value={{scorer: "choice_judge", score: null, judge_status: "failed"}} />);
  expect(screen.getByText(/评分失败/)).toBeTruthy();
  rerender(<EvaluationSummary value={{count: 4, scored: 3, coverage: 0.75, mean: 0.5, by_type: {choice: {count: 4, scored: 3, mean: 0.5}}}} />);
  expect(screen.getByText(/覆盖 3\/4（75.0%）/)).toBeTruthy();
});

it("ATM 数值、列表和开放题独立标注，列表说明交付集合", () => {
  const {rerender} = render(<EvaluationTypeBadge value={{source_qtype: "list_recall", answer_format: "media_list"}} />);
  expect(screen.getByText("题型 · ATM 列表召回")).toBeTruthy();
  rerender(<EvaluationTypeBadge value={{source_qtype: "number", answer_format: "open"}} />);
  expect(screen.getByText("题型 · ATM 数值")).toBeTruthy();
  rerender(<EvaluationResult value={{scorer: "atm_open", source_qtype: "open_end", answer_format: "open"}} />);
  expect(screen.getByText("ATM 开放问答")).toBeTruthy();
  expect(screen.getByText(/Judge 按回答内容判/)).toBeTruthy();
  rerender(<EvaluationResult value={{scorer: "atm_list", source_qtype: "list_recall", answer_format: "media_list"}} />);
  expect(screen.getByText(/按实际交付的图片 \/ 视频 ID 集合/)).toBeTruthy();
});
