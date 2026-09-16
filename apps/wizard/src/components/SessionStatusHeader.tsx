import type { ClockState } from "../useSessionSocket";
import type { ConnectionStatus, SessionView } from "../types";

// Session 狀態列（ADR-0001 §4 snapshot、ADR-0002 §5 連續 fallback）。
// Everything here comes from the authoritative `session_snapshot`; nothing is
// inferred locally except connection health.

interface Props {
  sessionId: string;
  caseId: string;
  status: ConnectionStatus;
  closedReason: string | null;
  session: SessionView;
  clock: ClockState;
  pendingCount: number;
}

const CONNECTION_LABEL: Record<ConnectionStatus, string> = {
  idle: "未連線",
  connecting: "連線中…",
  open: "已連線",
  degraded: "心跳逾時",
  reconnecting: "重新連線中…",
  closed: "連線已關閉",
};

const CONNECTION_COLOR: Record<ConnectionStatus, string> = {
  idle: "#9aa0ab",
  connecting: "#e0b341",
  open: "#4ec86f",
  degraded: "#e0b341",
  reconnecting: "#e0b341",
  closed: "#ff6b6b",
};

const SESSION_STATE_LABEL: Record<SessionView["session_state"], string> = {
  created: "已建立 created",
  starting: "準備開始 starting",
  active: "進行中 active",
  paused: "已暫停 paused",
  ended: "已結束 ended",
};

function Metric({ label, value }: { label: string; value: string }) {
  return (
    <div style={{ minWidth: 92 }}>
      <div style={{ fontSize: 11, color: "#9aa0ab" }}>{label}</div>
      <div style={{ fontSize: 14, fontWeight: 600 }}>{value}</div>
    </div>
  );
}

export default function SessionStatusHeader({
  sessionId,
  caseId,
  status,
  closedReason,
  session,
  clock,
  pendingCount,
}: Props) {
  return (
    <header
      style={{
        display: "flex",
        flexWrap: "wrap",
        gap: 20,
        alignItems: "center",
        padding: "10px 16px",
        background: "#1b1e24",
        borderBottom: "1px solid #2c313a",
      }}
    >
      <div>
        <div style={{ fontSize: 16, fontWeight: 700 }}>
          {sessionId}
          <span style={{ fontSize: 12, color: "#9aa0ab", marginLeft: 8 }}>
            {caseId || "—"}
          </span>
        </div>
        <div style={{ fontSize: 12, color: CONNECTION_COLOR[status] }}>
          ● {CONNECTION_LABEL[status]}
          {closedReason !== null && ` · ${closedReason}`}
        </div>
      </div>

      <Metric label="Session 狀態" value={SESSION_STATE_LABEL[session.session_state]} />
      <Metric
        label="播放"
        value={
          session.playback_state === "playing"
            ? `playing · ${session.active_clip ?? "—"}`
            : "idle"
        }
      />
      <Metric label="角色狀態" value={String(session.character_state)} />
      <Metric label="配合度" value={String(session.cooperation_level)} />
      <Metric
        label="連續 fallback"
        value={String(session.consecutive_fallbacks)}
      />
      <Metric label="snapshot rev" value={String(session.snapshot_revision)} />
      <Metric
        label="時鐘 offset"
        value={clock.offset_ms === null ? "同步中…" : `${clock.offset_ms}ms`}
      />
      <Metric label="未 ACK" value={String(pendingCount)} />

      <div style={{ flex: 1 }} />

      <div style={{ maxWidth: 260, fontSize: 11, color: "#9aa0ab" }}>
        <div>已揭露事實 revealed_facts</div>
        <div style={{ color: "#e8e8ea" }}>
          {session.revealed_facts.length === 0
            ? "—"
            : session.revealed_facts.join("、")}
        </div>
      </div>
    </header>
  );
}
