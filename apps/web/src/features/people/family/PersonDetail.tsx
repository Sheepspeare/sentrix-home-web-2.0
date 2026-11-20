/**
 * 人物详情侧栏。2026-11-12 按「意思相近的放一起」重排为五段：
 *
 * 1. **身份**：姓名（待确认的人在这里确认身份）、别名；
 * 2. **关系**：家庭归属（他和这个家的关系）+ 与具体某人的关系；
 * 3. **介绍**：人物画像与「像他 / 不像他」反馈（原「人物介绍」tab 合并进来）；
 * 4. **照片**；
 * 5. **整理**：标为路人、这不是一个人物——低频且有破坏性，收在最底下。
 *
 * **没有任何合并按钮**：改名或确认身份时，名字撞上同一相册里另一个人的名字或别名，才弹出
 * 「是同一个人吗？」（`SameNameDialog`）。合并只发生在这一个入口。
 *
 * **写回执先落本地，重拉在后台对账**（2026-11-19）：改名的别名历史、成员身份的
 * `locked`/`revision`、关系的正反两行都是服务端算的，但它们**都在写回执里**，所以用回执
 * 更新本地（`onPersonPatched` / `onRelationsChanged`），不等一份完整图谱回来。这仍然不是
 * 乐观更新——落本地的是服务端已提交的值。记忆图与检索索引在后台同步，侧栏按
 * `sync_job_id` 显示进度。
 *
 * **只有身份段的写入锁整个侧栏**（改名 / 确认 / 合并 / 别名 / 移除）；关系与家庭归属按
 * 写入键各锁各的，保存一条关系时仍可以改名字。
 *
 * **确认身份只等身份本身**（2026-11-20）：外观取样（每个事件一次视觉模型调用）由后端在
 * 请求之外跑，确认请求只剩写身份、挂提及、刷事件参与者。等待期间按钮写「确认中…」，姓名
 * 与家庭角色一并锁住——此前框还能打字、按回车却静默无效，看起来像卡死。
 */

import { useEffect, useRef, useState, type FormEvent } from "react";

import {
  batchConfirmPeople,
  deliveredPhotoThumbUrl,
  getCorrectionJob,
  getPerson,
  getPersonNameSync,
  getPersonPortrait,
  mergePeople,
  rejectFaceCluster,
  renamePerson,
  retryCorrectionJob,
  retryMemoryCorrections,
  sendPortraitFeedback,
  setEntityProperty,
  setFamilyMembership,
  setPersonHidden,
  type BatchConfirmOutcome,
  type FamilyMembershipValue,
  type FamilyPerson,
  type FamilyRelationship,
  type MemorySyncResult,
  type PortraitRevision,
} from "../../../shared/api/client";
import { newOperationId, reportClientEvent } from "../../../shared/clientEvents";
import { describeReviewError } from "../reviewErrors";
import { editKeyHandler } from "./editKeys";
import {
  defaultMergeTarget,
  FAMILY_ROLES,
  isNamed,
  isPendingPerson,
  membershipLabel,
  membershipOf,
  personDisplayName,
  MEMBERSHIP_VALUES,
} from "./membership";
import { PersonAvatar } from "./PersonAvatar";
import { RelationEditor } from "./RelationEditor";
import { findNamesakes } from "./sameName";
import { usePeopleWrites } from "./usePeopleWrites";
import { SameNameDialog } from "./SameNameDialog";
import type { PersonPatch } from "./useFamilyGraph";

const LIKE = "像他";
const DISLIKE = "不像他";

