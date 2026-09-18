import type { StudentRecorder } from "../useMediaRecorder";
import type { StudentSocket } from "../useStudentSocket";

interface StudentOverlayProps {
  socket: StudentSocket;
  recorder: StudentRecorder;
  cameraVisible: boolean;
  onToggleCamera: () => void;
  onStartRecording: () => void;
  onStopRecording: () => void;
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
}: StudentOverlayProps) {
  const paused = socket.session.session_state === "paused";

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
          {recorder.error ? <span className="panel-error">{recorder.error}</span> : null}
        </div>
        <div className="button-row">
          {recorder.status === "recording" ? (
            <button onClick={onStopRecording}>Stop REC</button>
          ) : (
            <button onClick={onStartRecording}>Start REC</button>
          )}
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
