import { FormEvent, useState } from "react";

interface TokenGateProps {
  busy: boolean;
  error: string | null;
  onSubmit: (token: string) => void;
}

export function TokenGate({ busy, error, onSubmit }: TokenGateProps) {
  const [token, setToken] = useState("");

  function handleSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const trimmed = token.trim();
    if (trimmed && !busy) {
      onSubmit(trimmed);
      setToken("");
    }
  }

  return (
    <main className="gate">
      <h1>mind-probe · OP 管理</h1>
      <form onSubmit={handleSubmit} className="panel">
        <label htmlFor="op-token">Operator token</label>
        <input
          id="op-token"
          type="password"
          autoComplete="off"
          spellCheck={false}
          value={token}
          onChange={(event) => setToken(event.target.value)}
        />
        <button type="submit" disabled={busy || !token.trim()}>
          {busy ? "驗證中…" : "進入"}
        </button>
        {error && <p className="error">{error}</p>}
        <p className="hint">
          Token 只存在這個分頁的記憶體，不寫入瀏覽器儲存區。重新整理或關閉分頁後要重新輸入。
          只在可信任的電腦上使用，不要把 token 給學生或其他角色。
        </p>
      </form>
    </main>
  );
}
