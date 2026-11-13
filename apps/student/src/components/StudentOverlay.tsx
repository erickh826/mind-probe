import type { StudentRecorder } from "../useMediaRecorder";
import type { StudentSocket } from "../useStudentSocket";
import { planStudentLeave } from "../leaveSession";
import { waitingForWizardCopy } from "../sessionGate";
import { permanentBlockMessage } from "../uploadQueue";

interface StudentOverlayProps {
  socket: StudentSocket;
  recorder: StudentRecorder;
  cameraVisible: boolean;
  onToggleCamera: () => void;
  onStartRecording: () => void;
  onStopRecording: () => void;
  onLeave: () => void;
  /** False until the server session is active, and after a reload. */
  allowStartRecording?: boolean;
}

function statusLabel(value: string): string {
  return value.replace(/_/g, " ");
}

/** What the student should know about the connection, if anything. */
function connectionNotice(socket: StudentSocket): string | null {
  if (socket.admitted) {
    return null;
  }
  switch (socket.notice) {
    case "occupied":
      return "此場次的學生端已有另一個裝置連線中，約 30 秒後自動再試。這段期間不能開始錄影。";
    case "unavailable":
      return "伺服器暫時無法確認場次狀態，稍後自動再試。";
    case "ended":
      return "場次已結束。";
    case "replaced":
      return "此裝置已被另一個連線取代，不會再自動連線。";
    default:
      return null;
  }
}

export function StudentOverlay({
  socket,
  recorder,
  cameraVisible,
  onToggleCamera,
  onStartRecording,
  onStopRecording,
  onLeave,
  allowStartRecording = false,
}: StudentOverlayProps) {
  const paused = socket.session.session_state === "paused";
  const banner = recorder.banner;
  const recording = recorder.status === "recording" || recorder.status === "stopping";
  const waiting = waitingForWizardCopy(socket.session.session_state, recording);
  const leave = planStudentLeave({
    banner,
    recorderStatus: recorder.status,
    pendingCount: recorder.pendingCount,
  });

  return (
    <>
      <header className="hud">
        <div className={`status-pill connection-${socket.status}`}>
          <span className="dot" />
          {statusLabel(socket.status)}
        </div>
        <div className={`status-pill rec-${recorder.status}`}>
          <span className="dot" />
          {recorder.status === "recording" ? "REC" : statusLabel(recorder.status)}
        </div>
        <div className="status-pill">
          offset {socket.clock.offset_ms ?? "-"}ms
          {socket.clock.rtt_ms !== null ? ` / rtt ${socket.clock.rtt_ms}ms` : ""}
        </div>
        <div className="status-pill">snapshot {socket.session.snapshot_revision}</div>
        <div className="status-pill">pending {socket.pendingCount}</div>
      </header>

      <aside className="side-panel">
        <div>
          <p className="eyebrow">Session</p>
          <strong>{socket.session.session_state}</strong>
          {socket.closedReason ? <span>{socket.closedReason}</span> : null}
          {connectionNotice(socket) ? <span>{connectionNotice(socket)}</span> : null}
        </div>
        <div>
          <p className="eyebrow">Playback</p>
          <strong>{socket.currentClipId ?? "idle"}</strong>
        </div>
        <div>
          <p className="eyebrow">Recording Queue</p>
          <strong>
            {recorder.uploadedCount} uploaded / {recorder.failedCount} failed
            {recorder.pendingCount > 0 ? ` / 尚有 ${recorder.pendingCount} 段未上傳` : ""}
          </strong>
          {banner.kind === "safe" ? (
            <span style={{ color: "#42d392", fontWeight: 700, marginTop: "4px", display: "block" }}>
              可以安全關閉
            </span>
          ) : null}
          {banner.kind === "confirming" ? (
            <span style={{ color: "#f2c14e", fontWeight: 600, marginTop: "4px", display: "block" }}>
              正在確認…請勿關閉
            </span>
          ) : null}
          {banner.kind === "uploading" ? (
            <span style={{ color: "#f2c14e", fontWeight: 600, marginTop: "4px", display: "block" }}>
              尚有 {banner.pending} 段未上傳
            </span>
          ) : null}
          {banner.kind === "interrupted" ? (
            <span style={{ color: "#ff8a8a", fontWeight: 700, marginTop: "4px", display: "block" }}>
              錄影已中斷，請通知老師或管理員
            </span>
          ) : null}
          {banner.kind === "incomplete" ? (
            <span style={{ color: "#ff8a8a", fontWeight: 700, marginTop: "4px", display: "block" }}>
              錄影不完整，已通知管理員
            </span>
          ) : null}
          {banner.kind === "needs_operator" ? (
            <span style={{ color: "#ff8a8a", fontWeight: 700, marginTop: "4px", display: "block" }}>
              {permanentBlockMessage(banner.block)}
            </span>
          ) : null}
          {recorder.storageWarning ? (
            <span style={{ color: "#f2c14e", display: "block", marginTop: "4px" }}>
              {recorder.storageWarning}
            </span>
          ) : null}
          {recorder.error ? <span className="panel-error">{recorder.error}</span> : null}
        </div>
        <div className="button-row">
          {recorder.status === "recording" ? (
            <button
              onClick={onStopRecording}
              title="Stop REC：停止錄影，留在此頁並繼續上傳。不會結束場次，也不會離開。"
            >
              Stop REC
            </button>
          ) : allowStartRecording && recorder.canStart ? (
            <button
              onClick={onStartRecording}
              title="開始錄影。只有伺服器已進入進行中才會出現。"
            >
              Start REC
            </button>
          ) : null}
          <button onClick={onLeave} title={leave.title}>
            {leave.buttonLabel}
          </button>
          <button onClick={recorder.retryFailed} disabled={recorder.failedCount === 0}>
            Retry
          </button>
          <button onClick={onToggleCamera}>{cameraVisible ? "Hide cam" : "Show cam"}</button>
        </div>
        <p className="leave-note">{leave.detail}</p>
        <p className="leave-note">
          Stop REC 停止錄影並留在此頁。結束場次由 Wizard 操作。離開場次回到加入頁，不刪除已保存的片段。
        </p>
      </aside>

      {cameraVisible && recorder.stream ? (
        <video
          className="camera-preview"
          muted
          playsInline
          autoPlay
          ref={(node) => {
            if (node && node.srcObject !== recorder.stream) {
              node.srcObject = recorder.stream;
            }
          }}
        />
      ) : null}

      {waiting ? (
        <div className="wait-banner" role="status">
          <p className="eyebrow">尚未錄影</p>
          <p>{waiting}</p>
        </div>
      ) : null}

      {paused ? (
        <div className="pause-overlay">
          <p className="eyebrow">Paused</p>
          <h2>{statusLabel(socket.session.pause_reason ?? "manual")}</h2>
        </div>
      ) : null}
    </>
  );
}
