import { useEffect, useRef, useState } from "react";

/** A clipboard write still pending after this long is treated as failed. */
const CLIPBOARD_TIMEOUT_MS = 1500;

interface CopyFieldProps {
  label: string;
  value: string;
}

function writeWithTimeout(value: string): Promise<void> {
  // Some browsers leave writeText pending behind a permission prompt.
  return Promise.race([
    navigator.clipboard.writeText(value),
    new Promise<void>((_, reject) =>
      window.setTimeout(() => reject(new Error("clipboard timeout")), CLIPBOARD_TIMEOUT_MS),
    ),
  ]);
}

/** A read-only code with a copy button. Falls back to selecting the text for Ctrl+C. */
export function CopyField({ label, value }: CopyFieldProps) {
  const [copied, setCopied] = useState<"idle" | "ok" | "failed">("idle");
  const inputRef = useRef<HTMLInputElement>(null);

  useEffect(() => {
    if (copied !== "ok") {
      return;
    }
    const timer = window.setTimeout(() => setCopied("idle"), 2000);
    return () => window.clearTimeout(timer);
  }, [copied]);

  async function copy() {
    try {
      await writeWithTimeout(value);
      setCopied("ok");
    } catch {
      inputRef.current?.focus();
      inputRef.current?.select();
      setCopied("failed");
    }
  }

  return (
    <div className="copy-field">
      <span className="copy-label">{label}</span>
      <input
        ref={inputRef}
        readOnly
        value={value}
        spellCheck={false}
        autoComplete="off"
        onFocus={(event) => event.currentTarget.select()}
        aria-label={label}
      />
      <button type="button" onClick={copy}>
        {copied === "ok" ? "已複製" : copied === "failed" ? "已選取，請按 Ctrl+C" : "複製"}
      </button>
    </div>
  );
}
