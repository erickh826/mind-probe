export type SessionAuthMode = "join_code" | "bearer" | "review_code";

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
