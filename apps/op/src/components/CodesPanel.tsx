import { formatServerTime } from "../opLogic";
import type { HeldCodes, JoinCodeRole, ReviewCodeRole } from "../types";
import { CopyField } from "./CopyField";

const JOIN_ROLES: { role: JoinCodeRole; label: string }[] = [
  { role: "student", label: "學生 join code" },
  { role: "wizard", label: "Wizard join code" },
  { role: "teacher", label: "老師 join code（即時）" },
];

const REVIEW_ROLES: { role: ReviewCodeRole; label: string }[] = [
  { role: "teacher", label: "老師 review code" },
  { role: "observer", label: "觀察員 review code" },
];

interface CodesPanelProps {
  sessionId: string;
  codes: HeldCodes | undefined;
  onForget: () => void;
}

export function CodesPanel({ sessionId, codes, onForget }: CodesPanelProps) {
  const join = JOIN_ROLES.filter(({ role }) => codes?.join?.[role]);
  const review = REVIEW_ROLES.filter(({ role }) => codes?.review?.[role]);
  const upload = codes?.upload;

  if (join.length === 0 && review.length === 0 && !upload) {
    return (
      <section className="panel codes">
        <h3>角色碼</h3>
        <p className="hint">
          這個分頁沒有 {sessionId} 的明文碼。伺服器只存雜湊值，join code 只在建立場次時顯示一次，
          之後無法再取得。review code 可在下方重新產生；補傳碼可在錄影狀態區發出。
        </p>
      </section>
    );
  }

  return (
    <section className="panel codes">
      <h3>角色碼</h3>
      <p className="warning">
        這些碼只存在這個分頁的記憶體，重新整理或關閉分頁就會消失，伺服器也無法再顯示。
        透過可信任的管道交給對應的人，不要貼到公開的地方。
      </p>
      {join.length > 0 && (
        <div className="code-group">
          <h4>Join code（一次性，換取即時連線）</h4>
          {join.map(({ role, label }) => (
            <CopyField key={role} label={label} value={codes!.join![role]!} />
          ))}
        </div>
      )}
      {review.length > 0 && (
        <div className="code-group">
          <h4>Review code（場次結束後審閱用）</h4>
          {review.map(({ role, label }) => (
            <CopyField key={role} label={label} value={codes!.review![role]!} />
          ))}
        </div>
      )}
      {upload && (
        <div className="code-group">
          <h4>補傳碼（一次性）</h4>
          <CopyField label="學生補傳碼" value={upload.code} />
          <p className="hint">
            碼的到期時間：{formatServerTime(upload.expiresAt)}。場次已結束時，補傳仍須在寬限期內完成，以右側的寬限剩餘為準。
          </p>
        </div>
      )}
      <button type="button" className="secondary" onClick={onForget}>
        已轉交，從這個分頁清除
      </button>
    </section>
  );
}
