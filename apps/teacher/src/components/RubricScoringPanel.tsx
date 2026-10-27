import { useEffect, useMemo, useState } from "react";

import type { RubricCategory, RubricData, RubricEvaluationReport, TeacherMarker } from "../types";

interface RubricScoringPanelProps {
  caseId: string;
  rubric: RubricData | null;
  sessionId: string;
  teacherMarkers: TeacherMarker[];
}

const DEFAULT_CATEGORIES: RubricCategory[] = [
  { key: "opening", label: "開場及程序說明", max: 10 },
  { key: "rapport", label: "建立關係及尊重", max: 10 },
  { key: "questioning", label: "提問方式", max: 20 },
  { key: "active_listening", label: "主動聆聽及避免打斷", max: 15 },
  { key: "follow_up", label: "跟進細節及矛盾", max: 15 },
  { key: "evidence_strategy", label: "證據展示策略", max: 10 },
  { key: "avoid_coercion", label: "避免誘導、威嚇及不當承諾", max: 10 },
  { key: "closing", label: "總結及結束", max: 5 },
  { key: "reflection", label: "事後反思", max: 5 },
];

function clampScore(value: number, max: number): number {
  if (!Number.isFinite(value)) {
    return 0;
  }
  return Math.min(max, Math.max(0, value));
}

export function RubricScoringPanel({ caseId, rubric, sessionId, teacherMarkers }: RubricScoringPanelProps) {
  const categories = rubric?.categories.length ? rubric.categories : DEFAULT_CATEGORIES;
  const [scores, setScores] = useState<Record<string, number>>({});
  const [comments, setComments] = useState<Record<string, string>>({});
  const [overallComment, setOverallComment] = useState("");

  useEffect(() => {
    setScores((current) =>
      Object.fromEntries(categories.map((category) => [category.key, clampScore(current[category.key] ?? 0, category.max)])),
    );
    setComments((current) => Object.fromEntries(categories.map((category) => [category.key, current[category.key] ?? ""])));
  }, [categories]);

  const totalScore = useMemo(
    () => categories.reduce((total, category) => total + clampScore(scores[category.key] ?? 0, category.max), 0),
    [categories, scores],
  );
  const totalPossible = rubric?.total ?? categories.reduce((total, category) => total + category.max, 0);

  function buildReport(): RubricEvaluationReport {
    return {
      session_id: sessionId,
      case_id: caseId,
      exported_at: new Date().toISOString(),
      total_score: totalScore,
      total_possible: totalPossible,
      overall_comment: overallComment,
      items: categories.map((category) => ({
        key: category.key,
        label: category.label,
        score: clampScore(scores[category.key] ?? 0, category.max),
        max: category.max,
        comment: comments[category.key] ?? "",
      })),
      teacher_markers: teacherMarkers,
    };
  }

  function download(content: string, extension: string, type: string) {
    const url = URL.createObjectURL(new Blob([content], { type }));
    const link = document.createElement("a");
    link.href = url;
    link.download = `${sessionId || "session"}_evaluation_report.${extension}`;
    link.click();
    URL.revokeObjectURL(url);
  }

  function exportJson() {
    const report = buildReport();
    download(JSON.stringify(report, null, 2), "json", "application/json");
  }


  function csvCell(value: unknown): string {
    return `"${String(value ?? "").replaceAll('"', '""')}"`;
  }

  function exportCsv() {
    const report = buildReport();
    const rows = [
      ["session_id", "case_id", "exported_at", "criterion_key", "criterion", "score", "maximum", "comment", "overall_comment"],
      ...report.items.map((item) => [report.session_id, report.case_id, report.exported_at, item.key, item.label, item.score, item.max, item.comment, report.overall_comment]),
    ];
    download(`\uFEFF${rows.map((row) => row.map(csvCell).join(",")).join("\r\n")}`, "csv", "text/csv;charset=utf-8");
  }

  function exportTextReport() {
    const report = buildReport();
    const lines = [
      "Mind Probe scoring report",
      `Session: ${report.session_id}`,
      `Case: ${report.case_id}`,
      `Exported: ${report.exported_at}`,
      `Total: ${report.total_score} / ${report.total_possible}`,
      "",
      ...report.items.flatMap((item) => [`${item.label}: ${item.score} / ${item.max}`, `Comment: ${item.comment || "-"}`, ""]),
      `Overall remarks: ${report.overall_comment || "-"}`,
      "",
      "Teacher markers:",
      ...report.teacher_markers.map((marker) => `${(marker.t_ms / 1000).toFixed(1)}s — ${marker.note}`),
    ];
    download(lines.join("\r\n"), "txt", "text/plain;charset=utf-8");
  }

  return (
    <section className="panel rubric-panel">
      <div className="panel-heading">
        <div>
          <p className="eyebrow">Rubric</p>
          <h2>Scoring</h2>
        </div>
        <strong className="score-total">
          {totalScore} / {totalPossible}
        </strong>
      </div>

      <div className="rubric-list">
        {categories.map((category) => (
          <article className="rubric-item" key={category.key}>
            <div className="rubric-row">
              <label htmlFor={`score-${category.key}`}>{category.label}</label>
              <div className="score-input">
                <input
                  id={`score-${category.key}`}
                  max={category.max}
                  min={0}
                  onChange={(event) =>
                    setScores((current) => ({
                      ...current,
                      [category.key]: clampScore(Number(event.target.value), category.max),
                    }))
                  }
                  type="number"
                  value={scores[category.key] ?? 0}
                />
                <span>/ {category.max}</span>
              </div>
            </div>
            <textarea
              aria-label={`${category.label} comment`}
              onChange={(event) =>
                setComments((current) => ({
                  ...current,
                  [category.key]: event.target.value,
                }))
              }
              placeholder="Comment"
              rows={2}
              value={comments[category.key] ?? ""}
            />
          </article>
        ))}
      </div>

      <label className="field stacked-field">
        <span>Overall remarks</span>
        <textarea
          onChange={(event) => setOverallComment(event.target.value)}
          rows={4}
          value={overallComment}
        />
      </label>

      <div className="export-buttons">
        <button className="primary-button" disabled={!sessionId} onClick={exportJson} type="button">Export JSON</button>
        <button className="primary-button" disabled={!sessionId} onClick={exportCsv} type="button">Export CSV</button>
        <button className="primary-button" disabled={!sessionId} onClick={exportTextReport} type="button">Scoring report</button>
      </div>
    </section>
  );
}
