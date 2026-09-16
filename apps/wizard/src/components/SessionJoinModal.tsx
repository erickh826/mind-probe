import { useState } from "react";
import type { FormEvent } from "react";
import { ApiError, fetchSession, joinSession } from "../api";

// Session 加入畫面（ADR-0001 §8）。
// The Wizard trades a one-time join code for the HttpOnly session cookie the
// WebSocket upgrade verifies. The *role* is decided server-side from the code,
// so a non-Wizard code here surfaces later as a 4401 close, not a silent
// mis-join.

export interface JoinedSession {
  sessionId: string;
  caseId: string;
}

interface Props {
  onJoined: (session: JoinedSession) => void;
}

/** Stable per-device id bound into the token, so a reload reuses one identity. */
function clientId(): string {
  const key = "mindprobe.wizard.client_id";
  const existing = window.localStorage.getItem(key);
  if (existing) {
    return existing;
  }
  const generated =
    typeof crypto !== "undefined" && typeof crypto.randomUUID === "function"
      ? crypto.randomUUID()
      : `wizard-${Date.now().toString(36)}`;
  window.localStorage.setItem(key, generated);
  return generated;
}

const inputStyle = {
  width: "100%",
  padding: "8px 10px",
  marginTop: 4,
  fontSize: 15,
  fontFamily: "inherit",
  boxSizing: "border-box" as const,
};

export default function SessionJoinModal({ onJoined }: Props) {
  const [sessionId, setSessionId] = useState("");
  const [joinCode, setJoinCode] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function handleSubmit(event: FormEvent) {
    event.preventDefault();
    const trimmedSession = sessionId.trim();
    const trimmedCode = joinCode.trim();
    if (!trimmedSession || !trimmedCode) {
      setError("請輸入 Session ID 與 Join Code");
      return;
    }
    setBusy(true);
    setError(null);
    try {
      const result = await joinSession(trimmedSession, trimmedCode, clientId());
      if (result.role !== "wizard") {
        // Fail loudly here rather than letting the socket close with 4401:
        // the join code was issued for a different console.
        setError(`此 Join Code 對應角色為 ${result.role}，不是 wizard`);
        return;
      }
      const info = await fetchSession(trimmedSession);
      onJoined({ sessionId: trimmedSession, caseId: info.case_id });
    } catch (cause) {
      setError(cause instanceof ApiError ? cause.message : String(cause));
    } finally {
      setBusy(false);
    }
  }

  return (
    <div
      style={{
        display: "flex",
        alignItems: "center",
        justifyContent: "center",
        minHeight: "100vh",
        fontFamily: "system-ui",
        background: "#101216",
        color: "#e8e8ea",
      }}
    >
      <form
        onSubmit={handleSubmit}
        style={{
          width: 380,
          padding: 24,
          background: "#1b1e24",
          borderRadius: 10,
          border: "1px solid #2c313a",
        }}
      >
        <h1 style={{ fontSize: 20, marginTop: 0 }}>Wizard 控制台 · 加入 Session</h1>
        <p style={{ fontSize: 13, color: "#9aa0ab", marginTop: 0 }}>
          輸入教師建立 Session 時取得的 Wizard join code。
        </p>

        <label style={{ display: "block", marginBottom: 12, fontSize: 13 }}>
          Session ID
          <input
            style={inputStyle}
            value={sessionId}
            onChange={(e) => setSessionId(e.target.value)}
            placeholder="S001"
            autoFocus
          />
        </label>

        <label style={{ display: "block", marginBottom: 16, fontSize: 13 }}>
          Join Code
          <input
            style={inputStyle}
            value={joinCode}
            onChange={(e) => setJoinCode(e.target.value)}
            placeholder="wizard join code"
          />
        </label>

        {error !== null && (
          <p
            role="alert"
            style={{ color: "#ff8080", fontSize: 13, margin: "0 0 12px" }}
          >
            {error}
          </p>
        )}

        <button
          type="submit"
          disabled={busy}
          style={{
            width: "100%",
            padding: "10px 12px",
            fontSize: 15,
            fontFamily: "inherit",
            cursor: busy ? "progress" : "pointer",
          }}
        >
          {busy ? "加入中…" : "加入 Session"}
        </button>
      </form>
    </div>
  );
}
