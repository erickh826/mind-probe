import { useCallback, useEffect, useMemo, useState } from "react";

import { createOpClient, OpApiError, type OpClient } from "./api";
import { CodesPanel } from "./components/CodesPanel";
import { CreateSessionForm } from "./components/CreateSessionForm";
import { SessionDetail } from "./components/SessionDetail";
import { SessionTable } from "./components/SessionTable";
import { TokenGate } from "./components/TokenGate";
import { API_BASE_URL } from "./config";
import {
  addFirstReads,
  appendPage,
  createGeneration,
  errorMessage,
  formatServerTime,
  nextReads,
  type ProgressReads,
} from "./opLogic";
import type { HeldCodes, OperatorSessionRow } from "./types";

const PAGE_SIZE = 50;
/** A refresh re-reads at most this many pages of what the operator scrolled back through. */
const REFRESH_MAX_PAGES = 20;

function describeError(error: unknown): string {
  if (error instanceof OpApiError) {
    return errorMessage(error.status, error.detail);
  }
  return errorMessage(0, "");
}

function isAuthFailure(error: unknown): boolean {
  return error instanceof OpApiError && error.status === 401;
}

export default function App() {
  // The operator token lives only in React state: never in storage or a URL.
  const [token, setToken] = useState<string | null>(null);
  const client = useMemo<OpClient | null>(
    () => (token ? createOpClient({ baseUrl: API_BASE_URL, token }) : null),
    [token],
  );

  // Advanced on every sign-in and sign-out. A response whose ticket predates the
  // advance answers a token this tab no longer holds, so it must not write state.
  const [auth] = useState(createGeneration);
  // Advanced per list request, so only the latest one replaces the rows.
  const [listGen] = useState(createGeneration);

  const [gateBusy, setGateBusy] = useState(false);
  const [gateError, setGateError] = useState<string | null>(null);

  const [rows, setRows] = useState<OperatorSessionRow[]>([]);
  const [nextCursor, setNextCursor] = useState<string | null>(null);
  const [reads, setReads] = useState<ProgressReads>({});
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

  const signOut = useCallback(
    (message: string | null) => {
      auth.advance();
      listGen.advance();
      setToken(null);
      setRows([]);
      setNextCursor(null);
      setReads({});
      setLastReadAt(null);
      setListBusy(false);
      setListError(null);
      setHeld({});
      setSelectedId(null);
      setCases([]);
      setCreateBusy(false);
      setCreateError(null);
      setGateError(message);
    },
    [auth, listGen],
  );

  const listFailed = useCallback(
    (error: unknown) => {
      if (isAuthFailure(error)) {
        signOut(describeError(error));
        return;
      }
      setListError(describeError(error));
    },
    [signOut],
  );

  const shownCount = rows.length;
  const refresh = useCallback(async () => {
    if (!client) {
      return;
    }
    const live = auth.ticket();
    const latest = listGen.advance();
    const current = () => live() && latest();
    setListBusy(true);
    setListError(null);
    try {
      let page = await client.listSessions({ limit: PAGE_SIZE });
      let fresh = page.sessions;
      // Re-read as far back as the operator had loaded, so those rows stay current too.
      for (
        let pages = 1;
        page.next_cursor && fresh.length < shownCount && pages < REFRESH_MAX_PAGES;
        pages += 1
      ) {
        if (!current()) {
          return;
        }
        page = await client.listSessions({ limit: PAGE_SIZE, before: page.next_cursor });
        fresh = appendPage(fresh, page.sessions);
      }
      if (!current()) {
        return;
      }
      const readAtMs = Date.now();
      setRows(fresh);
      setNextCursor(page.next_cursor);
      setReads((previous) => nextReads(previous, fresh, readAtMs));
      setLastReadAt(new Date(readAtMs).toISOString());
    } catch (error) {
      if (current()) {
        listFailed(error);
      }
    } finally {
      if (current()) {
        setListBusy(false);
      }
    }
  }, [auth, client, listFailed, listGen, shownCount]);

  async function loadOlder() {
    if (!client || !nextCursor) {
      return;
    }
    const live = auth.ticket();
    const latest = listGen.advance();
    const current = () => live() && latest();
    setListBusy(true);
    setListError(null);
    try {
      const page = await client.listSessions({ limit: PAGE_SIZE, before: nextCursor });
      if (!current()) {
        return;
      }
      const readAtMs = Date.now();
      setRows((shown) => appendPage(shown, page.sessions));
      setReads((previous) => addFirstReads(previous, page.sessions, readAtMs));
      setNextCursor(page.next_cursor);
    } catch (error) {
      if (current()) {
        listFailed(error);
      }
    } finally {
      if (current()) {
        setListBusy(false);
      }
    }
  }

  async function signIn(candidate: string) {
    const live = auth.advance();
    setGateBusy(true);
    setGateError(null);
    const probe = createOpClient({ baseUrl: API_BASE_URL, token: candidate });
    try {
      const [page, caseList] = await Promise.all([
        probe.listSessions({ limit: PAGE_SIZE }),
        probe.listCases(),
      ]);
      if (!live()) {
        return;
      }
      const readAtMs = Date.now();
      setRows(page.sessions);
      setNextCursor(page.next_cursor);
      setReads(nextReads({}, page.sessions, readAtMs));
      setLastReadAt(new Date(readAtMs).toISOString());
      setCases(caseList.cases);
      setToken(candidate);
    } catch (error) {
      if (live()) {
        setGateError(describeError(error));
      }
    } finally {
      setGateBusy(false);
    }
  }

  async function createSession(sessionId: string, caseId: string) {
    if (!client) {
      return;
    }
    const live = auth.ticket();
    setCreateBusy(true);
    setCreateError(null);
    try {
      const created = await client.createSession(sessionId, caseId);
      // Signed out meanwhile: the codes leave with the token instead of
      // surfacing in the next sign-in.
      if (!live()) {
        return;
      }
      setHeld((current) => ({
        ...current,
        [created.session_id]: { join: created.join_codes, review: created.review_codes },
      }));
      setSelectedId(created.session_id);
      setCreatedCount((count) => count + 1);
      await refresh();
    } catch (error) {
      if (!live()) {
        return;
      }
      if (isAuthFailure(error)) {
        signOut(describeError(error));
        return;
      }
      setCreateError(describeError(error));
    } finally {
      if (live()) {
        setCreateBusy(false);
      }
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
        <div className="row">
          <span className="hint">
            已顯示 {rows.length} 個場次{nextCursor ? "" : "（全部）"}
          </span>
          {nextCursor && (
            <button type="button" className="secondary" onClick={loadOlder} disabled={listBusy}>
              {listBusy ? "讀取中…" : "載入更早的場次"}
            </button>
          )}
        </div>
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
            // A fresh instance per session, so one session's late answers never reach another.
            key={selectedRow.session_id}
            client={client}
            authTicket={auth.ticket}
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
