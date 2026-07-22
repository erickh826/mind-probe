import { useEffect, useRef, useState } from "react";
import type { EventEnvelope } from "@mind-probe/shared";

// Student端（spec §3.2 A / §10）：全螢幕顯示虛擬疑犯 + getUserMedia/MediaRecorder。
// This is a scaffold — capture/upload/playback logic is sketched as TODOs.

export default function App() {
  const suspectVideoRef = useRef<HTMLVideoElement>(null);
  const [recording, setRecording] = useState(false);

  useEffect(() => {
    // TODO(§10.1): capture student camera + mic and start MediaRecorder.
    //   const stream = await navigator.mediaDevices.getUserMedia({ video: true, audio: true });
    //   const recorder = new MediaRecorder(stream); // browser default = WebM
    //   recorder.ondataavailable = (e) => queueChunkUpload(e.data); // 2s chunks
    //   recorder.start(2000);
    // Failed chunk uploads go to an IndexedDB queue for background retry (審閱後修訂 5).

    // TODO(§9): open WebSocket to /ws/{session_id}/student, run ping/pong clock sync,
    // and on `clip_command` swap the suspect <video> src (dual-player crossfade).
    // ws.onmessage = (ev) => handleEvent(JSON.parse(ev.data) as EventEnvelope);
    const _typeAnchor: EventEnvelope | null = null;
    void _typeAnchor;
  }, []);

  return (
    <div style={{ position: "fixed", inset: 0, background: "#000", color: "#fff" }}>
      {/* Fullscreen suspect video (§3.2 A). Dual <video> elements for crossfade (§7). */}
      <video
        ref={suspectVideoRef}
        style={{ width: "100%", height: "100%", objectFit: "contain" }}
        playsInline
      />

      {/* System + recording status overlay (§3.2 A). */}
      <div style={{ position: "absolute", top: 12, left: 12, fontSize: 14 }}>
        <span style={{ color: recording ? "#ff5555" : "#888" }}>
          ● {recording ? "REC" : "idle"}
        </span>
        {/* TODO: optional suspect subtitles toggle (§3.2 A). */}
      </div>

      {/* TODO: start / pause / resume / end session controls (§3.2 A). */}
      <button onClick={() => setRecording((r) => !r)} style={{ position: "absolute", bottom: 12, left: 12 }}>
        toggle rec (placeholder)
      </button>
    </div>
  );
}
