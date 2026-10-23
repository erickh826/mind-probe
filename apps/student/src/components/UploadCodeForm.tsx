import { FormEvent, useState } from "react";

interface UploadCodeFormProps {
  sessionId: string;
  pending: boolean;
  error: string | null;
  onSubmit: (sessionId: string, code: string) => Promise<void>;
}

export function UploadCodeForm({ sessionId, pending, error, onSubmit }: UploadCodeFormProps) {
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
          <input readOnly value={sessionId} />
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
        <button disabled={pending || code.trim() === ""}>
          {pending ? "Checking code..." : "Continue upload"}
        </button>
      </form>
    </div>
  );
}
