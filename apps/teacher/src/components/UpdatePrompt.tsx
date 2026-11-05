/** Offers a reload once a newer build is deployed. Never reloads by itself. */
export function UpdatePrompt() {
  return (
    <div className="update-prompt" role="status">
      <span>網站已更新到新版本，重新整理後即可使用。</span>
      <button type="button" onClick={() => window.location.reload()}>
        重新整理
      </button>
    </div>
  );
}
