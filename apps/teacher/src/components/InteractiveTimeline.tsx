import { useState } from "react";

import type { ClipTimelineEntry, RecordingSpan, TeacherMarker, TimelineEntry, TimelineResponse, WizardActionEntry } from "../types";

interface InteractiveTimelineProps {
  currentTimeMs: number;
  onSeek: (tMs: number) => void;
  timeline: TimelineResponse | null;
  teacherMarkers: TeacherMarker[];
  onAddTeacherMarker: (note: string) => void;
  onRemoveTeacherMarker: (id: string) => void;
}

function formatTime(ms: number | null): string {
  if (ms === null || !Number.isFinite(ms)) {
    return "--:--";
  }
  const sign = ms < 0 ? "-" : "";
  const absoluteSeconds = Math.floor(Math.abs(ms) / 1000);
  const minutes = Math.floor(absoluteSeconds / 60);
  const seconds = absoluteSeconds % 60;
  return `${sign}${minutes}:${seconds.toString().padStart(2, "0")}`;
}

function entryTime(entry: TimelineEntry): number {
  return Math.max(0, entry.t_ms ?? 0);
}

function spanStart(span: RecordingSpan): number {
  return Math.max(0, span.start_ms ?? 0);
}

function spanEnd(span: RecordingSpan): number {
  return Math.max(spanStart(span) + 250, span.end_ms ?? spanStart(span) + 250);
}

function labelForAction(entry: WizardActionEntry): string {
  if (entry.action === "fallback_used") {
    return `Fallback ${entry.fallback_category ?? ""}`.trim();
  }
  if (entry.action === "uncovered_question") {
    return "Uncovered question";
  }
  if (entry.action === "session_paused") {
    return `Paused ${entry.reason ?? ""}`.trim();
  }
  return entry.action.replaceAll("_", " ");
}

function timelineDuration(timeline: TimelineResponse): number {
  const entryMax = timeline.entries.reduce((max, entry) => {
    if (entry.kind === "clip") {
      return Math.max(max, entryTime(entry), Math.max(0, entry.end_t_ms ?? 0));
    }
    return Math.max(max, entryTime(entry));
  }, 0);
  const spanMax = timeline.recording_spans.reduce((max, span) => Math.max(max, spanEnd(span)), 0);
  return Math.max(entryMax, spanMax, 1000);
}

