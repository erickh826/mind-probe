import { useCallback, useEffect, useMemo, useState } from "react";
import type { UncoveredQuestionPayload } from "@mind-probe/shared";
import { ApiError, fetchDecisionTree, findThinkingClipId, resolveClipId } from "./api";
import { useSessionSocket } from "./useSessionSocket";
import {
  CATEGORY_KEYS,
  EMPTY_SELECTION,
  OPTION_KEYS,
  TONE_KEYS,
} from "./types";
import type {
  CategoryKey,
  DecisionTree,
  OptionKey,
  StagedSelection,
  ToneKey,
} from "./types";
import SessionJoinModal from "./components/SessionJoinModal";
import type { JoinedSession } from "./components/SessionJoinModal";
import SessionStatusHeader from "./components/SessionStatusHeader";
import DecisionTreePanel from "./components/DecisionTreePanel";
import FallbackPanel from "./components/FallbackPanel";
import PauseResumeModal from "./components/PauseResumeModal";
import UncoveredQuestionModal from "./components/UncoveredQuestionModal";
import HotkeyCheatsheet from "./components/HotkeyCheatsheet";

// Wizard 控制台（spec §8）：三層結構 分類(F1–F8) → 回答選項(1–6) → 語氣狀態。
// Wizard 在兩至三次按鍵內選出回答。
//
// This component owns the global hotkey dispatcher and wires the panels to
// `useSessionSocket`, which owns the ADR-0001/0002/0003 protocol itself.

/** True when the keystroke belongs to a text field rather than the console. */
function isTypingTarget(target: EventTarget | null): boolean {
  if (!(target instanceof HTMLElement)) {
    return false;
  }
  const tag = target.tagName;
  return (
    tag === "INPUT" ||
    tag === "TEXTAREA" ||
    tag === "SELECT" ||
    target.isContentEditable
  );
}

function isCategoryKey(key: string): key is CategoryKey {
  return (CATEGORY_KEYS as readonly string[]).includes(key);
}

function isOptionKey(key: string): key is OptionKey {
  return (OPTION_KEYS as readonly string[]).includes(key);
}

function isToneKey(key: string): key is ToneKey {
  return (TONE_KEYS as readonly string[]).includes(key);
}