interface Props {
  readonly person: FamilyPerson;
  readonly people: FamilyPerson[];
  readonly relationships: FamilyRelationship[];
  readonly onClose: () => void;
  readonly onChanged: () => Promise<void>;
  /** 写回执里已提交的人物字段先落本地（页面再在后台对账）。不给就等 `onChanged()` 重拉。 */
  readonly onPersonPatched?: (id: string, patch: PersonPatch) => void;
  /** 关系写回执先落本地。不给就等 `onChanged()` 重拉。 */
  readonly onRelationsChanged?: (scopeId: string, a: string, b: string, rows: readonly FamilyRelationship[]) => void;
  /** 图上拖拽落到人身上时预选的对方；普通点开是 null。 */
  readonly relateTarget?: string | null;
  readonly relateAttempt?: number;
  readonly mergeConfirmation?: MergeConfirmation | null;
  readonly onMergeConfirmation?: (confirmation: MergeConfirmation | null) => void;
  /** 当前这个人被并进了别人：详情要跟过去，而不是停在一个已不存在的人上。 */
  readonly onRedirect?: (personId: string) => void;
}

export interface MergeConfirmation {
  readonly personId: string;
  readonly name: string;
  readonly role: string | null;
}

/** 正在问「是同一个人吗？」的那一次改名 / 确认。 */
interface NamesakePrompt {
  readonly candidates: readonly FamilyPerson[];
  readonly role: string | null;
  readonly name: string;
  readonly mode: "rename" | "confirm";
}

/**
 * 写入之后怎么让图谱跟上：`await` 等重拉完（要跳到另一个人、或者没有本地落地的入口时）；
 * `background` 发出重拉不等；`none` 已经用回执落了本地（落本地时页面自己会在后台对账）。
 */
type RefreshMode = "await" | "background" | "none";

function syncOfConfirm(outcome: BatchConfirmOutcome): MemorySyncResult | undefined {
  return outcome.sync_job_id
    ? { sync_status: "pending", sync_error: "", sync_job_id: outcome.sync_job_id }
    : undefined;
}

