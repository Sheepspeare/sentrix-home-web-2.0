/**
 * 「改成同一个名字就是在说他们是同一个人吗？」——隐藏式合并的判定。
 *
 * 2026-11-12 起人物页没有任何「合并」按钮：用户把一个人改名（或确认身份）成与同一相册里
 * 另一个人相同的名字或别名时，才弹出确认框问是否合并。这里只负责找出那位同名的人。
 */

import type { FamilyPerson } from "../../../shared/api/client";
import { isNamed, isPendingPerson, personDisplayName } from "./membership";

/** 比较名字时的归一：去首尾空白、忽略大小写（拉丁字母的「Tom」与「tom」视为同名）。 */
export function normalizeName(name: string): string {
  return name.trim().toLowerCase();
}

/**
 * 同一相册里名字或别名等于 `name` 的另一个人。多个时优先已确认的、再优先出现多的——
 * 那最可能是用户心里「已经有的那位」。没有则 `null`。
 */
export function findNamesake(
  person: FamilyPerson,
  name: string,
  people: readonly FamilyPerson[],
): FamilyPerson | null {
  const needle = normalizeName(name);
  if (needle === "") return null;
  const matches = people.filter((other) => {
    if (other.id === person.id || other.scope_id !== person.scope_id) return false;
    if (isNamed(other) && normalizeName(personDisplayName(other)) === needle) return true;
    return other.aliases.some((alias) => normalizeName(alias) === needle);
  });
  matches.sort((a, b) => {
    const confirmed = Number(!isPendingPerson(b)) - Number(!isPendingPerson(a));
    if (confirmed !== 0) return confirmed;
    return b.appearance_count - a.appearance_count;
  });
  return matches[0] ?? null;
}

/** 命中的是对方的本名还是别名：确认框里要说清楚「这是谁的什么」。 */
export function namesakeMatchesAlias(namesake: FamilyPerson, name: string): boolean {
  const needle = normalizeName(name);
  return !(isNamed(namesake) && normalizeName(personDisplayName(namesake)) === needle);
}