export function InteractiveTimeline({ currentTimeMs, onAddTeacherMarker, onRemoveTeacherMarker, onSeek, teacherMarkers, timeline }: InteractiveTimelineProps) {
  const [markerNote, setMarkerNote] = useState("");
  if (!timeline) {
    return (
      <section className="timeline panel">
        <div className="panel-heading">
          <div>
            <p className="eyebrow">Timeline</p>
            <h2>Unified replay</h2>
          </div>
        </div>
        <p className="muted">Load a review session to render clip spans, Wizard actions, sync events, and recording coverage.</p>
      </section>
    );
  }

  const duration = timelineDuration(timeline);
  const clips = timeline.entries.filter((entry): entry is ClipTimelineEntry => entry.kind === "clip");
  const actions = timeline.entries.filter((entry): entry is WizardActionEntry => entry.kind === "wizard_action");
  const syncAndLifecycle = timeline.entries.filter((entry) => entry.kind === "sync" || entry.kind === "lifecycle");
  const ticks = Array.from({ length: 6 }, (_, index) => Math.round((duration / 5) * index));
  const cursorLeft = `${Math.min(100, Math.max(0, (currentTimeMs / duration) * 100))}%`;

  return (
    <section className="timeline panel">
      <div className="panel-heading">
        <div>
          <p className="eyebrow">Timeline</p>
          <h2>Unified replay</h2>
        </div>
        <span className="time-chip">{formatTime(currentTimeMs)}</span>
      </div>

      <form className="marker-form" onSubmit={(event) => {
        event.preventDefault();
        const note = markerNote.trim();
        if (note) {
          onAddTeacherMarker(note);
          setMarkerNote("");
        }
      }}>
        <input aria-label="Teacher marker note" onChange={(event) => setMarkerNote(event.target.value)} placeholder="Add a teacher marker at the current time" value={markerNote} />
        <button className="primary-button" disabled={!markerNote.trim()} type="submit">Add marker</button>
      </form>

      <div
        className="timeline-canvas"
        onClick={(event) => {
          const rect = event.currentTarget.getBoundingClientRect();
          const ratio = (event.clientX - rect.left) / rect.width;
          onSeek(Math.round(Math.max(0, Math.min(1, ratio)) * duration));
        }}
        role="presentation"
      >
        <div className="timeline-cursor" style={{ left: cursorLeft }} />

        <div className="ruler timeline-track">
          {ticks.map((tick) => (
            <span key={tick} style={{ left: `${(tick / duration) * 100}%` }}>
              {formatTime(tick)}
            </span>
          ))}
        </div>

        <div className="timeline-track clip-track" aria-label="Suspect clip spans">
          {clips.map((clip) => {
            const start = entryTime(clip);
            const manifestDurationMs = clip.clip?.manifest_duration_ms ?? clip.clip?.duration_ms ?? 1000;
            const end = Math.max(start + 250, clip.end_t_ms ?? start + manifestDurationMs);
            return (
              <button
                className="clip-span"
                key={`${clip.clip_id ?? "clip"}-${clip.sequence}-${start}`}
                onClick={(event) => {
                  event.stopPropagation();
                  onSeek(start);
                }}
                style={{
                  left: `${(start / duration) * 100}%`,
                  width: `${Math.max(1.5, ((end - start) / duration) * 100)}%`,
                }}
                title={clip.clip?.text ?? clip.clip_id ?? "Clip span"}
                type="button"
              >
                <span>{clip.clip_id ?? "clip"}</span>
              </button>
            );
          })}
        </div>

        <div className="timeline-track action-track" aria-label="Wizard markers">
          {actions.map((action) => {
            const start = entryTime(action);
            return (
              <button
                className={`event-marker ${action.action}`}
                key={`${action.action}-${action.sequence}-${start}`}
                onClick={(event) => {
                  event.stopPropagation();
                  onSeek(start);
                }}
                style={{ left: `${(start / duration) * 100}%` }}
                title={[labelForAction(action), action.question_text, action.note].filter(Boolean).join(" - ")}
                type="button"
              >
                {labelForAction(action)}
              </button>
            );
          })}
        </div>

        <div className="timeline-track sync-track" aria-label="Sync and lifecycle markers">
          {syncAndLifecycle.map((entry) => {
            const start = entryTime(entry);
            return (
              <button
                className={`thin-marker ${entry.kind}`}
                key={`${entry.event_type}-${entry.sequence}-${start}`}
                onClick={(event) => {
                  event.stopPropagation();
                  onSeek(start);
                }}
                style={{ left: `${(start / duration) * 100}%` }}
                title={entry.event_type}
                type="button"
              />
            );
          })}
        </div>

        <div className="timeline-track teacher-track" aria-label="Teacher markers">
          {teacherMarkers.map((marker) => (
            <button className="event-marker teacher-marker" key={marker.id} onClick={(event) => { event.stopPropagation(); onSeek(marker.t_ms); }} onContextMenu={(event) => { event.preventDefault(); event.stopPropagation(); onRemoveTeacherMarker(marker.id); }} style={{ left: `${(marker.t_ms / duration) * 100}%` }} title={`${marker.note} (right-click to remove)`} type="button">{marker.note}</button>
          ))}
        </div>

        <div className="timeline-track recording-track" aria-label="Recording spans">
          {timeline.recording_spans.map((span) => {
            const start = spanStart(span);
            const end = spanEnd(span);
            return (
              <button
                className="recording-span"
                key={`${span.first_sequence}-${span.last_sequence}`}
                onClick={(event) => {
                  event.stopPropagation();
                  onSeek(start);
                }}
                style={{
                  left: `${(start / duration) * 100}%`,
                  width: `${Math.max(1.5, ((end - start) / duration) * 100)}%`,
                }}
                title={`Chunks ${span.first_sequence}-${span.last_sequence}, ${span.chunk_count} total`}
                type="button"
              />
            );
          })}
        </div>
      </div>
    </section>
  );
}
