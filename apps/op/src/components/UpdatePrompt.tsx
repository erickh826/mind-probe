interface UpdatePromptProps {
  /** Plaintext codes on screen are lost by a reload; say so next to the button. */
  holdsCodes: boolean;
}

/** Offers a reload once a newer build is deployed. Never reloads by itself. */
export function UpdatePrompt({ holdsCodes }: UpdatePromptProps) {
  return (
    <div className="update-prompt" role="status">
      <span>網站已更新到新版本，重新整理後即可使用。</span>
      {holdsCodes ? (
        <strong className="update-prompt-warning">重新整理會清除畫面上尚未複製的單次碼，請先複製保存。</strong>
      ) : null}
      <button type="button" onClick={() => window.location.reload()}>
        重新整理
      </button>
    </div>
  );
}
