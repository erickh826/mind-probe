import { useCallback, useEffect, useRef, useState } from "react";
import { uploadRecordingChunk } from "./api";
import type { QueuedRecordingChunk, RecorderStatus } from "./types";

interface UseMediaRecorderOptions {
  sessionId: string | null;
  onChunkUploaded: (chunkIndex: number, timestampMs: number) => void;
}

export interface StudentRecorder {
  status: RecorderStatus;
  stream: MediaStream | null;
  chunks: QueuedRecordingChunk[];
  error: string | null;
  uploadedCount: number;
  failedCount: number;
  start: () => Promise<void>;
  stop: () => void;
  retryFailed: () => void;
}

function pickMimeType(): string | undefined {
  const candidates = [
    "video/webm;codecs=vp9,opus",
    "video/webm;codecs=vp8,opus",
    "video/webm",
  ];
  return candidates.find((candidate) => MediaRecorder.isTypeSupported(candidate));
}

export function useMediaRecorder({
  sessionId,
  onChunkUploaded,
}: UseMediaRecorderOptions): StudentRecorder {
  const [status, setStatus] = useState<RecorderStatus>(
    typeof MediaRecorder === "undefined" ? "unavailable" : "idle",
  );
  const [stream, setStream] = useState<MediaStream | null>(null);
  const [chunks, setChunks] = useState<QueuedRecordingChunk[]>([]);
  const [error, setError] = useState<string | null>(null);
  const recorderRef = useRef<MediaRecorder | null>(null);
  const sequenceRef = useRef(0);
  const sessionIdRef = useRef<string | null>(sessionId);

  useEffect(() => {
    sessionIdRef.current = sessionId;
  }, [sessionId]);

  const updateChunk = useCallback(
    (chunkIndex: number, patch: Partial<QueuedRecordingChunk>) => {
      setChunks((items) =>
        items.map((item) =>
          item.chunk_index === chunkIndex ? { ...item, ...patch } : item,
        ),
      );
    },
    [],
  );

  const uploadChunk = useCallback(
    async (chunk: QueuedRecordingChunk) => {
      const activeSessionId = sessionIdRef.current;
      if (activeSessionId === null) {
        setError("Recording chunk is queued, but no session is active.");
        updateChunk(chunk.chunk_index, { status: "failed" });
        return;
      }
      updateChunk(chunk.chunk_index, {
        status: "uploading",
        attempts: chunk.attempts + 1,
      });
      try {
        await uploadRecordingChunk(activeSessionId, chunk.chunk_index, chunk.blob);
        updateChunk(chunk.chunk_index, { status: "uploaded" });
        onChunkUploaded(chunk.chunk_index, chunk.timestamp_ms);
      } catch (cause) {
        const message = `Chunk ${chunk.chunk_index} upload failed: ${String(cause)}`;
        setError(message);
        updateChunk(chunk.chunk_index, { status: "failed" });
      }
    },
    [onChunkUploaded, updateChunk],
  );

  const start = useCallback(async () => {
    if (status === "recording" || status === "requesting") {
      return;
    }
    if (typeof MediaRecorder === "undefined") {
      setStatus("unavailable");
      setError("MediaRecorder is not available in this browser.");
      return;
    }
    if (!navigator.mediaDevices?.getUserMedia) {
      setStatus("unavailable");
      setError("getUserMedia is not available in this browser.");
      return;
    }

    setStatus("requesting");
    setError(null);
    try {
      const mediaStream = await navigator.mediaDevices.getUserMedia({
        video: true,
        audio: true,
      });
      const mimeType = pickMimeType();
      const recorder = new MediaRecorder(
        mediaStream,
        mimeType ? { mimeType } : undefined,
      );
      recorderRef.current = recorder;
      setStream(mediaStream);

      recorder.ondataavailable = (event) => {
        if (event.data.size === 0) {
          return;
        }
        const chunk: QueuedRecordingChunk = {
          chunk_index: sequenceRef.current,
          timestamp_ms: Date.now(),
          blob: event.data,
          status: "queued",
          attempts: 0,
        };
        sequenceRef.current += 1;
        setChunks((items) => [chunk, ...items].slice(0, 60));
        void uploadChunk(chunk);
      };

      recorder.onerror = (event) => {
        const mediaError = event as Event & { error?: DOMException };
        const message = mediaError.error?.message || "MediaRecorder error.";
        setError(message);
        setStatus("error");
      };

      recorder.onstop = () => {
        setStatus("stopped");
      };

      recorder.start(2_000);
      setStatus("recording");
    } catch (cause) {
      const message = `Unable to start recording: ${String(cause)}`;
      setError(message);
      setStatus("error");
    }
  }, [status, uploadChunk]);

  const stop = useCallback(() => {
    const recorder = recorderRef.current;
    if (recorder && recorder.state !== "inactive") {
      setStatus("stopping");
      recorder.stop();
    }
    recorderRef.current = null;
    setStream((current) => {
      current?.getTracks().forEach((track) => track.stop());
      return null;
    });
  }, []);

  const retryFailed = useCallback(() => {
    const failed = chunks.filter((chunk) => chunk.status === "failed");
    for (const chunk of failed) {
      void uploadChunk(chunk);
    }
  }, [chunks, uploadChunk]);

  useEffect(() => {
    return () => {
      const recorder = recorderRef.current;
      if (recorder && recorder.state !== "inactive") {
        recorder.stop();
      }
      stream?.getTracks().forEach((track) => track.stop());
    };
  }, [stream]);

  const uploadedCount = chunks.filter((chunk) => chunk.status === "uploaded").length;
  const failedCount = chunks.filter((chunk) => chunk.status === "failed").length;

  return {
    status,
    stream,
    chunks,
    error,
    uploadedCount,
    failedCount,
    start,
    stop,
    retryFailed,
  };
}
