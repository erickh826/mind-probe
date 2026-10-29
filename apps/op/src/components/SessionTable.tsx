import {
  attentionFlags,
  formatSeconds,
  formatServerTime,
  graceRemainingSeconds,
  labelFor,
  RECORDING_LABELS,
  STATUS_LABELS,
} from "../opLogic";
import type { OperatorSessionRow } from "../types";

interface SessionTableProps {
  rows: OperatorSessionRow[];
  nowMs: number;
  selectedId: string | null;
  heldCodeIds: Set<string>;
  onSelect: (sessionId: string) => void;
}

export function SessionTable({ rows, nowMs, selectedId, heldCodeIds, onSelect }: SessionTableProps) {
  if (rows.length === 0) {
    return <p className="hint">還沒有場次。</p>;
  }
  return (
    <table className="sessions">
      <thead>
        <tr>
          <th>場次 ID</th>
          <th>案件</th>
          <th>狀態</th>
          <th>建立時間</th>
          <th>錄影</th>
          <th>片段數</th>
          <th>距上一片段</th>
          <th>補傳寬限剩餘</th>
          <th>注意</th>
        </tr>
      </thead>
      <tbody>
        {rows.map((row) => {
          const flags = attentionFlags(row);
          const grace = graceRemainingSeconds(row.upload_grace_ends_at, nowMs);
          return (
            <tr
              key={row.session_id}
              className={row.session_id === selectedId ? "selected" : undefined}
              onClick={() => onSelect(row.session_id)}
            >
              <td>
                <button type="button" className="link" onClick={() => onSelect(row.session_id)}>
                  {row.session_id}
                </button>
                {heldCodeIds.has(row.session_id) && <span className="badge">有碼</span>}
              </td>
              <td>{row.case_id}</td>
              <td>{labelFor(STATUS_LABELS, row.status)}</td>
              <td>{formatServerTime(row.created_at)}</td>
              <td>{labelFor(RECORDING_LABELS, row.recording_state)}</td>
              <td>{row.stored_sequences}</td>
              <td>{formatSeconds(row.seconds_since_last_chunk)}</td>
              <td>{grace === null ? "—" : formatSeconds(grace)}</td>
              <td>
                {flags.map((flag) => (
                  <span key={flag} className="flag">
                    {flag}
                  </span>
                ))}
              </td>
            </tr>
          );
        })}
      </tbody>
    </table>
  );
}
