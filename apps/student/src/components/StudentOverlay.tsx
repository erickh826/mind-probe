import type { StudentRecorder } from "../useMediaRecorder";
import type { StudentSocket } from "../useStudentSocket";

interface StudentOverlayProps {
  socket: StudentSocket;
  recorder: StudentRecorder;
  cameraVisible: boolean;
  onToggleCamera: () => void;
  onStartRecording: () => void;
  onStopRecording: () => void;
  /** False after a reload: the student re-uploads, and does not start a new take. */
  allowStartRecording?: boolean;
}

function statusLabel(value: string): string {
  return value.replace(/_/g, " ");
}

export function StudentOverlay({
  socket,
  recorder,
  cameraVisible,
  onToggleCamera,
  onStartRecording,
  onStopRecording,
  allowStartRecording = true,
}: StudentOverlayProps) {
  const paused = socket.session.session_state === "paused";
  const banner = recorder.banner;

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
          {recorder.storageWarning ? (
            <span style={{ color: "#f2c14e", display: "block", marginTop: "4px" }}>
              {recorder.storageWarning}
            </span>
          ) : null}
          {recorder.error ? <span className="panel-error">{recorder.error}</span> : null}
        </div>
        <div className="button-row">
          {recorder.status === "recording" ? (
            <button onClick={onStopRecording}>Stop REC</button>
          ) : allowStartRecording && recorder.canStart ? (
            <button onClick={onStartRecording}>Start REC</button>
          ) : null}
          <button onClick={recorder.retryFailed} disabled={recorder.failedCount === 0}>
            Retry
          </button>
          <button onClick={onToggleCamera}>{cameraVisible ? "Hide cam" : "Show cam"}</button>
        </div>
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

      {paused ? (
        <div className="pause-overlay">
          <p className="eyebrow">Paused</p>
          <h2>{statusLabel(socket.session.pause_reason ?? "manual")}</h2>
        </div>
      ) : null}
    </>
  );
}
