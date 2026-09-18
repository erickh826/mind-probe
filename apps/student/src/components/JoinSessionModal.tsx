import { FormEvent, useState } from "react";

interface JoinSessionModalProps {
  pending: boolean;
  error: string | null;
  onJoin: (sessionId: string, joinCode: string) => Promise<void>;
}

export function JoinSessionModal({ pending, error, onJoin }: JoinSessionModalProps) {
  const [sessionId, setSessionId] = useState("S001");
  const [joinCode, setJoinCode] = useState("");

  const submit = (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    void onJoin(sessionId.trim(), joinCode.trim());
  };

  return (
    <div className="join-shell">
      <form className="join-panel" onSubmit={submit}>
        <div>
          <p className="eyebrow">mind-probe Student</p>
          <h1>Join Session</h1>
        </div>
        <label>
          Session ID
          <input
            autoComplete="off"
            disabled={pending}
            value={sessionId}
            onChange={(event) => setSessionId(event.target.value)}
          />
        </label>
        <label>
          Join Code
          <input
            autoComplete="one-time-code"
            disabled={pending}
            value={joinCode}
            onChange={(event) => setJoinCode(event.target.value)}
          />
        </label>
        {error ? <p className="join-error">{error}</p> : null}
        <button disabled={pending || sessionId.trim() === "" || joinCode.trim() === ""}>
          {pending ? "Joining..." : "Join as Student"}
        </button>
      </form>
    </div>
  );
}
