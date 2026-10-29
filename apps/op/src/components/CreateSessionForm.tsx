import { FormEvent, useEffect, useState } from "react";

import { defaultSessionId, sessionIdProblem } from "../opLogic";

interface CreateSessionFormProps {
  cases: string[];
  busy: boolean;
  error: string | null;
  onCreate: (sessionId: string, caseId: string) => void;
}

export function CreateSessionForm({ cases, busy, error, onCreate }: CreateSessionFormProps) {
  const [sessionId, setSessionId] = useState(() => defaultSessionId(new Date()));
  const [caseId, setCaseId] = useState("");

  useEffect(() => {
    if (!caseId && cases.length > 0) {
      setCaseId(cases[0]);
    }
  }, [caseId, cases]);

  const trimmed = sessionId.trim();
  const problem = sessionIdProblem(trimmed);

  function handleSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (problem || !caseId || busy) {
      return;
    }
    onCreate(trimmed, caseId);
  }

  return (
    <form className="panel create-form" onSubmit={handleSubmit}>
      <h2>建立場次</h2>
      <div className="row">
        <label>
          場次 ID
          <input
            value={sessionId}
            spellCheck={false}
            onChange={(event) => setSessionId(event.target.value)}
          />
        </label>
        <button type="button" onClick={() => setSessionId(defaultSessionId(new Date()))}>
          用現在時間
        </button>
        <label>
          案件
          <select value={caseId} onChange={(event) => setCaseId(event.target.value)}>
            {cases.length === 0 && <option value="">（沒有案件）</option>}
            {cases.map((id) => (
              <option key={id} value={id}>
                {id}
              </option>
            ))}
          </select>
        </label>
        <button type="submit" disabled={busy || Boolean(problem) || !caseId}>
          {busy ? "建立中…" : "建立"}
        </button>
      </div>
      {trimmed && problem && <p className="error">{problem}</p>}
      {error && <p className="error">{error}</p>}
    </form>
  );
}
