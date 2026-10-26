import { useCallback, useEffect, useMemo, useState } from "react";

import {
  clearReviewSession,
  readReviewSession,
  rememberAccessToken,
  subscribeReviewSessionDrop,
} from "@mind-probe/shared";
import { fetchAuditLogs, fetchClipManifest, fetchRecordingStatus, fetchRubric, fetchSession, fetchTimeline, joinSession, reviewLogin, reviewLogout } from "./api";
import "./App.css";
import { DualVideoPlayer } from "./components/DualVideoPlayer";
import { InteractiveTimeline } from "./components/InteractiveTimeline";
import { RubricScoringPanel } from "./components/RubricScoringPanel";
import { SessionJoinBar, type SessionLoadRequest } from "./components/SessionJoinBar";
import { reviewErrorMessage, ReviewRequestError, shouldReturnToReviewLogin } from "./reviewErrors";
import { readAuthMode, rememberAuthMode, type SessionAuthMode } from "./sessionAuthMode";
import type { AuthContext, AuditResponse, ClipManifestEntry, RecordingStatus, RubricData, SessionInfo, TeacherMarker, TimelineResponse } from "./types";

function errorText(error: unknown): string {
  if (error instanceof Error && error.message) {
    return error.message;
  }
  return "Unable to load review session.";
}

