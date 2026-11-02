import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import type { ClipManifestEntry } from "@mind-probe/shared";
import {
  clearJoinedSession,
  readJoinedSession,
  rememberJoinedSession,
} from "@mind-probe/shared";
import { fetchClipManifest, fetchSession, fetchUploadStatus, joinSession, uploadLogin } from "./api";
import { restoreStudentSession, UNAVAILABLE_RESTORE_MESSAGE } from "./restoreSession";
import { mediaUrl } from "./config";
import { JoinSessionModal } from "./components/JoinSessionModal";
import { UploadCodeForm } from "./components/UploadCodeForm";
import {
  interruptRecording,
  isRevokedCloseReason,
  resumeUnauthorizedUploads,
} from "./recordingController";
import { findUnfinishedUploadSessions, type UploadRecovery } from "./uploadRecovery";
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
  const [restorePending, setRestorePending] = useState(
    () => readJoinedSession()?.role === "student",
  );
  const [restoreError, setRestoreError] = useState<string | null>(null);
  const [recoveries, setRecoveries] = useState<UploadRecovery[]>([]);
  const [recoveryChecked, setRecoveryChecked] = useState(false);
  const [preferJoin, setPreferJoin] = useState(false);
  const [cameraVisible, setCameraVisible] = useState(true);
  const autoRecordingAttemptedRef = useRef(false);
  const previousRecorderStatusRef = useRef<string>("idle");

  const socket = useStudentSocket(
    joined?.uploadOnly ? null : (joined?.sessionId ?? null),
    joined?.uploadOnly ? null : (joined?.accessToken ?? null),
  );
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
        clearJoinedSession(join.session_id);
        throw new Error(`Join code is for role "${join.role}", not student.`);
      }
      const session = await fetchSession(join.session_id);
      const clips = await fetchClipManifest(session.case_id);
      rememberJoinedSession({
        sessionId: session.session_id,
        role: "student",
        accessToken: join.access_token,
      });
      setJoined({
        sessionId: session.session_id,
        caseId: session.case_id,
        clips,
        accessToken: join.access_token,
      });
    } catch (cause) {
      setJoinError(String(cause instanceof Error ? cause.message : cause));
    } finally {
      setJoinPending(false);
    }
  }, []);

  const handleUploadLogin = useCallback(async (sessionId: string, code: string) => {
    setJoinPending(true);
    setJoinError(null);
    try {
      const login = await uploadLogin(sessionId, code);
      await resumeUnauthorizedUploads(sessionId);
      rememberJoinedSession({
        sessionId,
        role: "student",
        accessToken: login.access_token,
      });
      setJoined({
        sessionId,
        caseId: "",
        clips: [],
        accessToken: login.access_token,
        restoredFromStorage: true,
        uploadOnly: true,
      });
    } catch (cause) {
      setJoinError(String(cause instanceof Error ? cause.message : cause));
    } finally {
      setJoinPending(false);
    }
  }, []);

  useEffect(() => {
    if (!restorePending) {
      return;
    }
    const stored = readJoinedSession();
    if (stored === null || stored.role !== "student") {
      setRestorePending(false);
      return;
    }
    let cancelled = false;
    void (async () => {
      const restored = await restoreStudentSession(stored, {
        fetchSession,
        fetchClipManifest,
        fetchUploadStatus,
        clearJoinedSession,
      });
      if (cancelled) {
        return;
      }
      if (restored.kind === "live") {
        setRestoreError(null);
        setJoined({
          sessionId: restored.sessionId,
          caseId: restored.caseId,
          clips: restored.clips,
          accessToken: restored.accessToken,
          restoredFromStorage: true,
        });
      } else if (restored.kind === "upload-only") {
        setRestoreError(null);
        setJoined({
          sessionId: restored.sessionId,
          caseId: "",
          clips: [],
          accessToken: restored.accessToken,
          restoredFromStorage: true,
          uploadOnly: true,
        });
      } else if (restored.kind === "rejected") {
        setRestoreError(null);
        setJoinError(restored.message);
      } else {
        setRestoreError(UNAVAILABLE_RESTORE_MESSAGE);
      }
      setRestorePending(false);
    })();
    return () => {
      cancelled = true;
    };
  }, [restorePending]);

  useEffect(() => {
    if (restorePending || joined !== null || readJoinedSession() !== null) {
      return;
    }
    let cancelled = false;
    setRecoveryChecked(false);
    void findUnfinishedUploadSessions()
      .then((found) => {
        if (!cancelled) {
          setRecoveries(found);
        }
      })
      .catch(() => {
        if (!cancelled) {
          setRecoveries([]);
        }
      })
      .finally(() => {
        if (!cancelled) {
          setRecoveryChecked(true);
        }
      });
    return () => {
      cancelled = true;
    };
  }, [restorePending, joined]);

  useEffect(() => {
    if (
      joined === null ||
      joined.restoredFromStorage ||
      socket.status !== "open" ||
      autoRecordingAttemptedRef.current
    ) {
      return;
    }
    autoRecordingAttemptedRef.current = true;
    void recorder.start();
  }, [joined, recorder.start, socket.status]);

  // A revoked live token must stop the camera, not only the reconnects.
  const liveSessionId = joined && !joined.uploadOnly ? joined.sessionId : null;
  useEffect(() => {
    if (
      liveSessionId === null ||
      socket.status !== "closed" ||
      !isRevokedCloseReason(socket.closedReason)
    ) {
      return;
    }
    void interruptRecording(liveSessionId);
  }, [liveSessionId, socket.closedReason, socket.status]);

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

  if (restorePending || joined === null) {
    if (restorePending && joined === null) {
      return (
        <div className="join-shell">
          <p className="join-panel">Restoring session…</p>
        </div>
      );
    }
    if (restoreError && joined === null) {
      return (
        <div className="join-shell">
          <div className="join-panel">
            <h1>Session not reachable</h1>
            <p className="join-error">{restoreError}</p>
            <button
              type="button"
              onClick={() => {
                setRestoreError(null);
                setRestorePending(true);
              }}
            >
              Retry
            </button>
          </div>
        </div>
      );
    }
    if (!recoveryChecked) {
      return (
        <div className="join-shell">
          <p className="join-panel">Checking local recording…</p>
        </div>
      );
    }
    if (!preferJoin && recoveries.length > 0) {
      return (
        <UploadCodeForm
          sessions={recoveries}
          pending={joinPending}
          error={joinError}
          onSubmit={handleUploadLogin}
          onJoinAnother={() => {
            setJoinError(null);
            setPreferJoin(true);
          }}
        />
      );
    }
    return (
      <JoinSessionModal
        pending={joinPending}
        error={joinError}
        onJoin={handleJoin}
        onResumeUpload={
          recoveries.length > 0
            ? () => {
                setJoinError(null);
                setPreferJoin(false);
              }
            : undefined
        }
      />
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
        allowStartRecording={!joined.restoredFromStorage && recorder.canStart}
      />
    </div>
  );
}
