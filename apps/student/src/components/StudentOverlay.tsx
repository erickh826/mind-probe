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
  const isStoppedOrEnded = recorder.status === "stopped" || socket.session.session_state === "ended";
  const hasPending = recorder.chunks.some((c) => c.status === "queued" || c.status === "uploading");
  const isSafeToClose = isStoppedOrEnded && !hasPending && recorder.failedCount === 0 && recorder.uploadedCount > 0;

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
          </strong>
          {isSafeToClose ? (
            <span style={{ color: "#42d392", fontWeight: 700, marginTop: "4px", display: "block" }}>
              ✅ 錄影已全數保存，可安全關閉 (Safe to close)
            </span>
          ) : isStoppedOrEnded && hasPending ? (
            <span style={{ color: "#f2c14e", fontWeight: 600, marginTop: "4px", display: "block" }}>
              ⏳ 正在上傳剩餘錄影，請勿關閉視窗...
            </span>
          ) : isStoppedOrEnded && recorder.failedCount > 0 ? (
            <span style={{ color: "#ff8a8a", fontWeight: 600, marginTop: "4px", display: "block" }}>
              ❌ 有片段上傳失敗，請點擊 Retry 重試
            </span>
          ) : null}
          {recorder.error ? <span className="panel-error">{recorder.error}</span> : null}
        </div>
        <div className="button-row">
          {recorder.status === "recording" ? (
            <button onClick={onStopRecording}>Stop REC</button>
          ) : allowStartRecording ? (
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
