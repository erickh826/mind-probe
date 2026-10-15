#!/usr/bin/env python3
"""Script to scaffold a new mind-probe case directory (spec §12).

Generates the required 7 JSON configuration files and media directory:
  cases/<CASE_ID>/
  ├── case_metadata.json
  ├── ground_truth.json
  ├── suspect_knowledge.json
  ├── disclosure_rules.json
  ├── wizard_decision_tree.json
  ├── clip_manifest.json
  ├── rubric.json
  └── media/
"""

import argparse
import json
import sys
from pathlib import Path


def create_case(case_id: str, title: str, suspect_name: str, language: str = "zh-HK", cases_dir: Path | None = None) -> Path:
    case_id = case_id.strip().upper()
    if not case_id:
        raise ValueError("case_id cannot be empty")

    root = (cases_dir or Path(__file__).resolve().parent.parent / "cases") / case_id
    if root.exists():
        print(f"[!] Warning: Directory {root} already exists.")
    else:
        root.mkdir(parents=True, exist_ok=True)

    media_dir = root / "media"
    media_dir.mkdir(exist_ok=True)

    # 1. case_metadata.json
    case_metadata = {
        "case_id": case_id,
        "title": title,
        "language": language,
        "version": "0.1.0",
        "description": f"Case definition for {title} ({case_id}).",
        "suspect": {
            "name": suspect_name,
            "role": "suspect",
            "posture": "seated"
        },
        "interview": {
            "expected_duration_min": [10, 15],
            "setting": "固定模擬審訊室 fixed mock interrogation room"
        },
        "clip_budget_target": [25, 35]
    }

    # 2. ground_truth.json
    ground_truth = {
        "case_id": case_id,
        "description": "客觀真實事實 Ground Truth（spec §12）。用於評估審訊是否揭露真相。",
        "timeline": [
            { "time": "20:00", "event": "Suspect arrived at the scene" },
            { "time": "21:30", "event": "Incident occurred" },
            { "time": "22:00", "event": "Suspect departed" }
        ],
        "key_facts": {
            "motive": "Financial distress",
            "accomplices": [],
            "evidence_location": "Hidden in vehicle"
        }
    }

    # 3. suspect_knowledge.json
    suspect_knowledge = {
        "case_id": case_id,
        "description": "疑犯知悉的事實範圍與預設態度（spec §12）。",
        "known_facts": [
            "Was present at the location",
            "Knows victim/complainant"
        ],
        "concealed_facts": [
            "Actual departure time",
            "Possession of stolen goods"
        ],
        "false_claims": [
            "Claimed left early at 20:30"
        ]
    }

    # 4. disclosure_rules.json
    disclosure_rules = {
        "case_id": case_id,
        "description": "披露規則：控制哪些事實在滿足前置條件後才可由疑犯透露，避免矛盾（spec §12）。",
        "rules": [
            {
                "fact": "claimed_alibi",
                "requires": [],
                "forbidden_after": [],
                "note": "疑犯可隨時提出不在場證明。"
            },
            {
                "fact": "actual_timeline",
                "requires": ["evidence_presented", "student_confronts_contradiction"],
                "forbidden_after": [],
                "note": "只有當學生出示證據並指出矛盾後，疑犯才透露真實時間線。"
            }
        ]
    }

    # 5. wizard_decision_tree.json
    wizard_decision_tree = {
        "case_id": case_id,
        "description": "Wizard 三層決策樹（spec §8）：分類(F1–F8) → 回答選項 → 語氣狀態。葉節點對應 clip_manifest 的 clip_id。",
        "tiers": {
            "categories": {
                "F1": "identity",
                "F2": "relationships",
                "F3": "timeline",
                "F4": "location",
                "F5": "evidence",
                "F6": "contradiction",
                "F7": "procedural_rights",
                "F8": "general"
            },
            "states": {
                "N": "neutral",
                "T": "thinking",
                "D": "defensive",
                "I": "impatient",
                "R": "refusing"
            }
        },
        "tree": {
            "F3": {
                "label": "時間線 Timeline",
                "options": {
                    "1": { "label": "到達時間 arrival_time", "clips": { "N": None } },
                    "2": { "label": "離開時間 leave_time", "clips": { "N": None, "D": None } }
                }
            },
            "F8": {
                "label": "通用回答 General / Fallback",
                "options": {
                    "1": { "label": "要求澄清 ask_clarify", "clips": { "N": f"{case_id}_FALLBACK_CLARIFY_01" } },
                    "2": { "label": "不明白 dont_understand", "clips": { "N": None } },
                    "3": { "label": "不記得 dont_remember", "clips": { "D": None } },
                    "4": { "label": "拒答 refuse", "clips": { "R": None } }
                }
            }
        }
    }

    # 6. clip_manifest.json
    clip_manifest = {
        "case_id": case_id,
        "clips": [
            {
                "clip_id": f"{case_id}_FALLBACK_CLARIFY_01",
                "category": "fallback",
                "intent": "ask_clarify",
                "character_state": "neutral",
                "text": "你想問咩？我唔係好明你嘅意思。",
                "reveals": [],
                "requires": [],
                "forbidden_after": [],
                "duration_ms": 3000,
                "file": f"media/{case_id}_FALLBACK_CLARIFY_01.mp4",
                "hotkey": "F8-1-N"
            }
        ]
    }

    # 7. rubric.json
    rubric = {
        "case_id": case_id,
        "description": "教師評分 rubric（spec §13），總分 100。系統只提供證據及標記，正式評分由教師負責。",
        "total": 100,
        "categories": [
            { "key": "opening", "label": "開場及程序說明", "max": 10 },
            { "key": "rapport", "label": "建立關係及尊重", "max": 10 },
            { "key": "questioning", "label": "提問方式", "max": 20 },
            { "key": "active_listening", "label": "主動聆聽及避免打斷", "max": 15 },
            { "key": "follow_up", "label": "跟進細節及矛盾", "max": 15 },
            { "key": "evidence_strategy", "label": "證據展示策略", "max": 10 },
            { "key": "avoid_coercion", "label": "避免誘導、威嚇及不當承諾", "max": 10 },
            { "key": "closing", "label": "總結及結束", "max": 5 },
            { "key": "reflection", "label": "事後反思", "max": 5 }
        ]
    }

    files = {
        "case_metadata.json": case_metadata,
        "ground_truth.json": ground_truth,
        "suspect_knowledge.json": suspect_knowledge,
        "disclosure_rules.json": disclosure_rules,
        "wizard_decision_tree.json": wizard_decision_tree,
        "clip_manifest.json": clip_manifest,
        "rubric.json": rubric,
    }

    for filename, content in files.items():
        file_path = root / filename
        with open(file_path, "w", encoding="utf-8") as f:
            json.dump(content, f, ensure_ascii=False, indent=2)
            f.write("\n")
        print(f"  + Created {file_path.relative_to(root.parent.parent)}")

    print(f"\n[+] Successfully created case {case_id} at {root}")
    return root


def main():
    parser = argparse.ArgumentParser(description="Create a new mind-probe case scaffold.")
    parser.add_argument("case_id", help="Unique Case ID, e.g. CASE002")
    parser.add_argument("--title", default="新案件 New Interrogation Case", help="Case Title")
    parser.add_argument("--suspect", default="張三 Cheung Sam", help="Suspect Name")
    parser.add_argument("--language", default="zh-HK", help="Language code (default: zh-HK)")

    args = parser.parse_args()
    create_case(
        case_id=args.case_id,
        title=args.title,
        suspect_name=args.suspect,
        language=args.language,
    )


if __name__ == "__main__":
    main()
