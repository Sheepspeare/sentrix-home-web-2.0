/**
 * 同名确认框：「已有一位『王王』，是同一个人吗？」
 *
 * 这是人物页唯一的合并入口（隐藏式合并）。用户只回答一个问题：
 * - **是同一个人** → 合并：保留谁按 `defaultMergeTarget`（已确认 > 出现多），照片、关系、
 *   别名都归到一起；
 * - **不是，只是同名** → 照常改名 / 确认，两人并存；
 * - **取消** → 什么都不做，名字框保留用户刚输入的内容。
 *
 * 真正的写操作由调用方给（改名与确认身份两条路不同），这里只管忙碌态与错误展示。
 */

import { useState } from "react";

import type { FamilyPerson } from "../../../shared/api/client";
import { Modal } from "../../../shared/ui/Modal";
import { describeReviewError } from "../reviewErrors";
import { isPendingPerson, personDisplayName } from "./membership";
import { PersonAvatar } from "./PersonAvatar";
import { namesakeMatchesAlias } from "./sameName";

interface Props {
  readonly person: FamilyPerson;
  readonly namesake: FamilyPerson;
  /** 用户刚输入的名字。 */
  readonly name: string;
  readonly onMerge: () => Promise<void>;
  readonly onKeepApart: () => Promise<void>;
  readonly onCancel: () => void;
}

export function SameNameDialog({ person, namesake, name, onMerge, onKeepApart, onCancel }: Props) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const clean = name.trim();
  const viaAlias = namesakeMatchesAlias(namesake, clean);

  async function run(action: () => Promise<void>) {
    setBusy(true);
    setError(null);
    try {
      await action();
    } catch (cause) {
      setError(describeReviewError(cause));
      setBusy(false);
    }
  }

  return (
    <Modal
      kicker="同名提醒"
      title={`已有一位「${clean}」，是同一个人吗？`}
      onClose={busy ? () => undefined : onCancel}
    >
      <p className="merge-dialog-lead">
        {viaAlias
          ? `「${clean}」是「${personDisplayName(namesake)}」的别名。`
          : `这个相册里已经有人叫「${clean}」。`}
        如果是同一个人，合并后照片、关系和称呼都会归到一起。
      </p>
      <ul className="merge-dialog-list same-name-pair">
        {[person, namesake].map((item) => (
          <li key={item.id} className="merge-dialog-option">
            <PersonAvatar person={item} size="sm" />
            <span className="merge-dialog-name">
              {item.id === person.id ? "当前这位" : personDisplayName(item)}
              <small>
                {isPendingPerson(item) ? "待确认 · " : ""}出现 {item.appearance_count} 次
              </small>
            </span>
          </li>
        ))}
      </ul>
      <p className="merge-dialog-warning">合并不会改动原始照片，但无法一键撤销。</p>
      {error === null ? null : (
        <p className="error" role="alert">
          {error}
        </p>
      )}
      <div className="merge-dialog-actions">
        <button type="button" className="button ghost" disabled={busy} onClick={onCancel}>
          取消
        </button>
        <button type="button" className="button ghost" disabled={busy} onClick={() => void run(onKeepApart)}>
          不是，只是同名
        </button>
        <button type="button" className="button primary" disabled={busy} onClick={() => void run(onMerge)}>
          是同一个人，合并
        </button>
      </div>
    </Modal>
  );
}
