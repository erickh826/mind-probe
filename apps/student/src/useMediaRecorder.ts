import { useCallback, useEffect, useState } from "react";
import {
  bindSession,
  errorIsRetryable,
  recordingView,
  retryFailed,
  scheduleRelease,
  setChunkUploadedListener,
  startRecording,
  stopRecording,
  subscribe,
  type RecordingView,
} from "./recordingController";
import type { RecorderStatus } from "./types";
import type { CloseBanner } from "./uploadQueue";

interface UseMediaRecorderOptions {
  sessionId: string | null;
  onChunkUploaded: (chunkIndex: number, timestampMs: number) => void;
}

export interface StudentRecorder {
  status: RecorderStatus;
  stream: MediaStream | null;
  error: string | null;
  uploadedCount: number;
  failedCount: number;
  pendingCount: number;
  banner: CloseBanner;
  storageWarning: string | null;
  canStart: boolean;
  start: () => Promise<void>;
  stop: () => void;
  retryFailed: () => void;
}

export function useMediaRecorder({
  sessionId,
  onChunkUploaded,
}: UseMediaRecorderOptions): StudentRecorder {
  const [view, setView] = useState<RecordingView>(recordingView);

  useEffect(() => subscribe(setView), []);

  useEffect(() => {
    setChunkUploadedListener(onChunkUploaded);
    return () => setChunkUploadedListener(null);
  }, [onChunkUploaded]);

  useEffect(() => {
    if (sessionId === null) {
      return;
    }
    void bindSession(sessionId);
    return () => scheduleRelease();
  }, [sessionId]);

  useEffect(() => {
    if (sessionId === null || view.status !== "error" || !errorIsRetryable()) {
      return;
    }
    const retry = () => void startRecording(sessionId);
    navigator.mediaDevices?.addEventListener("devicechange", retry);
    const timer = window.setInterval(retry, 5_000);
    return () => {
      navigator.mediaDevices?.removeEventListener("devicechange", retry);
      window.clearInterval(timer);
    };
  }, [sessionId, view.status]);

  const start = useCallback(async () => {
    if (sessionId === null) {
      return;
    }
    await startRecording(sessionId);
  }, [sessionId]);

  return {
    status: view.status,
    stream: view.stream,
    error: view.error,
    uploadedCount: view.uploadedCount,
    failedCount: view.failedCount,
    pendingCount: view.pendingCount,
    banner: view.banner,
    storageWarning: view.storageWarning,
    canStart: view.canStart,
    start,
    stop: stopRecording,
    retryFailed: () => {
      void retryFailed();
    },
  };
}
