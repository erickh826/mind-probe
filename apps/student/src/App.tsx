import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import type { ClipManifestEntry } from "@mind-probe/shared";
import { fetchClipManifest, fetchSession, joinSession } from "./api";
import { mediaUrl } from "./config";
import { JoinSessionModal } from "./components/JoinSessionModal";
import { StudentOverlay } from "./components/StudentOverlay";
import { SuspectVideoPlayer } from "./components/SuspectVideoPlayer";
import { useMediaRecorder } from "./useMediaRecorder";
import { useStudentSocket } from "./useStudentSocket";
import type { JoinedSession, PlaybackClip } from "./types";
import "./App.css";

function findIdleClip(clips: ClipManifestEntry[]): ClipManifestEntry | null {
  return (
    clips.find((clip) => /idle|loop/i.test(`${clip.clip_id} ${clip.intent}`)) ?? null
  );
}

function toPlaybackClip(
  caseId: string,
  clip: ClipManifestEntry | null,
  loop: boolean,
): PlaybackClip | null {
  if (clip === null) {
    return null;
  }
  return {
    clipId: clip.clip_id,
    src: clip.file ? mediaUrl(caseId, clip.file) : null,
    label: clip.text || clip.clip_id,
    loop,
  };
}

export default function App() {
  const [joined, setJoined] = useState<JoinedSession | null>(null);
  const [joinPending, setJoinPending] = useState(false);
  const [joinError, setJoinError] = useState<string | null>(null);
  const [cameraVisible, setCameraVisible] = useState(true);
  const autoRecordingAttemptedRef = useRef(false);
  const previousRecorderStatusRef = useRef<string>("idle");

  const socket = useStudentSocket(joined?.sessionId ?? null);
  const recorder = useMediaRecorder({
    sessionId: joined?.sessionId ?? null,
    onChunkUploaded: socket.sendChunkUploaded,
  });

  const clipLookup = useMemo(() => {
    const map = new Map<string, ClipManifestEntry>();
    for (const clip of joined?.clips ?? []) {
      map.set(clip.clip_id, clip);
    }
    return map;
  }, [joined?.clips]);

  const idleClip = useMemo(() => {
    if (joined === null) {
      return null;
    }
    return toPlaybackClip(joined.caseId, findIdleClip(joined.clips), true);
  }, [joined]);

  const activeClip = useMemo(() => {
    if (joined === null || socket.currentClipId === null) {
      return null;
    }
    const clip = clipLookup.get(socket.currentClipId) ?? null;
    if (clip === null) {
      return {
        clipId: socket.currentClipId,
        src: null,
        label: socket.currentClipId,
        loop: false,
      };
    }
    return toPlaybackClip(joined.caseId, clip, false);
  }, [clipLookup, joined, socket.currentClipId]);

  const handleJoin = useCallback(async (sessionId: string, joinCode: string) => {
    setJoinPending(true);
    setJoinError(null);
    try {
      const join = await joinSession(sessionId, joinCode, "student-browser");
      if (join.role !== "student") {
        throw new Error(`Join code is for role "${join.role}", not student.`);
      }
      const session = await fetchSession(join.session_id);
      const clips = await fetchClipManifest(session.case_id);
      setJoined({
        sessionId: session.session_id,
        caseId: session.case_id,
        clips,
      });
    } catch (cause) {
      setJoinError(String(cause instanceof Error ? cause.message : cause));
    } finally {
      setJoinPending(false);
    }
  }, []);

  useEffect(() => {
    if (joined === null || socket.status !== "open" || autoRecordingAttemptedRef.current) {
      return;
    }
    autoRecordingAttemptedRef.current = true;
    void recorder.start();
  }, [joined, recorder.start, socket.status]);

  useEffect(() => {
    const previous = previousRecorderStatusRef.current;
    previousRecorderStatusRef.current = recorder.status;
    if (previous !== "recording" && recorder.status === "recording") {
      socket.sendRecordingStarted();
    }
    if (
      (previous === "recording" || previous === "stopping") &&
      ["stopped", "error", "unavailable"].includes(recorder.status)
    ) {
      socket.sendRecordingStopped(recorder.status);
    }
  }, [recorder.status, socket.sendRecordingStarted, socket.sendRecordingStopped]);

  if (joined === null) {
    return (
      <JoinSessionModal pending={joinPending} error={joinError} onJoin={handleJoin} />
    );
  }

  return (
    <div className="app-shell">
      <SuspectVideoPlayer
        activeClip={activeClip}
        idleClip={idleClip}
        paused={socket.session.session_state === "paused"}
        onClipStarted={socket.sendClipStarted}
        onClipEnded={socket.sendClipEnded}
      />
      <StudentOverlay
        socket={socket}
        recorder={recorder}
        cameraVisible={cameraVisible}
        onToggleCamera={() => setCameraVisible((visible) => !visible)}
        onStartRecording={() => {
          void recorder.start();
        }}
        onStopRecording={recorder.stop}
      />
    </div>
  );
}
