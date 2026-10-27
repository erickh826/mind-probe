const REVIEW_ERROR_COPY: Record<string, string> = {
  rate_limited: "Too many attempts. Wait a minute and try again.",
  invalid_review_code: "That review code is not valid.",
  revoked_session_token: "This review session was revoked. Enter the review code again.",
  refresh_reused:
    "This review session was revoked because its refresh token was reused. Enter the review code again.",
  invalid_refresh_token: "This review session is no longer valid. Enter the review code again.",
  expired_session_token: "This review session expired. Enter the review code again.",
  expired: "This review session expired. Enter the review code again.",
  "review session missing": "This review session is no longer valid. Enter the review code again.",
  "review refresh incomplete": "This review session is no longer valid. Enter the review code again.",
};

export class ReviewRequestError extends Error {
  readonly detail: string;

  constructor(detail: string, message = reviewErrorMessage(detail)) {
    super(message);
    this.name = "ReviewRequestError";
    this.detail = detail;
  }
}

export function reviewErrorMessage(detail: string): string {
  return REVIEW_ERROR_COPY[detail] ?? detail;
}

export function shouldReturnToReviewLogin(detail: string): boolean {
  return (
    detail === "revoked_session_token" ||
    detail === "refresh_reused" ||
    detail === "invalid_refresh_token" ||
    detail === "expired_session_token" ||
    detail === "expired" ||
    detail === "invalid_review_code" ||
    detail === "rate_limited"
  );
}