export default function App() {
  const [joined, setJoined] = useState<JoinedSession | null>(null);
  const [tree, setTree] = useState<DecisionTree>({});
  const [treeError, setTreeError] = useState<string | null>(null);
  const [selection, setSelection] = useState<StagedSelection>(EMPTY_SELECTION);
  const [pauseOpen, setPauseOpen] = useState(false);
  const [uncoveredOpen, setUncoveredOpen] = useState(false);
  // Local reference point for `uncovered_question.timestamp_ms`. The snapshot
  // only carries `start_at_server_ms` while the session is `starting`, so once
  // it is running there is no server-supplied origin to subtract from.
  const [activeSinceMs, setActiveSinceMs] = useState<number | null>(null);

  const socket = useSessionSocket(joined?.sessionId ?? null);
  const { session, status } = socket;

  // 載入案件決策樹（cases/<case>/wizard_decision_tree.json）。
  useEffect(() => {
    if (joined === null || !joined.caseId) {
      return;
    }
    let cancelled = false;
    fetchDecisionTree(joined.caseId)
      .then((loaded) => {
        if (!cancelled) {
          setTree(loaded);
          setTreeError(null);
        }
      })
      .catch((cause: unknown) => {
        if (!cancelled) {
          setTreeError(cause instanceof ApiError ? cause.message : String(cause));
        }
      });
    return () => {
      cancelled = true;
    };
  }, [joined]);

  useEffect(() => {
    if (session.session_state === "active") {
      setActiveSinceMs((current) => current ?? Date.now());
    } else if (session.session_state === "ended") {
      setActiveSinceMs(null);
    }
  }, [session.session_state]);

  const connected = status === "open" || status === "degraded";
  const paused = session.session_state === "paused";
  const ended = session.session_state === "ended";
  /** Playback commands are only meaningful on a live, unpaused session. */
  const canCommand = connected && !paused && !ended;

  const resolvedClipId = useMemo(
    () => resolveClipId(tree, selection.category, selection.option, selection.tone),
    [selection.category, selection.option, selection.tone, tree],
  );

  const confirmStaged = useCallback(() => {
    if (!canCommand || resolvedClipId === null) {
      return;
    }
    socket.sendClipCommand(resolvedClipId);
    setSelection(EMPTY_SELECTION);
  }, [canCommand, resolvedClipId, socket]);

  const playThinking = useCallback(() => {
    const clipId = findThinkingClipId(tree);
    if (clipId === null || !canCommand) {
      return;
    }
    socket.sendClipCommand(clipId);
  }, [canCommand, socket, tree]);

  const submitUncovered = useCallback(
    (payload: UncoveredQuestionPayload) => {
      socket.sendUncoveredQuestion(payload);
      setUncoveredOpen(false);
    },
    [socket],
  );

  const submitPause = useCallback(
    (reason: Parameters<typeof socket.sendSessionPaused>[0]) => {
      socket.sendSessionPaused(reason);
      setPauseOpen(false);
    },
    [socket],
  );

  // 全域快捷鍵（spec §8）。輸入框聚焦時一律不攔截。
  useEffect(() => {
    if (joined === null) {
      return;
    }

    function onKeyDown(event: KeyboardEvent) {
      if (event.ctrlKey || event.altKey || event.metaKey) {
        return;
      }
      if (isTypingTarget(event.target)) {
        return;
      }

      // While a modal is up only Escape is global; everything else belongs to
      // the dialog so the Wizard cannot fire a clip from behind it.
      if (pauseOpen || uncoveredOpen) {
        if (event.key === "Escape") {
          event.preventDefault();
          setPauseOpen(false);
          setUncoveredOpen(false);
        }
        return;
      }

      const key = event.key;

      if (isCategoryKey(key)) {
        // Tier 1 replaces the whole staged path: the old option/tone belonged
        // to a different category.
        event.preventDefault();
        setSelection({ category: key, option: null, tone: null });
        return;
      }

      if (isOptionKey(key)) {
        event.preventDefault();
        setSelection((current) => ({ ...current, option: key, tone: null }));
        return;
      }

      if (key === " ") {
        event.preventDefault();
        confirmStaged();
        return;
      }

      if (key === "Escape") {
        event.preventDefault();
        if (session.playback_state === "playing" && canCommand) {
          socket.sendReturnToIdle();
        }
        setSelection(EMPTY_SELECTION);
        return;
      }

      const upper = key.toUpperCase();

      // Spec §8 gives N/T/D/I/R to tier 3 *and* T/I to playback. Resolve by
      // context: once an answer option is staged the keys mean tone, otherwise
      // they mean playback. Documented in HotkeyCheatsheet.
      if (isToneKey(upper) && selection.option !== null) {
        event.preventDefault();
        setSelection((current) => ({ ...current, tone: upper }));
        return;
      }

      switch (upper) {
        case "I":
          event.preventDefault();
          if (canCommand) {
            socket.sendReturnToIdle();
            setSelection(EMPTY_SELECTION);
          }
          return;
        case "T":
          event.preventDefault();
          playThinking();
          return;
        case "B":
          event.preventDefault();
          if (canCommand) {
            // spec §7.2: the student talked over the suspect.
            socket.sendClipInterrupted("student_barge_in");
          }
          return;
        case "F":
          event.preventDefault();
          if (canCommand) {
            socket.sendRequestFallback("CLARIFY");
          }
          return;
        case "M":
          event.preventDefault();
          setUncoveredOpen(true);
          return;
        case "P":
          event.preventDefault();
          setPauseOpen(true);
          return;
        default:
          break;
      }
    }

    window.addEventListener("keydown", onKeyDown);
    return () => window.removeEventListener("keydown", onKeyDown);
  }, [
    canCommand,
    confirmStaged,
    joined,
    pauseOpen,
    playThinking,
    selection.option,
    session.playback_state,
    socket,
    uncoveredOpen,
  ]);

  if (joined === null) {
    return <SessionJoinModal onJoined={setJoined} />;
  }

  return (
    <div
      style={{
        fontFamily: "system-ui",
        background: "#101216",
        color: "#e8e8ea",
        minHeight: "100vh",
      }}
    >
      <SessionStatusHeader
        sessionId={joined.sessionId}
        caseId={joined.caseId}
        status={status}
        closedReason={socket.closedReason}
        session={session}
        clock={socket.clock}
        pendingCount={socket.pendingCount}
      />

      {paused && (
        <div
          role="alert"
          style={{
            padding: "10px 16px",
            background: "#3a2f14",
            borderBottom: "1px solid #7a6420",
            display: "flex",
            alignItems: "center",
            gap: 12,
          }}
        >
          <strong>Session 已暫停</strong>
          <span style={{ fontSize: 13, color: "#d8cfa8" }}>
            暫停期間無法送出回答或 fallback。
          </span>
          <button
            type="button"
            onClick={socket.sendSessionResumed}
            disabled={!connected}
            style={{ padding: "6px 14px", fontFamily: "inherit" }}
          >
            恢復 Session
          </button>
        </div>
      )}

      {treeError !== null && (
        <div
          role="alert"
          style={{ padding: "8px 16px", background: "#4a2020", fontSize: 13 }}
        >
          決策樹載入失敗：{treeError}
        </div>
      )}

      <main
        style={{
          display: "grid",
          gridTemplateColumns: "minmax(0, 2fr) minmax(280px, 1fr)",
          gap: 12,
          padding: 12,
        }}
      >
        <div>
          <DecisionTreePanel
            tree={tree}
            selection={selection}
            resolvedClipId={resolvedClipId}
            disabled={!canCommand}
            onSelectCategory={(key) =>
              setSelection({ category: key, option: null, tone: null })
            }
            onSelectOption={(key) =>
              setSelection((current) => ({ ...current, option: key, tone: null }))
            }
            onSelectTone={(key) =>
              setSelection((current) => ({ ...current, tone: key }))
            }
            onConfirm={confirmStaged}
          />
        </div>

        <aside>
          <FallbackPanel
            consecutiveFallbacks={session.consecutive_fallbacks}
            thresholdAlert={socket.thresholdAlert}
            onDismissAlert={socket.dismissThresholdAlert}
            disabled={!canCommand}
            onRequestFallback={socket.sendRequestFallback}
          />

          <section
            style={{
              background: "#1b1e24",
              border: "1px solid #2c313a",
              borderRadius: 8,
              padding: 12,
              marginBottom: 12,
            }}
          >
            <h2 style={{ fontSize: 13, margin: "0 0 8px", color: "#9aa0ab" }}>
              事件紀錄 Activity
            </h2>
            {socket.lastAck !== null && (
              <div style={{ fontSize: 11, color: "#9aa0ab", marginBottom: 6 }}>
                最後 ACK：{socket.lastAck.status}
                {socket.lastAck.reason !== null && ` · ${socket.lastAck.reason}`}
              </div>
            )}
            <ul
              style={{
                listStyle: "none",
                margin: 0,
                padding: 0,
                maxHeight: 220,
                overflowY: "auto",
                fontSize: 11,
                lineHeight: 1.7,
              }}
            >
              {socket.log.map((entry) => (
                <li
                  key={entry.id}
                  style={{
                    color:
                      entry.level === "error"
                        ? "#ff8080"
                        : entry.level === "warn"
                          ? "#e0b341"
                          : "#c3c7ce",
                  }}
                >
                  {new Date(entry.at_ms).toLocaleTimeString()} {entry.text}
                </li>
              ))}
            </ul>
          </section>

          <HotkeyCheatsheet />
        </aside>
      </main>

      <PauseResumeModal
        open={pauseOpen}
        onClose={() => setPauseOpen(false)}
        onPause={submitPause}
      />
      <UncoveredQuestionModal
        open={uncoveredOpen}
        onClose={() => setUncoveredOpen(false)}
        onSubmit={submitUncovered}
        sessionElapsedMs={activeSinceMs === null ? null : Date.now() - activeSinceMs}
      />
    </div>
  );
}
