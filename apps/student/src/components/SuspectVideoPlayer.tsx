import { useEffect, useMemo, useRef, useState } from "react";
import type { PlaybackClip } from "../types";

interface SuspectVideoPlayerProps {
  activeClip: PlaybackClip | null;
  idleClip: PlaybackClip | null;
  paused: boolean;
  onClipStarted: (clipId: string) => void;
  onClipEnded: (clipId: string) => void;
}

function waitForCanPlay(video: HTMLVideoElement): Promise<void> {
  if (video.readyState >= video.HAVE_FUTURE_DATA) {
    return Promise.resolve();
  }
  return new Promise((resolve) => {
    const done = () => {
      video.removeEventListener("canplay", done);
      video.removeEventListener("loadeddata", done);
      resolve();
    };
    video.addEventListener("canplay", done, { once: true });
    video.addEventListener("loadeddata", done, { once: true });
    window.setTimeout(done, 1_500);
  });
}

export function SuspectVideoPlayer({
  activeClip,
  idleClip,
  paused,
  onClipStarted,
  onClipEnded,
}: SuspectVideoPlayerProps) {
  const videoARef = useRef<HTMLVideoElement>(null);
  const videoBRef = useRef<HTMLVideoElement>(null);
  const [activeSlot, setActiveSlot] = useState<0 | 1>(0);
  const [displayedClip, setDisplayedClip] = useState<PlaybackClip | null>(null);
  const [playError, setPlayError] = useState<string | null>(null);
  const currentClipRef = useRef<PlaybackClip | null>(null);

  const desiredClip = useMemo(() => activeClip ?? idleClip, [activeClip, idleClip]);
  const desiredClipId = desiredClip?.clipId ?? null;

  useEffect(() => {
    const current = currentClipRef.current;
    if (desiredClipId === current?.clipId) {
      return;
    }
    currentClipRef.current = desiredClip;
    setDisplayedClip(desiredClip);
    setPlayError(null);

    if (desiredClip === null || desiredClip.src === null) {
      videoARef.current?.pause();
      videoBRef.current?.pause();
      return;
    }

    let cancelled = false;
    const nextSlot: 0 | 1 = activeSlot === 0 ? 1 : 0;
    const nextVideo = nextSlot === 0 ? videoARef.current : videoBRef.current;
    const previousVideo = activeSlot === 0 ? videoARef.current : videoBRef.current;
    if (!nextVideo) {
      return;
    }

    nextVideo.loop = desiredClip.loop;
    nextVideo.src = desiredClip.src;
    nextVideo.currentTime = 0;
    nextVideo.load();

    void waitForCanPlay(nextVideo)
      .then(() => {
        if (cancelled) {
          return undefined;
        }
        return nextVideo.play();
      })
      .then(() => {
        if (cancelled) {
          return;
        }
        setActiveSlot(nextSlot);
        if (!desiredClip.loop) {
          onClipStarted(desiredClip.clipId);
        }
        window.setTimeout(() => {
          previousVideo?.pause();
        }, 280);
      })
      .catch((cause) => {
        if (!cancelled) {
          setPlayError(String(cause));
        }
      });

    return () => {
      cancelled = true;
    };
  }, [activeSlot, desiredClip, desiredClipId, onClipStarted]);

  useEffect(() => {
    const activeVideo = activeSlot === 0 ? videoARef.current : videoBRef.current;
    if (!activeVideo) {
      return;
    }
    if (paused) {
      activeVideo.pause();
      return;
    }
    if (displayedClip?.src) {
      void activeVideo.play().catch((cause) => setPlayError(String(cause)));
    }
  }, [activeSlot, displayedClip?.src, paused]);

  const handleEnded = () => {
    const clip = currentClipRef.current;
    if (clip && !clip.loop) {
      onClipEnded(clip.clipId);
    }
  };

  return (
    <div className="suspect-stage">
      <video
        ref={videoARef}
        className={activeSlot === 0 ? "suspect-video is-active" : "suspect-video"}
        playsInline
        preload="auto"
        onEnded={handleEnded}
      />
      <video
        ref={videoBRef}
        className={activeSlot === 1 ? "suspect-video is-active" : "suspect-video"}
        playsInline
        preload="auto"
        onEnded={handleEnded}
      />
      <div className="stage-caption">
        <span>{displayedClip?.label ?? "Awaiting suspect video"}</span>
        {displayedClip?.src === null ? <strong>Missing media source</strong> : null}
        {playError ? <strong>{playError}</strong> : null}
      </div>
    </div>
  );
}
