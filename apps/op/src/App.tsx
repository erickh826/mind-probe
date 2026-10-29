import { useCallback, useEffect, useMemo, useState } from "react";

import { createOpClient, OpApiError, type OpClient } from "./api";
import { CodesPanel } from "./components/CodesPanel";
import { CreateSessionForm } from "./components/CreateSessionForm";
import { SessionDetail } from "./components/SessionDetail";
import { SessionTable } from "./components/SessionTable";
import { TokenGate } from "./components/TokenGate";
import { API_BASE_URL } from "./config";
import { errorMessage, formatServerTime, type ProgressRead } from "./opLogic";
import type { HeldCodes, OperatorSessionRow, SessionListResponse } from "./types";

type Reads = Record<string, { previous?: ProgressRead; current: ProgressRead }>;

function describeError(error: unknown): string {
  if (error instanceof OpApiError) {
    return errorMessage(error.status, error.detail);
  }
  return errorMessage(0, "");
}

function isAuthFailure(error: unknown): boolean {
  return error instanceof OpApiError && error.status === 401;
}

function nextReads(previous: Reads, rows: OperatorSessionRow[], readAtMs: number): Reads {
  const reads: Reads = {};
  for (const row of rows) {
    reads[row.session_id] = {
      previous: previous[row.session_id]?.current,
      current: { storedSequences: row.stored_sequences, readAtMs },
    };
  }
  return reads;
}

export default function App() {
  // The operator token lives only in React state: never in storage or a URL.
  const [token, setToken] = useState<string | null>(null);
  const client = useMemo<OpClient | null>(
    () => (token ? createOpClient({ baseUrl: API_BASE_URL, token }) : null),
    [token],
  );

  const [gateBusy, setGateBusy] = useState(false);
  const [gateError, setGateError] = useState<string | null>(null);

  const [rows, setRows] = useState<OperatorSessionRow[]>([]);
  const [reads, setReads] = useState<Reads>({});
  const [lastReadAt, setLastReadAt] = useState<string | null>(null);
  const [listBusy, setListBusy] = useState(false);
  const [listError, setListError] = useState<string | null>(null);
  const [cases, setCases] = useState<string[]>([]);
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [held, setHeld] = useState<Record<string, HeldCodes>>({});

  const [createBusy, setCreateBusy] = useState(false);
  const [createError, setCreateError] = useState<string | null>(null);
  const [createdCount, setCreatedCount] = useState(0);

  const [nowMs, setNowMs] = useState(() => Date.now());
  useEffect(() => {
    const timer = window.setInterval(() => setNowMs(Date.now()), 1000);
    return () => window.clearInterval(timer);
  }, []);

  const signOut = useCallback((message: string | null) => {
    setToken(null);
    setRows([]);
    setReads({});
    setHeld({});
    setSelectedId(null);
    setCases([]);
    setGateError(message);
  }, []);

  const applyList = useCallback((list: SessionListResponse) => {
    const readAtMs = Date.now();
    setRows(list.sessions);
    setReads((previous) => nextReads(previous, list.sessions, readAtMs));
    setLastReadAt(new Date(readAtMs).toISOString());
  }, []);

  const refresh = useCallback(async () => {
    if (!client) {
      return;
    }
    setListBusy(true);
    setListError(null);
    try {
      applyList(await client.listSessions());
    } catch (error) {
      if (isAuthFailure(error)) {
        signOut(describeError(error));
        return;
      }
      setListError(describeError(error));
    } finally {
      setListBusy(false);
    }
  }, [applyList, client, signOut]);

  async function signIn(candidate: string) {
    setGateBusy(true);
    setGateError(null);
    const probe = createOpClient({ baseUrl: API_BASE_URL, token: candidate });
    try {
      const [list, caseList] = await Promise.all([probe.listSessions(), probe.listCases()]);
      applyList(list);
      setCases(caseList.cases);
      setToken(candidate);
    } catch (error) {
      setGateError(describeError(error));
    } finally {
      setGateBusy(false);
    }
  }

  async function createSession(sessionId: string, caseId: string) {
    if (!client) {
      return;
    }
    setCreateBusy(true);
    setCreateError(null);
    try {
      const created = await client.createSession(sessionId, caseId);
      setHeld((current) => ({
        ...current,
        [created.session_id]: { join: created.join_codes, review: created.review_codes },
      }));
      setSelectedId(created.session_id);
      setCreatedCount((count) => count + 1);
      await refresh();
    } catch (error) {
      if (isAuthFailure(error)) {
        signOut(describeError(error));
        return;
      }
      setCreateError(describeError(error));
    } finally {
      setCreateBusy(false);
    }
  }

  // Plaintext codes cannot be fetched again, so warn before a reload drops them.
  const holdsCodes = Object.keys(held).length > 0;
  useEffect(() => {
    if (!holdsCodes) {
      return;
    }
    const warn = (event: BeforeUnloadEvent) => {
      event.preventDefault();
      event.returnValue = "";
    };
    window.addEventListener("beforeunload", warn);
    return () => window.removeEventListener("beforeunload", warn);
  }, [holdsCodes]);

  if (!client) {
    return <TokenGate busy={gateBusy} error={gateError} onSubmit={signIn} />;
  }

  const selectedRow = rows.find((row) => row.session_id === selectedId) ?? null;
  const heldCodeIds = new Set(Object.keys(held));

  return (
    <div className="layout">
      <header className="topbar">
        <h1>mind-probe · OP 管理</h1>
        <span className="hint">API：{API_BASE_URL || "（同源）"}</span>
        <button type="button" className="secondary" onClick={() => signOut(null)}>
          清除 token 並離開
        </button>
      </header>

      <CreateSessionForm
        key={createdCount}
        cases={cases}
        busy={createBusy}
        error={createError}
        onCreate={createSession}
      />

      <section className="panel">
        <div className="row">
          <h2>場次</h2>
          <button type="button" onClick={refresh} disabled={listBusy}>
            {listBusy ? "讀取中…" : "重新整理"}
          </button>
          <span className="hint">上次讀取：{lastReadAt ? formatServerTime(lastReadAt) : "—"}</span>
        </div>
        {listError && <p className="error">{listError}</p>}
        <SessionTable
          rows={rows}
          nowMs={nowMs}
          selectedId={selectedId}
          heldCodeIds={heldCodeIds}
          onSelect={setSelectedId}
        />
      </section>

      {selectedRow && (
        <div className="split">
          <CodesPanel
            sessionId={selectedRow.session_id}
            codes={held[selectedRow.session_id]}
            onForget={() =>
              setHeld((current) => {
                const next = { ...current };
                delete next[selectedRow.session_id];
                return next;
              })
            }
          />
          <SessionDetail
            client={client}
            row={selectedRow}
            nowMs={nowMs}
            progress={reads[selectedRow.session_id]}
            onUploadCode={(sessionId, issued) =>
              setHeld((current) => ({
                ...current,
                [sessionId]: {
                  ...current[sessionId],
                  upload: { code: issued.upload_code, expiresAt: issued.expires_at },
                },
              }))
            }
            onReviewCodes={(sessionId, codes) =>
              setHeld((current) => ({
                ...current,
                [sessionId]: { ...current[sessionId], review: codes.review_codes },
              }))
            }
          />
        </div>
      )}
    </div>
  );
}
