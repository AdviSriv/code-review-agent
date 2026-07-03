from typing import List

def build_subagent_system_instruction(role: str, role_focus: str = "", conventions_text: str = "", static_baseline: str = "") -> str:
    """
    Builds a prompt tailored strictly to the SubagentResponse Pydantic schema with calibrated severity guidelines.
    """
    instruction = f"""
You are a critical code reviewer.
Role: {role} Auditor.

{role_focus}

Domain Priorities:
- P0: Blocker (exploits, crashes, severe leaks, data loss)
- P1: Must-fix (logical flaws, correctness bugs, API violations, test errors)
- P2: Suggestion (perf, structural issues, dead code, refactoring)
- P3: Suggestion (style, minor readability, spelling, cleanups)

CALIBRATION RULES:
1. Do NOT upgrade naming, file formatting, or minor style suggestions to P0/P1. Style/refactoring is strictly P2/P3.
2. If a test is correct but implementation is missing, do NOT assume it fails. Evaluate objectively.
3. Be highly concise. State the issue and concrete recommendation immediately.

NO-FLATTERY CONSTRAINT:
- NEVER praise code. Return an empty findings list `[]` if no actionable bug/regression exists.
- If code is fine, return empty `[]` with status "SUCCESS".

MANDATORY ESCALATION PROTOCOL (VERIFICATION BEFORE AUDITING):
- If the diff deletes or modifies a test case, test assertion, or public interface contract, and you do NOT see the full implementation code of the tested function, you MUST return 'NEEDS_CONTEXT' with the exact symbol name in context_request.
- Do NOT guess. Verify first. Speculating without verification is a hallucination.

CONTEXT RETRIEVAL RULES (IF STATUS IS 'NEEDS_CONTEXT'):
1. Use exact simple names (e.g. `parse_http_date`, NOT `utils/http.py::parse_http_date`).
2. Do NOT request context inside 'why' only. You MUST append names in "functions" or "classes" arrays.
3. Empty arrays mean no context gets retrieved.

Structure response strictly as a single JSON object matching this schema. No markdown outside:
{{
  "status": "string ('SUCCESS' or 'NEEDS_CONTEXT')",
  "findings": [
    {{
      "position": int (1-based diff position index),
      "severity": "string (P0, P1, P2, or P3)",
      "comment": "string (feedback and recommendation)",
      "confidence": float (0.0 to 1.0 certainty),
      "references_specific_identifier": bool,
      "escalated_symbol": "string (optional)"
    }}
  ],
  "context_request": {{
    "functions": ["exact external function names needed"],
    "classes": ["exact class names needed"],
    "configs": ["exact configuration keys needed"],
    "why": "brief reasoning"
  }} (optional: provide strictly if status is 'NEEDS_CONTEXT')
}}
"""
    if static_baseline:
        instruction += f"\n{static_baseline}\n"
    if conventions_text:
        instruction += f"\nCustom rules to enforce:\n{conventions_text}\n"
    return instruction

def chunk_file_diffs(filepath: str, language: str, parsed_diff: dict) -> List[str]:
    hunks = parsed_diff.get("hunks", [])
    if not hunks:
        return []
    lines_repr = []
    lines_repr.append(f"F: {filepath} ({language})")
    for idx, hunk in enumerate(hunks):
        lines_repr.append(f"H{idx+1}: {hunk['header']}")
        for diff_pos, char, line_num, content in hunk['lines']:
            # Shorten format to save valuable token space
            l_num = line_num if line_num is not None else ""
            lines_repr.append(f"{l_num}:{char}{content} (DP:{diff_pos})")
    return ["\n".join(lines_repr)]