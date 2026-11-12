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
 * **写操作全部走 client，成功后调 `onChanged()` 重拉**——不做本地乐观更新：改名的
 * 别名历史、成员身份的 `locked`/`revision`、关系的正反两行都是服务端算的。
 */

import { useEffect, useRef, useState, type FormEvent } from "react";

import {
  batchConfirmPeople,
  deliveredPhotoThumbUrl,
  getPerson,
  getPersonPortrait,
  mergePeople,
  rejectFaceCluster,
  renamePerson,
  sendPortraitFeedback,
  setEntityProperty,
  setFamilyMembership,
  setPersonHidden,
  type FamilyMembershipValue,
  type FamilyPerson,
  type FamilyRelationship,
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
import { findNamesake } from "./sameName";
import { SameNameDialog } from "./SameNameDialog";

const LIKE = "像他";
const DISLIKE = "不像他";

interface Props {
  readonly person: FamilyPerson;
  readonly people: FamilyPerson[];
  readonly relationships: FamilyRelationship[];
  readonly onClose: () => void;
  readonly onChanged: () => Promise<void>;
  /** 图上拖拽落到人身上时预选的对方；普通点开是 null。 */
  readonly relateTarget?: string | null;
  /** 当前这个人被并进了别人：详情要跟过去，而不是停在一个已不存在的人上。 */
  readonly onRedirect?: (personId: string) => void;
}

/** 正在问「是同一个人吗？」的那一次改名 / 确认。 */
interface NamesakePrompt {
  readonly namesake: FamilyPerson;
  readonly name: string;
  readonly mode: "rename" | "confirm";
}

export function PersonDetail({
  person,
  people,
  relationships,
  onClose,
  onChanged,
  relateTarget = null,
  onRedirect,
}: Props) {
  const pending = isPendingPerson(person);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  // 未命名的人不把占位文案「未确认姓名人物」预填进输入框——那句话不是名字。
  const savedName = isNamed(person) ? personDisplayName(person) : "";
  const [name, setName] = useState(savedName);
  const [familyRole, setFamilyRole] = useState("");
  const [rejecting, setRejecting] = useState(false);
  const [prompt, setPrompt] = useState<NamesakePrompt | null>(null);
  const [portrait, setPortrait] = useState<PortraitRevision | null | undefined>(undefined);
  const [aliases, setAliases] = useState<string[]>(person.aliases);
  const [aliasesOpen, setAliasesOpen] = useState(false);
  const [aliasText, setAliasText] = useState("");
  // 打开编辑时的原文：取消（按钮或 Esc）就回到它，不把半截输入留在框里。
  const aliasOriginal = useRef("");
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

  async function run(action: () => Promise<unknown>): Promise<boolean> {
    setBusy(true);
    setError(null);
    try {
      await action();
      await onChanged();
      return true;
    } catch (cause) {
      setError(describeReviewError(cause));
      return false;
    } finally {
      setBusy(false);
    }
  }

  // ---- 身份：改名 / 确认，撞名时先问 -------------------------------------------------

  async function rename(next: string) {
    const result = await renamePerson(person.id, next);
    setAliases(result.aliases);
  }

  /**
   * 确认身份：**不是改名**。改名只动 `canonical_name`，实体仍是 pending——没有提及、
   * 没有事件参与者，问答与按人找照片都看不到他（2026-11-11 「王王」就是这么丢的）。
   * 走 `POST /people/batch-confirm` 的单件形式。
   */
  async function confirm(next: string, mergeSameName: boolean): Promise<string | null> {
    const result = await batchConfirmPeople([
      {
        person_id: person.id,
        name: next,
        family_role: familyRole === "" ? null : familyRole,
        merge_same_name: mergeSameName,
      },
    ]);
    const failure = result.failed[0];
    if (failure !== undefined) throw new Error(failure.error);
    const outcome = result.confirmed[0];
    return outcome !== undefined && outcome.entity_id !== person.id ? outcome.entity_id : null;
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
    if (busy || next === "" || (!pending && next === savedName)) return;
    const namesake = findNamesake(person, next, people);
    if (namesake !== null) {
      setPrompt({ namesake, name: next, mode: pending ? "confirm" : "rename" });
      return;
    }
    if (pending) {
      reportConfirm("started");
      let mergedInto: string | null = null;
      void run(async () => {
        mergedInto = await confirm(next, true);
      }).then((ok) => {
        reportConfirm(ok ? "ok" : "failed");
        if (ok && mergedInto !== null) onRedirect?.(mergedInto);
      });
    } else {
      void run(() => rename(next));
    }
  }

  /**
   * 「是同一个人」：保留谁按 `defaultMergeTarget`（有名 > 已确认 > 出现多）。保留下来的
   * 人若仍待确认（两个都是未确认的面孔），合并后再按这个名字确认一次。
   */
  async function mergeWithNamesake(current: NamesakePrompt) {
    const keptId = defaultMergeTarget([person, current.namesake]);
    const sourceId = keptId === person.id ? current.namesake.id : person.id;
    const kept = keptId === person.id ? person : current.namesake;
    await mergePeople(keptId, [sourceId], current.name);
    if (current.mode === "confirm" && isPendingPerson(kept)) {
      const result = await batchConfirmPeople([
        { person_id: keptId, name: current.name, family_role: familyRole === "" ? null : familyRole },
      ]);
      const failure = result.failed[0];
      if (failure !== undefined) throw new Error(failure.error);
    }
    setPrompt(null);
    await onChanged();
    if (keptId !== person.id) onRedirect?.(keptId);
  }

  /** 「不是，只是同名」：照常改名 / 确认，并告诉后端不要做同名并入。 */
  async function keepApart(current: NamesakePrompt) {
    if (current.mode === "confirm") {
      reportConfirm("started");
      await confirm(current.name, false);
      reportConfirm("ok");
    } else {
      await rename(current.name);
    }
    setPrompt(null);
    await onChanged();
  }

  // ---- 别名 ------------------------------------------------------------------------

  function openAliases() {
    aliasOriginal.current = aliases.join("、");
    setAliasText(aliasOriginal.current);
    setAliasesOpen(true);
  }

  function cancelAliases() {
    setAliasText(aliasOriginal.current);
    setAliasesOpen(false);
  }

  function saveAliases(event?: FormEvent) {
    event?.preventDefault();
    if (busy) return;
    const next = splitAliases(aliasText);
    void run(async () => {
      // `PATCH /people/{id}/name` 只收 name；别名是通用实体属性，走属性写入。
      await setEntityProperty(person.id, "aliases", next);
      setAliases(next);
      setAliasesOpen(false);
    });
  }

  // ---- 介绍 / 整理 -------------------------------------------------------------------

  function sendFeedback(verdict: string) {
    void run(async () => {
      await sendPortraitFeedback(person.id, verdict);
      // 反馈写在服务端那一版上，重新取一遍才看到它的 feedback 字段。
      setPortrait(await getPersonPortrait(person.id));
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
      await rejectFaceCluster(cluster.id);
    });
    setRejecting(false);
    if (ok) onClose();
  }

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

      <section className="detail-section">
        <h3>身份</h3>
        <form className="detail-form" onSubmit={submitName}>
          {pending ? (
            <p className="detail-empty">这是系统从照片里整理出的面孔，确认后才会进入问答和按人找照片。</p>
          ) : null}
          <label>
            姓名
            <input
              value={name}
              placeholder={pending ? "这个人是谁？" : undefined}
              onChange={(event) => setName(event.target.value)}
              onKeyDown={editKeyHandler({ onSubmit: () => submitName(), onCancel: () => setName(savedName) })}
            />
          </label>
          {pending ? (
            <label>
              家庭角色（可选）
              <select aria-label="家庭角色" value={familyRole} onChange={(event) => setFamilyRole(event.target.value)}>
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
                确认身份
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
              <button type="button" className="button ghost small" onClick={() => setName(savedName)}>
                取消
              </button>
            )}
          </div>
          <small className="detail-hint">与已有的人同名时，会先问你是不是同一个人。</small>
        </form>

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
        person={person}
        people={people}
        relationships={relationships}
        busy={busy}
        run={run}
        relateTarget={relateTarget}
        header={
          <label className="detail-field">
            家庭归属
            <select
              value={currentMembership}
              disabled={busy}
              onChange={(event) => {
                const next = event.target.value as FamilyMembershipValue;
                void run(() => setFamilyMembership(person.id, person.scope_id, next));
              }}
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
                disabled={busy}
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

      {pending || person.hidden ? (
        <section className="detail-section detail-tidy">
          <h3>整理</h3>
          <div className="detail-actions">
            <button type="button" className="button ghost small" disabled={busy} onClick={() => void run(() => setPersonHidden(person.id, !person.hidden))}>
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
          namesake={prompt.namesake}
          name={prompt.name}
          onMerge={() => mergeWithNamesake(prompt)}
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