export function PersonDetail({
  person,
  people,
  relationships,
  onClose,
  onChanged,
  onPersonPatched,
  onRelationsChanged,
  relateTarget = null,
  relateAttempt = 0,
  mergeConfirmation = null,
  onMergeConfirmation,
  onRedirect,
}: Props) {
  const pending = isPendingPerson(person);
  const [busy, setBusy] = useState(false);
  // 确认身份在路上：按钮写「确认中…」。不复用 `busy`——标为路人、驳回也会锁侧栏。
  const [confirming, setConfirming] = useState(false);
  const active = useRef(false);
  const [syncResult, setSyncResult] = useState<MemorySyncResult | null>(null);
  const [syncRetrying, setSyncRetrying] = useState(false);
  const { pending: pendingWrites, write } = usePeopleWrites();
  const feedbackKey = "feedback:" + person.id;
  const membershipKey = "membership:" + person.id;
  // 有本地落地的入口就不必等重拉；单独渲染（测试、别处复用）时退回到等重拉。
  const patched: RefreshMode = onPersonPatched === undefined ? "await" : "none";
  const [error, setError] = useState<string | null>(null);
  // 未命名的人不把占位文案「未确认姓名人物」预填进输入框——那句话不是名字。
  const savedName = isNamed(person) ? personDisplayName(person) : "";
  const [name, setName] = useState(savedName);
  const [familyRole, setFamilyRole] = useState("");
  const [rejecting, setRejecting] = useState(false);
  const [prompt, setPrompt] = useState<NamesakePrompt | null>(null);
  const [portrait, setPortrait] = useState<PortraitRevision | null | undefined>(undefined);
  const [aliasUpdate, setAliasUpdate] = useState<{ base: string[]; value: string[] } | null>(null);
  const aliases = aliasUpdate?.base === person.aliases ? aliasUpdate.value : person.aliases;
  const [aliasesOpen, setAliasesOpen] = useState(false);
  const [aliasText, setAliasText] = useState("");
  // 打开编辑时的原文：取消（按钮或 Esc）就回到它，不把半截输入留在框里。
  const aliasOriginal = useRef<string[]>([]);
  const aliasInput = useRef<HTMLInputElement>(null);

  useEffect(() => {
    let cancelled = false;
    setPortrait(undefined);
    void getPersonPortrait(person.id)
      .then((value) => {
        if (!cancelled) setPortrait(value);
      })
      .catch(() => {
        if (!cancelled) setPortrait(null);
      });
    return () => {
      cancelled = true;
    };
  }, [person.id]);

  useEffect(() => {
    if (aliasesOpen) aliasInput.current?.focus();
  }, [aliasesOpen]);

  const syncJob = syncResult?.sync_job_id;
  const syncStatus = syncResult?.sync_status;
  useEffect(() => {
    if (syncStatus !== undefined && (!syncJob || syncStatus !== "pending")) return;
    let alive = true;
    let timer: number | undefined;
    const check = async () => {
      try {
        // 有任务 id 就按 id 查（关系、归属、确认身份、改名都有）；刚打开侧栏时查这个人
        // 最近一次改名的同步，没同步完就接着显示。
        const job = syncJob ? await getCorrectionJob(syncJob) : await getPersonNameSync(person.id);
        if (!alive || job === null) return;
        if (syncJob || job.status !== "synced") {
          setSyncResult((current) => {
            if (current?.sync_job_id && current.sync_job_id !== syncJob) return current;
            return { sync_job_id: job.id, sync_status: job.status, sync_error: job.error };
          });
        }
        if (job.status !== "pending") return;
      } catch {
        if (!syncJob) return;
      }
      if (alive) timer = window.setTimeout(() => void check(), 1200);
    };
    void check();
    return () => { alive = false; window.clearTimeout(timer); };
  }, [syncJob, person.id, syncStatus]);

  /**
   * 详情的写入口。`exclusive`（默认）锁整个侧栏、同一时间只跑一件——身份段的写入用它；
   * 关系与家庭归属传 `false`，由调用方用写入键各锁各的。
   */
  async function run(
    action: () => Promise<unknown>,
    { refresh = "background", exclusive = true }: { refresh?: RefreshMode; exclusive?: boolean } = {},
  ): Promise<boolean> {
    if (exclusive) {
      if (active.current) return false;
      active.current = true;
      setBusy(true);
    }
    setError(null);
    setSyncResult(null);
    try {
      const result = await action();
      if (result && typeof result === "object" && "sync_status" in result
        && (result.sync_status === "synced" || result.sync_status === "pending")) {
        setSyncResult(result as MemorySyncResult);
      }
      if (refresh === "await") await onChanged();
      else if (refresh === "background") void onChanged();
      return true;
    } catch (cause) {
      setError(describeReviewError(cause));
      return false;
    } finally {
      if (exclusive) {
        active.current = false;
        setBusy(false);
      }
    }
  }

  async function syncMemories() {
    if (syncRetrying || active.current) return;
    setSyncRetrying(true);
    try {
      if (syncJob) {
        const job = await retryCorrectionJob(syncJob);
        setSyncResult({ sync_job_id: job.id, sync_status: job.status, sync_error: job.error });
        return;
      }
      const { error: syncError } = await retryMemoryCorrections(person.scope_id);
      setSyncResult({ sync_status: syncError ? "pending" : "synced", sync_error: syncError });
      if (!syncError) await onChanged();
    } catch (cause) {
      setSyncResult({ sync_job_id: syncJob, sync_status: "pending", sync_error: describeReviewError(cause) });
    } finally {
      setSyncRetrying(false);
    }
  }

  // ---- 身份：改名 / 确认，撞名时先问 -------------------------------------------------

  async function rename(next: string) {
    const result = await renamePerson(person.id, next);
    setName(result.entity.canonical_name);
    setAliasUpdate({ base: person.aliases, value: result.aliases });
    onPersonPatched?.(person.id, { display_name: result.entity.canonical_name, aliases: result.aliases });
    return result;
  }

  /**
   * 确认身份：**不是改名**。改名只动 `canonical_name`，实体仍是 pending——没有提及、
   * 没有事件参与者，问答与按人找照片都看不到他（2026-11-11 「王王」就是这么丢的）。
   * 走 `POST /people/batch-confirm` 的单件形式。
   */
  async function confirm(personId: string, next: string, role: string | null): Promise<BatchConfirmOutcome> {
    const result = await batchConfirmPeople([
      { person_id: personId, name: next, family_role: role, merge_same_name: false },
    ]);
    const failure = result.failed[0];
    if (failure !== undefined) throw new Error(failure.error);
    const outcome = result.confirmed[0];
    if (outcome === undefined) throw new Error("身份确认没有返回结果");
    return outcome;
  }

  function reportConfirm(result: "started" | "ok" | "failed") {
    // 原「待确认的人物」页的两条埋点：确认入口换了位置，漏斗口径不变。
    void reportClientEvent({
      eventName: result === "started" ? "people.confirm_submit" : "people.confirm_result",
      result,
      operationId: newOperationId(),
      scopeId: person.scope_id,
      entityId: person.id,
      errorCode: result === "failed" ? "unknown" : undefined,
    });
  }

  function submitName(event?: FormEvent) {
    event?.preventDefault();
    const next = name.trim();
    if (active.current || prompt !== null || mergeConfirmation !== null || next === "" || (!pending && next === savedName)) return;
    const candidates = findNamesakes(person, next, people);
    const role = familyRole || null;
    if (candidates.length > 0) {
      setPrompt({ candidates, name: next, mode: pending ? "confirm" : "rename", role });
      return;
    }
    if (pending) {
      reportConfirm("started");
      let confirmedId = person.id;
      setConfirming(true);
      void run(async () => {
        const outcome = await confirm(person.id, next, role);
        confirmedId = outcome.entity_id;
        setName(next);
        if (confirmedId === person.id) onPersonPatched?.(person.id, { display_name: next, status: "confirmed" });
        return syncOfConfirm(outcome);
      }, { refresh: "none" }).then(async (ok) => {
        setConfirming(false);
        reportConfirm(ok ? "ok" : "failed");
        if (!ok) return;
        // 并进了别人：先等图谱里有他，再跳过去；否则详情会因为找不到人短暂关掉。
        if (confirmedId !== person.id) {
          await onChanged();
          onRedirect?.(confirmedId);
        } else if (onPersonPatched === undefined) {
          await onChanged();
        }
      });
    } else {
      void run(() => rename(next), { refresh: patched });
    }
  }

  async function mergeWithNamesake(current: NamesakePrompt, candidate: FamilyPerson) {
    const keptId = defaultMergeTarget([person, candidate]);
    const sourceId = keptId === person.id ? candidate.id : person.id;
    active.current = true;
    setBusy(true);
    try {
      const { entity } = await mergePeople(keptId, [sourceId], current.name);
      setPrompt(null);
      if (entity.id === person.id) {
        setName(entity.canonical_name);
        setAliasUpdate({ base: person.aliases, value: entity.aliases });
      }
      let confirmedId = entity.id;
      const recovery = { personId: entity.id, name: current.name, role: current.role };
      if (current.mode === "confirm" && entity.status === "pending") {
        try {
          confirmedId = (await confirm(entity.id, current.name, current.role)).entity_id;
        } catch {
          onMergeConfirmation?.(recovery);
          await onChanged();
          return;
        }
      }
      await onChanged();
      if (confirmedId !== person.id) onRedirect?.(confirmedId);
    } finally {
      active.current = false;
      setBusy(false);
    }
  }

  async function keepApart(current: NamesakePrompt) {
    active.current = true;
    setBusy(true);
    setConfirming(current.mode === "confirm");
    try {
      if (current.mode === "confirm") {
        reportConfirm("started");
        try {
          await confirm(person.id, current.name, current.role);
          setName(current.name);
          reportConfirm("ok");
        } catch (cause) {
          reportConfirm("failed");
          throw cause;
        }
      } else {
        const result = await rename(current.name);
        setSyncResult(result);
      }
      setPrompt(null);
      await onChanged();
    } finally {
      active.current = false;
      setBusy(false);
      setConfirming(false);
    }
  }

  function retryConfirmation(recovery: MergeConfirmation) {
    void run(async () => {
      const confirmedId = (await confirm(recovery.personId, recovery.name, recovery.role)).entity_id;
      setName(recovery.name);
      onMergeConfirmation?.(null);
      if (confirmedId !== person.id) onRedirect?.(confirmedId);
    });
  }

  // ---- 别名 ------------------------------------------------------------------------

  function openAliases() {
    aliasOriginal.current = [...aliases];
    setAliasText(aliases.join("、"));
    setAliasesOpen(true);
  }

  function cancelAliases() {
    setAliasText(aliasOriginal.current.join("、"));
    setAliasesOpen(false);
  }

  function saveAliases(event?: FormEvent) {
    event?.preventDefault();
    if (busy) return;
    const original = new Set(aliasOriginal.current);
    const current = new Set(aliases);
    const next = [...new Set([
      ...splitAliases(aliasText).filter((alias) => current.has(alias) || !original.has(alias)),
      ...aliases.filter((alias) => !original.has(alias)),
    ])];
    void run(async () => {
      // `PATCH /people/{id}/name` 只收 name；别名是通用实体属性，走属性写入。
      const result = await setEntityProperty(person.id, "aliases", next);
      const saved = Array.isArray(result.value) ? result.value.map(String) : next;
      setAliasUpdate({ base: person.aliases, value: saved });
      onPersonPatched?.(person.id, { aliases: saved });
      setAliasesOpen(false);
      return result;
    }, { refresh: patched });
  }

  // ---- 介绍 / 整理 -------------------------------------------------------------------

  function sendFeedback(verdict: string) {
    void write(feedbackKey, async () => {
      // 画像反馈不改图谱里的任何字段，不必重拉。
      await run(async () => {
        await sendPortraitFeedback(person.id, verdict);
        setPortrait(await getPersonPortrait(person.id));
      }, { refresh: "none", exclusive: false });
    });
  }

  /**
   * 「这不是一个人物」：拒绝它的人脸分组。后端删的是候选实体，人脸实例与原始照片都留着。
   * 只对待确认的人开放——已确认的人后端会回 409，界面上也不给这个按钮。
   */
  async function rejectPerson() {
    const ok = await run(async () => {
      const detail = await getPerson(person.id);
      const cluster = detail.clusters.find((item) => item.status !== "rejected");
      if (cluster === undefined) throw new Error("这个人物没有可移除的人脸分组");
      return rejectFaceCluster(cluster.id);
    }, { refresh: "await" });
    setRejecting(false);
    if (ok) onClose();
  }

  function changeMembership(next: FamilyMembershipValue) {
    void write(membershipKey, async () => {
      await run(async () => {
        const result = await setFamilyMembership(person.id, person.scope_id, next);
        if (result.membership !== null) onPersonPatched?.(person.id, { membership: result.membership });
        return result;
      }, { refresh: onPersonPatched === undefined ? "background" : "none", exclusive: false });
    });
  }

  function toggleHidden() {
    const next = !person.hidden;
    void run(async () => {
      const result = await setPersonHidden(person.id, next);
      onPersonPatched?.(person.id, { hidden: next });
      return result;
    }, { refresh: onPersonPatched === undefined ? "background" : "none" });
  }

  const relationRun = (action: () => Promise<unknown>) =>
    run(action, { refresh: onRelationsChanged === undefined ? "background" : "none", exclusive: false });

  const currentMembership: FamilyMembershipValue = membershipOf(person);
  const portraitText = portrait === undefined ? null : (portrait?.portrait_text ?? "").trim();
  const currentFeedback = portrait?.feedback ?? null;

  return (
    <aside className="person-detail" role="complementary" aria-label={`${personDisplayName(person)} 的详情`}>
      <header className="detail-head">
        <PersonAvatar person={person} size="lg" />
        <div>
          <strong>{personDisplayName(person)}</strong>
          <small>
            {pending ? "待确认" : "已确认"} · 出现 {person.appearance_count} 次
            {person.hidden ? " · 已标为路人" : ""}
          </small>
        </div>
        <button type="button" className="detail-close" aria-label="关闭详情" onClick={onClose}>
          ×
        </button>
      </header>

      {error === null ? null : (
        <p className="error" role="alert">
          {error}
        </p>
      )}

      {syncResult === null ? null : (
        <div className={"memory-sync-feedback is-" + syncResult.sync_status} role="status">
          <span>{syncResult.sync_status === "synced"
            ? "修改已保存，关联记忆已同步。"
            : syncResult.sync_error || syncResult.sync_status === "failed"
              ? "修改已保存，同步失败。 " + syncResult.sync_error
              : "修改已保存，正在同步关联记忆。"}</span>
          {syncResult.sync_status !== "synced" && (!syncJob || syncResult.sync_error || syncResult.sync_status === "failed") ? (
            <button type="button" className="button ghost small" disabled={busy || syncRetrying}
              onClick={() => void syncMemories()}>重试同步</button>
          ) : null}
        </div>
      )}
      <section className="detail-section">
        <h3>身份</h3>
        {mergeConfirmation !== null && pending ? (
          <div role="status">
            <p>人物已合并，身份待确认。可以重试确认「{mergeConfirmation.name}」。</p>
            <button type="button" className="button primary small" disabled={busy}
              onClick={() => retryConfirmation(mergeConfirmation)}>重试确认身份</button>
          </div>
        ) : <form className="detail-form" onSubmit={submitName}>
          {pending ? (
            <p className="detail-empty">这是系统从照片里整理出的面孔，确认后才会进入问答和按人找照片。</p>
          ) : null}
          <label>
            姓名
            <input
              value={name}
              placeholder={pending ? "这个人是谁？" : undefined}
              disabled={busy}
              onChange={(event) => setName(event.target.value)}
              onKeyDown={editKeyHandler({ onSubmit: () => submitName(), onCancel: () => setName(savedName) })}
            />
          </label>
          {pending ? (
            <label>
              家庭角色（可选）
              <select
                aria-label="家庭角色"
                value={familyRole}
                disabled={busy}
                onChange={(event) => setFamilyRole(event.target.value)}
              >
                <option value="">暂不填写</option>
                {FAMILY_ROLES.map((role) => (
                  <option key={role} value={role}>
                    {role}
                  </option>
                ))}
              </select>
            </label>
          ) : null}
          <div className="detail-actions">
            {pending ? (
              <button type="submit" className="button primary small" disabled={busy || name.trim() === ""}>
                {confirming ? "确认中…" : "确认身份"}
              </button>
            ) : (
              <button
                type="submit"
                className="button ghost small"
                disabled={busy || name.trim() === "" || name.trim() === savedName}
              >
                保存姓名
              </button>
            )}
            {name === savedName ? null : (
              <button type="button" className="button ghost small" disabled={busy} onClick={() => setName(savedName)}>
                取消
              </button>
            )}
          </div>
          <small className="detail-hint">与已有的人同名时，会先问你是不是同一个人。</small>
        </form>}

        <div className="detail-field">
          别名
          {aliasesOpen ? (
            <form className="detail-form" onSubmit={saveAliases}>
              <input
                ref={aliasInput}
                aria-label="人物别名"
                aria-describedby="alias-keys-hint"
                value={aliasText}
                placeholder="例如：小明、阿明（用顿号或逗号分隔）"
                onChange={(event) => setAliasText(event.target.value)}
                onKeyDown={editKeyHandler({ onSubmit: () => saveAliases(), onCancel: cancelAliases })}
              />
              <small id="alias-keys-hint" className="detail-hint">
                Enter 保存 · Esc 取消
              </small>
              <div className="detail-actions">
                <button type="submit" className="button ghost small" disabled={busy}>
                  保存别名
                </button>
                <button type="button" className="button ghost small" disabled={busy} onClick={cancelAliases}>
                  取消
                </button>
              </div>
            </form>
          ) : (
            <div className="detail-alias-row">
              {aliases.length === 0 ? (
                <span className="detail-empty">还没有别名</span>
              ) : (
                aliases.map((alias) => (
                  <span key={alias} className="detail-alias-chip">
                    {alias}
                  </span>
                ))
              )}
              <button type="button" className="text-button" disabled={busy} onClick={openAliases}>
                编辑别名
              </button>
            </div>
          )}
        </div>
      </section>

      <RelationEditor
        key={relateAttempt}
        person={person}
        people={people}
        relationships={relationships}
        busy={busy}
        run={relationRun}
        onRelationsChanged={onRelationsChanged}
        relateTarget={relateTarget}
        header={
          <label className="detail-field">
            家庭归属
            <select
              value={currentMembership}
              disabled={busy || pendingWrites.has(membershipKey)}
              onChange={(event) => changeMembership(event.target.value as FamilyMembershipValue)}
            >
              {MEMBERSHIP_VALUES.map((value) => (
                <option key={value} value={value}>
                  {membershipLabel(value)}
                </option>
              ))}
            </select>
          </label>
        }
      />

      <section className="detail-section">
        <h3>介绍</h3>
        <p className="detail-portrait">
          {portraitText === null ? "正在读取…" : portraitText === "" ? "还没有人物画像。" : portraitText}
        </p>
        {portraitText === null || portraitText === "" ? null : (
          <div className="detail-actions" role="group" aria-label="这段画像像不像他">
            {[LIKE, DISLIKE].map((verdict) => (
              <button
                key={verdict}
                type="button"
                className={currentFeedback === verdict ? "button primary small" : "button ghost small"}
                aria-pressed={currentFeedback === verdict}
                disabled={busy || pendingWrites.has(feedbackKey)}
                onClick={() => sendFeedback(verdict)}
              >
                {verdict}
              </button>
            ))}
          </div>
        )}
      </section>

      <section className="detail-section">
        <h3>照片</h3>
        {person.representative_media.length === 0 ? (
          <p className="detail-empty">还没有代表照片。</p>
        ) : (
          <ul className="detail-media">
            {person.representative_media.map((media) => (
              <li key={media.id}>
                <img src={deliveredPhotoThumbUrl(media.id)} alt="人物照片" loading="lazy" />
              </li>
            ))}
          </ul>
        )}
      </section>

      {!pending ? (
        <section className="detail-section person-memory-sync" aria-label="关联记忆修正">
          <h3>关联记忆</h3>
          <p className="detail-empty">按当前人物信息同步关联记忆、图谱与检索索引。</p>
          <button type="button" className="button primary small" disabled={busy || syncRetrying}
            onClick={() => void syncMemories()}>
            {syncRetrying ? "正在同步关联记忆…" : "修正并同步关联记忆"}
          </button>
        </section>
      ) : null}

      {pending || person.hidden ? (
        <section className="detail-section detail-tidy">
          <h3>整理</h3>
          <div className="detail-actions">
            <button type="button" className="button ghost small" disabled={busy} onClick={toggleHidden}>
              {person.hidden ? "取消路人标记" : "标为路人"}
            </button>
            {pending && !rejecting ? (
              <button type="button" className="button ghost small" disabled={busy} onClick={() => setRejecting(true)}>
                这不是一个人物
              </button>
            ) : null}
          </div>
          {rejecting ? (
            <div className="detail-confirm-row" role="group" aria-label="确认移除">
              <span>移除后这些面孔不再作为人物出现，原始照片不受影响。</span>
              <button type="button" className="button ghost small" disabled={busy} onClick={() => void rejectPerson()}>
                确定移除
              </button>
              <button type="button" className="button ghost small" onClick={() => setRejecting(false)}>
                取消
              </button>
            </div>
          ) : null}
        </section>
      ) : null}

      {prompt === null ? null : (
        <SameNameDialog
          person={person}
          candidates={prompt.candidates}
          familyRole={prompt.role}
          name={prompt.name}
          onMerge={(candidate) => mergeWithNamesake(prompt, candidate)}
          onKeepApart={() => keepApart(prompt)}
          onCancel={() => setPrompt(null)}
        />
      )}
    </aside>
  );
}

/** 别名输入：顿号、中英文逗号都当分隔符，去空、去重（后端也会再归一一次）。 */
function splitAliases(raw: string): string[] {
  return [
    ...new Set(
      raw
        .replace(/[、，]/g, ",")
        .split(",")
        .map((item) => item.trim())
        .filter((item) => item !== ""),
    ),
  ];
}
