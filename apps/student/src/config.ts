export const API_BASE_URL: string = (
  import.meta.env.VITE_API_BASE_URL ?? ""
).replace(/\/$/, "");

export const MEDIA_BASE_URL: string = (
  import.meta.env.VITE_MEDIA_BASE_URL ?? API_BASE_URL
).replace(/\/$/, "");

export function apiUrl(path: string): string {
  return `${API_BASE_URL}${path}`;
}

export function mediaUrl(caseId: string, file: string): string {
  if (/^https?:\/\//.test(file) || file.startsWith("/")) {
    return file;
  }
  return `${MEDIA_BASE_URL}/cases/${encodeURIComponent(caseId)}/${file}`;
}

export function sessionSocketUrl(sessionId: string): string {
  const origin = API_BASE_URL || window.location.origin;
  const wsOrigin = origin.replace(/^http/, "ws");
  return `${wsOrigin}/ws/${encodeURIComponent(sessionId)}/student`;
}
