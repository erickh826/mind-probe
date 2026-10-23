import { FormEvent, useState } from "react";
import type { UploadRecovery } from "../uploadRecovery";

interface UploadCodeFormProps {
  sessions: UploadRecovery[];
  pending: boolean;
  error: string | null;
  onSubmit: (sessionId: string, code: string) => Promise<void>;
  onJoinAnother: () => void;
}

export function UploadCodeForm({
  sessions,
  pending,
  error,
  onSubmit,
  onJoinAnother,
}: UploadCodeFormProps) {
  const [sessionId, setSessionId] = useState(sessions[sessions.length - 1]?.sessionId ?? "");
  const [code, setCode] = useState("");

  const submit = (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    void onSubmit(sessionId, code.trim());
  };

  return (
    <div className="join-shell">
      <form className="join-panel" onSubmit={submit}>
        <div>
          <p className="eyebrow">mind-probe Student</p>
          <h1>Resume upload</h1>
          <p>This browser still has an unfinished recording. Enter the upload code from the operator.</p>
        </div>
        <label>
          Session ID
          {sessions.length > 1 ? (
            <select
              disabled={pending}
              value={sessionId}
              onChange={(event) => setSessionId(event.target.value)}
            >
              {sessions.map((session) => (
                <option key={session.sessionId} value={session.sessionId}>
                  {session.sessionId}
                </option>
              ))}
            </select>
          ) : (
            <input readOnly value={sessionId} />
          )}
        </label>
        <label>
          Upload code
          <input
            autoComplete="one-time-code"
            disabled={pending}
            value={code}
            onChange={(event) => setCode(event.target.value)}
          />
        </label>
        {error ? <p className="join-error">{error}</p> : null}
        <button disabled={pending || code.trim() === "" || sessionId === ""}>
          {pending ? "Checking code..." : "Continue upload"}
        </button>
        <button className="join-secondary" type="button" disabled={pending} onClick={onJoinAnother}>
          Join another session
        </button>
      </form>
    </div>
  );
}
