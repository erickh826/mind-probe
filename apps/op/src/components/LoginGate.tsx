import { FormEvent, useState } from "react";

interface LoginGateProps {
  busy: boolean;
  error: string | null;
  onSubmit: (username: string, password: string) => void;
}

export function LoginGate({ busy, error, onSubmit }: LoginGateProps) {
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");

  function handleSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const name = username.trim();
    if (name && password && !busy) {
      onSubmit(name, password);
      setPassword("");
    }
  }

  return (
    <main className="gate">
      <h1>mind-probe · OP 管理</h1>
      <form onSubmit={handleSubmit} className="panel">
        <label htmlFor="op-username">管理員帳號</label>
        <input
          id="op-username"
          autoComplete="username"
          spellCheck={false}
          maxLength={200}
          value={username}
          onChange={(event) => setUsername(event.target.value)}
        />
        <label htmlFor="op-password">密碼</label>
        <input
          id="op-password"
          type="password"
          autoComplete="current-password"
          maxLength={1024}
          value={password}
          onChange={(event) => setPassword(event.target.value)}
        />
        <button type="submit" disabled={busy || !username.trim() || !password}>
          {busy ? "登入中…" : "登入"}
        </button>
        {error && <p className="error">{error}</p>}
        <p className="hint">
          登入後的憑證只存在這個分頁的記憶體，不寫入瀏覽器儲存區；重新整理、關閉分頁或登入到期後要重新登入。
          只在可信任的電腦上使用。
        </p>
      </form>
    </main>
  );
}
