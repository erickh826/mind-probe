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
