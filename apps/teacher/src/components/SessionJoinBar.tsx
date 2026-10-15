import { FormEvent, useMemo, useState } from "react";

import type { RecordingStatus, SessionInfo } from "../types";

export type SessionAuthMode = "join_code" | "bearer";

export interface SessionLoadRequest {
  sessionId: string;
  authMode: SessionAuthMode;
  credential: string;
  clientId: string;
}

interface SessionJoinBarProps {
  isLoading: boolean;
  onLoadSession: (request: SessionLoadRequest) => void;
  recordingStatus: RecordingStatus | null;
  session: SessionInfo | null;
}

function defaultClientId(): string {
  const stored = window.localStorage.getItem("mindprobe_teacher_client_id");
  if (stored) {
    return stored;
  }
  const generated = `teacher-${crypto.randomUUID?.() ?? Math.random().toString(36).slice(2)}`;
  window.localStorage.setItem("mindprobe_teacher_client_id", generated);
  return generated;
}

export function SessionJoinBar({ isLoading, onLoadSession, recordingStatus, session }: SessionJoinBarProps) {
  const [sessionId, setSessionId] = useState(session?.session_id ?? "");
  const [credential, setCredential] = useState("");
  const [authMode, setAuthMode] = useState<SessionAuthMode>("join_code");
  const clientId = useMemo(defaultClientId, []);

  const canSubmit = sessionId.trim().length > 0 && credential.trim().length > 0 && !isLoading;

  function handleSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!canSubmit) {
      return;
    }
    onLoadSession({
      sessionId: sessionId.trim(),
      authMode,
      credential: credential.trim(),
      clientId,
    });
  }

  return (
    <header className="join-bar">
      <div className="brand-lockup">
        <p className="eyebrow">mind-probe</p>
        <h1>Teacher Review</h1>
      </div>

      <form className="join-form" onSubmit={handleSubmit}>
        <div className="segmented-control" role="radiogroup" aria-label="Authentication mode">
          <label className={authMode === "join_code" ? "active" : ""}>
            <input
              checked={authMode === "join_code"}
              name="auth-mode"
              onChange={() => setAuthMode("join_code")}
              type="radio"
              value="join_code"
            />
            Join code
          </label>
          <label className={authMode === "bearer" ? "active" : ""}>
            <input
              checked={authMode === "bearer"}
              name="auth-mode"
              onChange={() => setAuthMode("bearer")}
              type="radio"
              value="bearer"
            />
            Bearer token
          </label>
        </div>

        <label className="field compact-field">
          <span>Session ID</span>
          <input
            autoComplete="off"
            onChange={(event) => setSessionId(event.target.value)}
            placeholder="S001"
            value={sessionId}
          />
        </label>

        <label className="field token-field">
          <span>{authMode === "join_code" ? "Teacher code" : "Token"}</span>
          <input
            autoComplete="off"
            onChange={(event) => setCredential(event.target.value)}
            placeholder={authMode === "join_code" ? "Join code" : "Bearer token"}
            type={authMode === "join_code" ? "text" : "password"}
            value={credential}
          />
        </label>

        <button className="primary-button" disabled={!canSubmit} type="submit">
          {isLoading ? "Loading" : "Load review"}
        </button>
      </form>

      <div className="recording-health" aria-label="Recording health">
        <span className={recordingStatus?.mp4.exists || recordingStatus?.raw_webm.exists ? "dot good" : "dot"} />
        <div>
          <strong>{recordingStatus?.mp4.exists ? "MP4 ready" : recordingStatus?.raw_webm.exists ? "Raw WebM ready" : "No recording"}</strong>
          <span>{recordingStatus ? `${recordingStatus.chunk_count} chunks` : "Awaiting session"}</span>
        </div>
      </div>
    </header>
  );
}
