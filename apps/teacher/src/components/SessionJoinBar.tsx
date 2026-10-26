import { FormEvent, useEffect, useMemo, useState } from "react";

import { credentialFieldCopy, type SessionAuthMode } from "../sessionAuthMode";
import type { RecordingStatus, SessionInfo } from "../types";

export type { SessionAuthMode };

export interface SessionLoadRequest {
  sessionId: string;
  authMode: SessionAuthMode;
  credential: string;
  clientId: string;
}

interface SessionJoinBarProps {
  authMode: SessionAuthMode;
  formNonce: number;
  initialSessionId?: string;
  isLoading: boolean;
  logoutPending: boolean;
  onAuthModeChange: (mode: SessionAuthMode) => void;
  onLoadSession: (request: SessionLoadRequest) => void;
  onLogout: () => void;
  recordingStatus: RecordingStatus | null;
  session: SessionInfo | null;
  showLogout: boolean;
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

export function SessionJoinBar({
  authMode,
  formNonce,
  initialSessionId = "",
  isLoading,
  logoutPending,
  onAuthModeChange,
  onLoadSession,
  onLogout,
  recordingStatus,
  session,
  showLogout,
}: SessionJoinBarProps) {
  const [sessionId, setSessionId] = useState(session?.session_id || initialSessionId);
  const [credential, setCredential] = useState("");
  const clientId = useMemo(defaultClientId, []);

  useEffect(() => {
    setCredential("");
  }, [formNonce]);

  const canSubmit = sessionId.trim().length > 0 && credential.trim().length > 0 && !isLoading && !logoutPending;

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
              onChange={() => onAuthModeChange("join_code")}
              type="radio"
              value="join_code"
            />
            Join code
          </label>
          <label className={authMode === "bearer" ? "active" : ""}>
            <input
              checked={authMode === "bearer"}
              name="auth-mode"
              onChange={() => onAuthModeChange("bearer")}
              type="radio"
              value="bearer"
            />
            Bearer token
          </label>
          <label className={authMode === "review_code" ? "active" : ""}>
            <input
              checked={authMode === "review_code"}
              name="auth-mode"
              onChange={() => onAuthModeChange("review_code")}
              type="radio"
              value="review_code"
            />
            Review code
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
          <span>{credentialFieldCopy(authMode).legend}</span>
          <input
            autoComplete="off"
            onChange={(event) => setCredential(event.target.value)}
            placeholder={credentialFieldCopy(authMode).placeholder}
            type={authMode === "bearer" ? "password" : "text"}
            value={credential}
          />
        </label>

        <div className="join-actions">
          <button className="primary-button" disabled={!canSubmit} type="submit">
            {isLoading ? "Loading" : "Load review"}
          </button>
          {showLogout ? (
            <button
              className="secondary-button"
              disabled={logoutPending}
              onClick={onLogout}
              type="button"
            >
              {logoutPending ? "Signing out" : "Log out"}
            </button>
          ) : null}
        </div>
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
