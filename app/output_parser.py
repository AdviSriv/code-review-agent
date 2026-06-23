import json
from typing import List
from app.models import CodeReviewResponse, CodeComment

SEVERITY_LABELS = {
    "P0": "Blocker",
    "P1": "Must-fix-before-merge",
    "P2": "Suggestion",
    "P3": "Suggestion"
}

def parse_and_sort_comments(raw_text: str) -> List[CodeComment]:
    try:
        cleaned_text = raw_text.strip()
        if cleaned_text.startswith("```json"):
            cleaned_text = cleaned_text[7:]
        if cleaned_text.startswith("```"):
            cleaned_text = cleaned_text[3:]
        if cleaned_text.endswith("```"):
            cleaned_text = cleaned_text[:-3]
            
        cleaned_text = cleaned_text.strip()
        data = json.loads(cleaned_text)
        
        if isinstance(data, dict) and "comments" in data:
            raw_comments = data["comments"]
        elif isinstance(data, list):
            raw_comments = data
        else:
            raw_comments = []
            
        comments = []
        for item in raw_comments:
            try:
                comment = CodeComment(**item)
                comments.append(comment)
            except Exception:
                pass
                
        severity_order = {"P0": 0, "P1": 1, "P2": 2, "P3": 3}
        comments.sort(key=lambda c: severity_order.get(c.severity, 99))
        return comments
    except Exception as e:
        raise ValueError(f"Parsing failed for raw text structure: {e}\nRaw output: {raw_text}")

def format_terminal_output(comments: List[CodeComment]):
    print("\n================== TERMINAL REVIEW RESULTS ==================")
    for c in comments:
        label = SEVERITY_LABELS.get(c.severity, "Suggestion")
        ref_id = " (References Identifier)" if c.references_specific_identifier else ""
        print(f"[{label}] ({c.role}) File: {c.file} | Diff Position: {c.position}{ref_id}")
        print(f"Feedback: {c.comment}")
        print("-" * 60)
    print("=============================================================\n")

def build_github_review_payload(comments: List[CodeComment]) -> List[dict]:
    gh_comments = []
    
    # Sort by severity before posting (P0 -> P3)
    severity_order = {"P0": 0, "P1": 1, "P2": 2, "P3": 3}
    comments.sort(key=lambda c: severity_order.get(c.severity, 99))
    
    for c in comments:
        label = SEVERITY_LABELS.get(c.severity, "Suggestion")
        
        # Dynamically append parenthetical category labels
        if c.role == "Multiple":
            header = f"### ⚠️ [{label}] Coalesced Findings"
        else:
            header = f"### ⚠️ [{label}] ({c.role}) Finding"
            
        body = f"{header}\n\n{c.comment}"
        gh_comments.append({
            "path": c.file,
            "position": c.position,
            "body": body
        })
    return gh_comments