import { useEffect, useMemo, useRef, useState } from "react";

import { fetchRecordingBlob } from "../api";
import { mediaUrl } from "../config";
import type { AuthContext, ClipManifestEntry, ClipTimelineEntry, TimelineResponse } from "../types";

interface DualVideoPlayerProps {
  auth: AuthContext;
  caseId: string;
  onTimeChange: (timeMs: number) => void;
  seekTarget: { tMs: number; nonce: number } | null;
  sessionId: string | null;
  timeline: TimelineResponse | null;
  clips: ClipManifestEntry[];
}

function clipEndMs(entry: ClipTimelineEntry): number {
  if (entry.end_t_ms !== null) {
    return entry.end_t_ms;
  }
  const manifestDurationMs = entry.clip?.manifest_duration_ms ?? entry.clip?.duration_ms;
  if (entry.t_ms !== null && manifestDurationMs) {
    return entry.t_ms + manifestDurationMs;
  }
  return entry.t_ms ?? 0;
}

function findActiveClip(clips: ClipTimelineEntry[], timeMs: number): ClipTimelineEntry | null {
  return (
    clips.find((entry) => {
      if (entry.t_ms === null || !entry.clip?.file) {
        return false;
      }
      return timeMs >= entry.t_ms && timeMs <= clipEndMs(entry);
    }) ?? null
  );
}

export function DualVideoPlayer({ auth, caseId, clips, onTimeChange, seekTarget, sessionId, timeline }: DualVideoPlayerProps) {
  const studentRef = useRef<HTMLVideoElement | null>(null);
  const suspectRef = useRef<HTMLVideoElement | null>(null);
  const suspectSrcRef = useRef<string>("");
  const [recordingSrc, setRecordingSrc] = useState<string>("");
  const [activeClip, setActiveClip] = useState<ClipTimelineEntry | null>(null);
  const [videoError, setVideoError] = useState<string | null>(null);

  const clipEntries = useMemo(
    () => timeline?.entries.filter((entry): entry is ClipTimelineEntry => entry.kind === "clip") ?? [],
    [timeline],
  );
  const idleClip = useMemo(
    () => clips.find((clip) => clip.category?.toLowerCase() === "idle" || clip.intent?.toLowerCase().includes("idle")) ?? null,
    [clips],
  );

  useEffect(() => {
    setVideoError(null);
    setRecordingSrc("");
    if (!sessionId) {
      return undefined;
    }

    const controller = new AbortController();
    let objectUrl = "";
    fetchRecordingBlob(sessionId, auth, controller.signal)
      .then((blob) => {
        objectUrl = URL.createObjectURL(blob);
        setRecordingSrc(objectUrl);
      })
      .catch((error) => {
        if (!controller.signal.aborted) {
          setVideoError(error instanceof Error ? error.message : "Recording could not be loaded.");
        }
      });

    return () => {
      controller.abort();
      if (objectUrl) {
        URL.revokeObjectURL(objectUrl);
      }
    };
  }, [auth, sessionId]);

  useEffect(() => {
    // Initialize suspect video with idle clip or first frame on load
    if (caseId && idleClip?.file && suspectRef.current && !suspectSrcRef.current) {
      const src = mediaUrl(caseId, idleClip.file);
      suspectRef.current.src = src;
      suspectSrcRef.current = src;
      suspectRef.current.loop = true;
    }
  }, [caseId, idleClip]);

  useEffect(() => {
    if (!seekTarget || !studentRef.current) {
      return;
    }
    studentRef.current.currentTime = seekTarget.tMs / 1000;
    onTimeChange(seekTarget.tMs);
  }, [onTimeChange, seekTarget]);

  function syncSuspect(timeMs: number) {
    const suspect = suspectRef.current;
    const student = studentRef.current;
    if (!suspect || !student || !caseId) {
      return;
    }

    const nextClip = findActiveClip(clipEntries, timeMs);
    setActiveClip(nextClip);

    const selectedFile = nextClip?.clip?.file ?? idleClip?.file;
    if (!selectedFile) {
      suspect.pause();
      suspect.removeAttribute("src");
      suspectSrcRef.current = "";
      return;
    }

    const nextSrc = mediaUrl(caseId, selectedFile);
    if (suspectSrcRef.current !== nextSrc) {
      suspect.src = nextSrc;
      suspectSrcRef.current = nextSrc;
    }

    suspect.loop = nextClip === null;
    suspect.playbackRate = student.playbackRate;

    // Idle is ambient footage rather than part of the session clock. Let it loop
    // continuously; only response clips have a meaningful span-relative offset.
    if (nextClip?.t_ms != null) {
      const targetSeconds = Math.max(0, (timeMs - nextClip.t_ms) / 1000);
      if (Number.isFinite(targetSeconds) && Math.abs(suspect.currentTime - targetSeconds) > 0.35) {
        suspect.currentTime = targetSeconds;
      }
    }

    if (student.paused) {
      suspect.pause();
    } else {
      void suspect.play().catch(() => undefined);
    }
  }

  function handleTimeUpdate() {
    const timeMs = Math.round((studentRef.current?.currentTime ?? 0) * 1000);
    onTimeChange(timeMs);
    syncSuspect(timeMs);
  }

  function handlePlay() {
    syncSuspect(Math.round((studentRef.current?.currentTime ?? 0) * 1000));
  }

  function handlePause() {
    suspectRef.current?.pause();
  }

  function handleRateChange() {
    syncSuspect(Math.round((studentRef.current?.currentTime ?? 0) * 1000));
  }

  return (
    <section className="dual-player panel" aria-label="Dual synchronized replay">
      <div className="player-card">
        <div className="player-caption">
          <div>
            <p className="eyebrow">Master clock</p>
            <h2>Student recording</h2>
          </div>
          <span>{sessionId ?? "No session"}</span>
        </div>
        <video
          className="review-video"
          controls
          crossOrigin="use-credentials"
          onPause={handlePause}
          onPlay={handlePlay}
          onRateChange={handleRateChange}
          onSeeked={handleTimeUpdate}
          onTimeUpdate={handleTimeUpdate}
          ref={studentRef}
          src={recordingSrc}
        />
        {videoError ? <p className="video-message">{videoError}</p> : null}
      </div>

      <div className="player-card">
        <div className="player-caption">
          <div>
            <p className="eyebrow">Synchronized view</p>
            <h2>Suspect clips</h2>
          </div>
          <span>{activeClip?.clip_id ?? "Idle"}</span>
        </div>
        <video className="review-video" muted playsInline ref={suspectRef} />
        <div className="clip-readout">
          <strong>{activeClip?.clip?.intent ?? idleClip?.intent ?? "Idle unavailable"}</strong>
          <span>{activeClip?.clip?.text ?? idleClip?.text ?? "This case manifest does not provide an Idle clip."}</span>
        </div>
      </div>
    </section>
  );
}
