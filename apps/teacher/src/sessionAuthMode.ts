export type SessionAuthMode = "join_code" | "bearer" | "review_code";

const AUTH_MODE_KEY = "mindprobe.teacher_auth_mode";

export function rememberAuthMode(mode: SessionAuthMode): void {
  try {
    sessionStorage.setItem(AUTH_MODE_KEY, mode);
  } catch {
    // The form still shows the mode chosen in this document.
  }
}

export function readAuthMode(): SessionAuthMode {
  try {
    const value = sessionStorage.getItem(AUTH_MODE_KEY);
    if (value === "join_code" || value === "bearer" || value === "review_code") {
      return value;
    }
  } catch {
    // Fall through to the default join form.
  }
  return "join_code";
}

export function credentialFieldCopy(mode: SessionAuthMode): {
  legend: string;
  placeholder: string;
} {
  if (mode === "review_code") {
    return { legend: "Review code", placeholder: "Review code" };
  }
  if (mode === "join_code") {
    return { legend: "Teacher code", placeholder: "Join code" };
  }
  return { legend: "Token", placeholder: "Bearer token" };
}
