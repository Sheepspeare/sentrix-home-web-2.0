import type { Run } from "../api/types.ts";

const count = (value: unknown): number | null =>
  typeof value === "number" && Number.isSafeInteger(value) && value >= 0 ? value : null;
const ratio = (value: number | null, total: number | null) => value === null && total === null ? "—" : `${value ?? "—"}/${total ?? "—"}`;

/** 列表与详情共用：答题已执行数、有效质量评分数、评分尝试结束数分别展示。 */
export function qaProgress(run: Run) {
  const phase = run.phases?.qa_eval;
  const progress = phase?.progress;
  const total = run.qa_count === null ? null : count(run.qa_count) ?? count(run.summary?.total) ??
    count(progress?.total) ?? count(progress?.judge_total) ?? count(phase?.judge_total);
  const agent = count(progress?.agent_completed) ?? count(phase?.agent_completed) ?? count(progress?.completed);
  const valid = count(run.summary?.judge_valid_count);
  const attempted = count(progress?.judge_completed) ?? count(phase?.judge_completed);
  const judgeEnabled = (progress?.judge_concurrency ?? phase?.judge_concurrency) !== 0;
  return {
    applicable: run.mode !== "build", judgeEnabled, agent, valid, attempted,
    agentRatio: ratio(agent, total), validRatio: ratio(valid, total),
    attemptedRatio: ratio(attempted, count(progress?.judge_total) ?? count(phase?.judge_total) ?? total),
    skipped: count(progress?.judge_skipped),
  };
}
