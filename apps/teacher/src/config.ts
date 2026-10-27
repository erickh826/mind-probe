const trimTrailingSlash = (value: string) => value.replace(/\/+$/, "");

function resolveApiBaseUrl(): string {
  const configured = import.meta.env.VITE_API_BASE_URL?.trim();
  if (configured) {
    return trimTrailingSlash(configured);
  }
  // Dev servers default to the local Session Server. Production builds fail
  // in vite.config.ts when VITE_API_BASE_URL is unset, so this branch is
  // removed from the production bundle.
  if (import.meta.env.DEV) {
    return "http://localhost:8000";
  }
  return "";
}

export const API_BASE_URL = resolveApiBaseUrl();

export const MEDIA_BASE_URL = trimTrailingSlash(
  import.meta.env.VITE_MEDIA_BASE_URL ?? API_BASE_URL,
);

export function apiUrl(path: string): string {
  return `${API_BASE_URL}${path.startsWith("/") ? path : `/${path}`}`;
}

export function recordingUrl(sessionId: string): string {
  return apiUrl(`/sessions/${encodeURIComponent(sessionId)}/recording`);
}

export function mediaUrl(caseId: string, file: string): string {
  if (/^https?:\/\//i.test(file) || file.startsWith("/")) {
    return file;
  }
  const normalizedFile = file.replace(/^\/+/, "");
  return `${MEDIA_BASE_URL}/cases/${encodeURIComponent(caseId)}/${normalizedFile}`;
}