function App() {
  const boot = useState(() => {
    const review = readReviewSession();
    if (review) {
      rememberAuthMode("review_code");
      return { authMode: "review_code" as SessionAuthMode, sessionId: review.sessionId, signedIn: true };
    }
    return { authMode: readAuthMode(), sessionId: "", signedIn: false };
  })[0];
  const [authMode, setAuthMode] = useState<SessionAuthMode>(boot.authMode);
  const [auth, setAuth] = useState<AuthContext>({ token: "" });
  const [session, setSession] = useState<SessionInfo | null>(null);
  const [timeline, setTimeline] = useState<TimelineResponse | null>(null);
  const [recordingStatus, setRecordingStatus] = useState<RecordingStatus | null>(null);
  const [rubric, setRubric] = useState<RubricData | null>(null);
  const [audit, setAudit] = useState<AuditResponse | null>(null);
  const [clips, setClips] = useState<ClipManifestEntry[]>([]);
  const [teacherMarkers, setTeacherMarkers] = useState<TeacherMarker[]>([]);
  const [currentTimeMs, setCurrentTimeMs] = useState(0);
  const [seekTarget, setSeekTarget] = useState<{ tMs: number; nonce: number } | null>(null);
  const [isLoading, setIsLoading] = useState(false);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [reviewSignedIn, setReviewSignedIn] = useState(boot.signedIn);
  const [logoutPending, setLogoutPending] = useState(false);
  const [formNonce, setFormNonce] = useState(0);

  const caseId = timeline?.case_id ?? session?.case_id ?? rubric?.case_id ?? "";

  const summaryItems = useMemo(() => {
    if (!timeline) {
      return [];
    }
    return [
      ["Events", String(timeline.summary.entry_count)],
      ["Clips", String(timeline.summary.clip_count)],
      ["Recording spans", String(timeline.summary.recording_span_count)],
      ["Origin", timeline.t0_source],
    ];
  }, [timeline]);

  const applyLoaded = useCallback(async (sessionId: string, token: string) => {
    const nextAuth: AuthContext = { token };
    const loadedSession = await fetchSession(sessionId, nextAuth);
    const loadedTimeline = await fetchTimeline(sessionId, nextAuth);
    const loadedStatus = await fetchRecordingStatus(sessionId, nextAuth);
    const loadedRubric = await fetchRubric(loadedTimeline.case_id || loadedSession.case_id, nextAuth);
    const loadedClips = await fetchClipManifest(loadedTimeline.case_id || loadedSession.case_id, nextAuth);
    const loadedAudit = await fetchAuditLogs(sessionId, nextAuth);
    const review = readReviewSession();
    const accessToken = review && review.sessionId === sessionId ? review.accessToken : token;
    rememberAccessToken(sessionId, accessToken);

    setAuth({ token: accessToken });
    setSession(loadedSession);
    setTimeline(loadedTimeline);
    setRecordingStatus(loadedStatus);
    setRubric(loadedRubric);
    setClips(loadedClips);
    setAudit(loadedAudit);
    setCurrentTimeMs(0);
    setSeekTarget({ tMs: 0, nonce: Date.now() });
    const storedMarkers = localStorage.getItem(`mind-probe:teacher-markers:${sessionId}`);
    try {
      const parsed: unknown = storedMarkers ? JSON.parse(storedMarkers) : [];
      setTeacherMarkers(Array.isArray(parsed) ? parsed as TeacherMarker[] : []);
    } catch {
      setTeacherMarkers([]);
    }
  }, []);

  const loadSession = useCallback(async (request: SessionLoadRequest) => {
    setIsLoading(true);
    setLoadError(null);
    setSession(null);
    setTimeline(null);
    setRecordingStatus(null);
    setRubric(null);
    setAudit(null);
    setClips([]);
    setTeacherMarkers([]);

    try {
      let token = "";
      if (request.authMode === "join_code") {
        clearReviewSession();
        const joined = await joinSession(request.sessionId, request.credential, request.clientId);
        if (joined.role !== "teacher" && joined.role !== "observer") {
          throw new Error(`Join code opened role '${joined.role}', not a review role.`);
        }
        token = joined.access_token;
      } else if (request.authMode === "review_code") {
        const logged = await reviewLogin(request.sessionId, request.credential, request.clientId);
        token = logged.access_token;
      } else {
        clearReviewSession();
        token = request.credential;
      }
      if (!token) {
        throw new Error("A Bearer token is required to load the review.");
      }
      await applyLoaded(request.sessionId, token);
      setReviewSignedIn(readReviewSession()?.sessionId === request.sessionId);
    } catch (error) {
      if (
        request.authMode === "review_code" ||
        (error instanceof ReviewRequestError && shouldReturnToReviewLogin(error.detail))
      ) {
        setAuthMode("review_code");
        rememberAuthMode("review_code");
      }
      if (readReviewSession() === null) {
        setReviewSignedIn(false);
      }
      setLoadError(errorText(error));
    } finally {
      setIsLoading(false);
    }
  }, [applyLoaded]);

  const chooseAuthMode = useCallback((mode: SessionAuthMode) => {
    setAuthMode(mode);
    rememberAuthMode(mode);
  }, []);

  const abandonReview = useCallback((message: string | null) => {
    setAuth({ token: "" });
    setSession(null);
    setTimeline(null);
    setRecordingStatus(null);
    setRubric(null);
    setAudit(null);
    setClips([]);
    setTeacherMarkers([]);
    setCurrentTimeMs(0);
    setSeekTarget(null);
    setIsLoading(false);
    setReviewSignedIn(false);
    setAuthMode("review_code");
    rememberAuthMode("review_code");
    setLoadError(message);
    setFormNonce((nonce) => nonce + 1);
  }, []);

  useEffect(() => {
    return subscribeReviewSessionDrop((detail) => {
      abandonReview(reviewErrorMessage(detail));
    });
  }, [abandonReview]);

  const logout = useCallback(async () => {
    const review = readReviewSession();
    const sessionId = review?.sessionId || session?.session_id || "";
    const token = review?.accessToken || auth.token;
    setLogoutPending(true);
    setLoadError(null);
    try {
      if (review && sessionId && token) {
        await reviewLogout(sessionId, { token });
      } else {
        clearReviewSession();
      }
      abandonReview(null);
    } catch (error) {
      if (readReviewSession() === null) {
        abandonReview(null);
        return;
      }
      setLoadError(errorText(error));
    } finally {
      setLogoutPending(false);
    }
  }, [abandonReview, auth.token, session]);

  useEffect(() => {
    const stored = readReviewSession();
    if (!stored) {
      return;
    }
    let cancelled = false;
    setIsLoading(true);
    setLoadError(null);
    applyLoaded(stored.sessionId, stored.accessToken)
      .then(() => {
        if (!cancelled) {
          setReviewSignedIn(readReviewSession()?.sessionId === stored.sessionId);
        }
      })
      .catch((error: unknown) => {
        if (cancelled) {
          return;
        }
        if (
          readReviewSession() === null ||
          (error instanceof ReviewRequestError && shouldReturnToReviewLogin(error.detail))
        ) {
          setAuthMode("review_code");
          rememberAuthMode("review_code");
          setReviewSignedIn(false);
        }
        setLoadError(errorText(error));
      })
      .finally(() => {
        if (!cancelled) {
          setIsLoading(false);
        }
      });
    return () => {
      cancelled = true;
    };
  }, [applyLoaded]);

  const seekTo = useCallback((tMs: number) => {
    setSeekTarget({ tMs: Math.max(0, tMs), nonce: Date.now() });
  }, []);

  const saveMarkers = useCallback((sessionId: string, markers: TeacherMarker[]) => {
    setTeacherMarkers(markers);
    localStorage.setItem(`mind-probe:teacher-markers:${sessionId}`, JSON.stringify(markers));
  }, []);

  return (
    <main className="app-shell">
      <SessionJoinBar
        authMode={authMode}
        formNonce={formNonce}
        initialSessionId={boot.sessionId}
        isLoading={isLoading}
        logoutPending={logoutPending}
        onAuthModeChange={chooseAuthMode}
        onLoadSession={loadSession}
        onLogout={() => void logout()}
        recordingStatus={recordingStatus}
        session={session}
        showLogout={reviewSignedIn}
      />

      {loadError ? <div className="alert alert-error">{loadError}</div> : null}

      <section className="review-grid" aria-label="Teacher review workspace">
        <div className="review-main">
          <DualVideoPlayer
            auth={auth}
            caseId={caseId}
            clips={clips}
            onTimeChange={setCurrentTimeMs}
            seekTarget={seekTarget}
            sessionId={session?.session_id ?? null}
            timeline={timeline}
          />

          <InteractiveTimeline
            currentTimeMs={currentTimeMs}
            teacherMarkers={teacherMarkers}
            onAddTeacherMarker={(note) => {
              if (!session) return;
              saveMarkers(session.session_id, [...teacherMarkers, { id: crypto.randomUUID(), t_ms: currentTimeMs, note, created_at: new Date().toISOString() }]);
            }}
            onRemoveTeacherMarker={(id) => {
              if (!session) return;
              saveMarkers(session.session_id, teacherMarkers.filter((marker) => marker.id !== id));
            }}
            onSeek={seekTo}
            timeline={timeline}
          />
        </div>

        <aside className="review-side" aria-label="Session scoring and metadata">
          <section className="panel compact-panel">
            <div className="panel-heading">
              <div>
                <p className="eyebrow">Session</p>
                <h2>{session?.session_id ?? "No session loaded"}</h2>
              </div>
              <span className={`status-pill ${session?.status ?? "idle"}`}>
                {session?.status ?? "idle"}
              </span>
            </div>

            <dl className="summary-list">
              <div>
                <dt>Case</dt>
                <dd>{caseId || "-"}</dd>
              </div>
              <div>
                <dt>Revision</dt>
                <dd>{session?.snapshot_revision ?? "-"}</dd>
              </div>
              {summaryItems.map(([label, value]) => (
                <div key={label}>
                  <dt>{label}</dt>
                  <dd>{value}</dd>
                </div>
              ))}
              <div>
                <dt>Recording</dt>
                <dd>
                  {recordingStatus
                    ? `${recordingStatus.chunk_count} chunks, ${recordingStatus.missing_sequences.length} gaps`
                    : "-"}
                </dd>
              </div>
            </dl>
          </section>

          <RubricScoringPanel caseId={caseId} rubric={rubric} sessionId={session?.session_id ?? ""} teacherMarkers={teacherMarkers} />

          <section className="panel compact-panel">
            <div className="panel-heading">
              <div>
                <p className="eyebrow">Audit</p>
                <h2>Access log</h2>
              </div>
              <span className="count-badge">{audit?.entries.length ?? 0}</span>
            </div>
            <div className="audit-list">
              {session && audit === null ? (
                <p className="muted">Audit is available to a teacher review login or an operator.</p>
              ) : audit && audit.entries.length > 0 ? (
                audit.entries.slice(-5).map((entry) => (
                  <article key={entry.id} className="audit-row">
                    <strong>{entry.action}</strong>
                    <span>{entry.actor_role ?? "unknown"}</span>
                    <time>{entry.created_at ? new Date(entry.created_at).toLocaleString() : "-"}</time>
                  </article>
                ))
              ) : (
                <p className="muted">Load a session to inspect audit activity.</p>
              )}
            </div>
          </section>
        </aside>
      </section>
    </main>
  );
}

export default App;
