import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";
import { CapacityReport } from "./CapacityReport.tsx";
import type { CapacityEvidence, CapacityRound } from "./CapacityReport.tsx";

afterEach(cleanup);

const round = (label: string, concurrency: number, passed: boolean): CapacityRound => ({
  label, run_id: label, concurrency, passed, questions: 64, completed: 64,
  judge_completed: 64, judge_failed: 0, status: "completed", qa_per_min: 64,
  qa_seconds: 60, judge_seconds: 80, latency_p50_s: 1, latency_p95_s: 2,
  ttft_p95_s: null, actual_batch_peak: 3, queue_peak: 1,
  memory_peak_gib: null, swap_delta_gib: 0, failures: {}, samples: [],
});

describe("capacity evidence", () => {
  it("requires both rounds and separates requested concurrency from actual model batch", () => {
    const evidence: CapacityEvidence = {
      schema_version: 1, updated_at: "2026-11-04", hardware: "M4", engine: "vllm-mlx",
      model: "Qwen", model_sha256: "model-hash", dataset_sha256: "dataset-hash",
      context_tokens: 32768, conditions: [],
      rounds: [round("C4-R1", 4, true), round("C4-R2", 4, true), round("C16-R1", 16, true)],
    };
    render(<CapacityReport evidence={evidence} />);
    expect(screen.getByText("C4")).toBeTruthy();
    expect(screen.getByText("C4 · 64.0 题/分钟")).toBeTruthy();
    expect(screen.queryByText("C16")).toBeNull();
    expect(screen.getAllByText("未采集").length).toBeGreaterThan(0);
    expect(screen.getByText("实际批次峰值")).toBeTruthy();
    expect(screen.getByText("尚未开始")).toBeTruthy();
  });
});

it("shows the independent 487 run's measurements separately from the 64-question matrix", () => {
  const full: CapacityRound = {
    ...round("C8-FULL487", 8, true), questions: 487, completed: 487, judge_completed: 487,
    qa_seconds: 600, judge_seconds: 700, qa_per_min: 48.7, actual_batch_peak: 6, queue_peak: 2,
  };
  const evidence: CapacityEvidence = {
    schema_version: 1, updated_at: "2026-11-04", hardware: "M4", engine: "vllm-mlx",
    model: "Qwen", model_sha256: "model-hash", dataset_sha256: "dataset-hash",
    context_tokens: 32768, conditions: [], rounds: [full],
  };
  render(<CapacityReport evidence={evidence} />);
  const overview = screen.getByRole("region", { name: "487 题独立测试总览" });
  expect(overview.textContent).toContain("487 题独立测试 · C8");
  expect(overview.textContent).toContain("48.7 题/分钟");
  expect(overview.textContent).toContain("6 / 2");
  expect(overview.textContent).toContain("客户端 QA 并发 8");
});

it("discloses scrape gaps separately from verified model execution", () => {
  const measured = {
    ...round("C4-R1", 4, true),
    resource_summary: { samples: 348, engine_metrics_missing_samples: 11 },
    model_call_evidence: { expected_calls: 315, recorded_calls: 315, verified: true },
  };
  render(<CapacityReport evidence={{
    schema_version: 1, updated_at: "2026-11-04", hardware: "M4", engine: "vllm-mlx",
    model: "Qwen", model_sha256: "model-hash", dataset_sha256: "dataset-hash",
    context_tokens: 32768, conditions: [], rounds: [measured],
  }} />);
  expect(screen.getByText(/模型指标采样 337\/348 次成功/)).toBeTruthy();
  expect(screen.getByText(/实际模型调用记录 315\/315 次/)).toBeTruthy();
  expect(screen.getByText(/指标缺失 11 次/)).toBeTruthy();
});

it("labels completed QA as failed acceptance when model evidence disagrees", () => {
  const failed = {
    ...round("C4-R2", 4, false),
    failures: { incomplete_model_call_evidence: 1 },
    model_call_evidence: { expected_calls: 290, recorded_calls: 290, verified: false },
  };
  render(<CapacityReport evidence={{
    schema_version: 1, updated_at: "2026-11-04", hardware: "M4", engine: "vllm-mlx",
    model: "Qwen", model_sha256: "model-hash", dataset_sha256: "dataset-hash",
    context_tokens: 32768, conditions: [], rounds: [round("C4-R1", 4, true), failed],
  }} />);
  expect(screen.getByRole("row", { name: /C4-R2/ }).textContent).toContain("未通过");
  expect(screen.queryByText("C4")).toBeNull();
  expect(screen.getByText(/尚未核对通过/)).toBeTruthy();
});
