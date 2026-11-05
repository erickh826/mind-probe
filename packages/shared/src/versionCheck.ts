// Tells a long-lived tab that a newer frontend build is deployed.
//
// Every production build writes `/version.json` (`{"buildId": ...}`) next to
// its bundle, and the bundle carries the same id as `__APP_BUILD_ID__`. A tab
// polls its own origin's file; a different id means the tab runs an old build.
// The monitor only reports. Each app decides when offering a reload is safe.

export type BuildEnv = Record<string, string | undefined>;

/** One id per build: the deployed commit on Vercel, otherwise a build timestamp. */
export function buildIdFrom(env: BuildEnv, now: number = Date.now()): string {
  const sha = env.VERCEL_GIT_COMMIT_SHA?.trim();
  return sha ? sha : `local-${now.toString(36)}`;
}

interface EmitContext {
  emitFile(file: { type: "asset"; fileName: string; source: string }): string;
}

/** Vite plugin that writes `version.json` into the build output. */
export function versionJsonPlugin(buildId: string) {
  return {
    name: "mind-probe-version-json",
    apply: "build" as const,
    generateBundle(this: EmitContext) {
      this.emitFile({
        type: "asset",
        fileName: "version.json",
        source: `${JSON.stringify({ buildId })}\n`,
      });
    },
  };
}

export const VERSION_CHECK_INTERVAL_MS = 60_000;
/** A tab that becomes visible again checks at most this often. */
export const VERSION_CHECK_MIN_GAP_MS = 10_000;

/**
 * The build id a `/version.json` response names, or null.
 *
 * A deploy from before this feature has no file, and the SPA rewrite answers
 * with index.html and 200. Anything that is not a JSON object with a short,
 * non-empty string `buildId` is null, never "update available".
 */
export async function readBuildId(response: Response): Promise<string | null> {
  if (!response.ok) {
    return null;
  }
  const type = response.headers.get("content-type") ?? "";
  if (!type.toLowerCase().includes("json")) {
    return null;
  }
  try {
    const body: unknown = await response.json();
    if (body === null || typeof body !== "object") {
      return null;
    }
    const id = (body as { buildId?: unknown }).buildId;
    return typeof id === "string" && id.length > 0 && id.length <= 200 ? id : null;
  } catch {
    return null;
  }
}

interface VisibilitySource {
  readonly visibilityState: string;
  addEventListener(type: "visibilitychange", listener: () => void): void;
  removeEventListener(type: "visibilitychange", listener: () => void): void;
}

export interface VersionMonitorOptions {
  currentBuildId: string;
  /** False (dev server, tests) keeps the monitor silent. */
  enabled?: boolean;
  url?: string;
  intervalMs?: number;
  fetchImpl?: (url: string, init: RequestInit) => Promise<Response>;
  now?: () => number;
  setIntervalImpl?: (callback: () => void, ms: number) => unknown;
  clearIntervalImpl?: (handle: unknown) => void;
  visibility?: VisibilitySource | null;
}

export interface VersionMonitor {
  /** For `useSyncExternalStore`. Polling runs only while someone subscribes. */
  subscribe(listener: () => void): () => void;
  /** The deployed build id when it differs from this tab's, otherwise null. */
  getSnapshot(): string | null;
  checkNow(): Promise<void>;
}

export function createVersionMonitor(options: VersionMonitorOptions): VersionMonitor {
  const enabled = options.enabled ?? true;
  const url = options.url ?? "/version.json";
  const intervalMs = options.intervalMs ?? VERSION_CHECK_INTERVAL_MS;
  const fetchImpl = options.fetchImpl ?? ((input, init) => fetch(input, init));
  const now = options.now ?? (() => Date.now());
  const setIntervalImpl =
    options.setIntervalImpl ?? ((callback, ms) => globalThis.setInterval(callback, ms));
  const clearIntervalImpl =
    options.clearIntervalImpl ??
    ((handle) => globalThis.clearInterval(handle as ReturnType<typeof setInterval>));
  const visibility =
    options.visibility !== undefined
      ? options.visibility
      : typeof document === "undefined"
        ? null
        : document;

  const listeners = new Set<() => void>();
  let latest: string | null = null;
  let timer: unknown = null;
  let inFlight = false;
  let lastCheckAt = Number.NEGATIVE_INFINITY;

  function stop() {
    if (timer !== null) {
      clearIntervalImpl(timer);
      timer = null;
    }
    visibility?.removeEventListener("visibilitychange", onVisible);
  }

  function onVisible() {
    if (visibility?.visibilityState === "visible" && now() - lastCheckAt >= VERSION_CHECK_MIN_GAP_MS) {
      void checkNow();
    }
  }

  async function checkNow() {
    if (!enabled || latest !== null || inFlight) {
      return;
    }
    inFlight = true;
    lastCheckAt = now();
    try {
      const response = await fetchImpl(url, {
        cache: "no-store",
        headers: { Accept: "application/json" },
      });
      const id = await readBuildId(response);
      if (id !== null && id !== options.currentBuildId && latest === null) {
        latest = id;
        // Found once is enough: the tab stays outdated until it reloads.
        stop();
        for (const listener of [...listeners]) {
          listener();
        }
      }
    } catch {
      // Offline or blocked: try again on the next tick.
    } finally {
      inFlight = false;
    }
  }

  function start() {
    if (!enabled || latest !== null || timer !== null) {
      return;
    }
    timer = setIntervalImpl(() => void checkNow(), intervalMs);
    visibility?.addEventListener("visibilitychange", onVisible);
    void checkNow();
  }

  return {
    subscribe(listener) {
      listeners.add(listener);
      start();
      return () => {
        listeners.delete(listener);
        if (listeners.size === 0) {
          stop();
        }
      };
    },
    getSnapshot: () => latest,
    checkNow,
  };
}
