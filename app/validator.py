import re
import builtins
from app.models import CodeComment

# Mapping validation metrics
SEVERITY_RANK = {"P0": 0, "P1": 1, "P2": 2, "P3": 3}
REPORT_THRESHOLD = SEVERITY_RANK["P2"]
CONFIDENCE_THRESHOLD = 0.6

# Common safe framework/language identifiers that shouldn't trigger demotion
SAFE_IDENTIFIERS = {
    "ValueError", "TypeError", "KeyError", "AttributeError", "NotImplementedError", "RuntimeError", "Exception",
    "self", "cls", "None", "True", "False", "dict", "list", "set", "tuple", "str", "int", "float", "bool",
    "QuerySet", "Query", "clone", "chain", "execute_sql", "explain", "explain_json", "options", "sql_format",
    "_explain_json_cache", "supported_explain_formats", "supports_explain_json", "supports_json_field"
}

def is_identifier_grounded(comment: str, parsed_diff_file: dict) -> bool:
    all_text = " ".join(
        list(parsed_diff_file.get("added_lines", {}).values()) + 
        list(parsed_diff_file.get("context_lines", {}).values())
    )
    candidates = re.findall(r"`([A-Za-z_][A-Za-z0-9_]*)`", comment)
    if not candidates:
        return True
    
    # Grounded if at least one candidate is in the diff, is a standard builtin, or is a safe framework identifier
    for c in candidates:
        if c in all_text or c in SAFE_IDENTIFIERS or hasattr(builtins, c):
            return True
            
    return False

def validate_and_deduplicate_comments(findings: list, file_path: str, parsed_diff_file: dict) -> list:
    valid_comments = []
    valid_positions = parsed_diff_file.get("valid_positions", set())
    
    for f in findings:
        f.file = file_path
        if f.position not in valid_positions:
            print(f"[Validator] Rejected hallucinated position {f.position} on {file_path}")
            continue
            
        if f.confidence < CONFIDENCE_THRESHOLD:
            continue
            
        if f.references_specific_identifier:
            if not is_identifier_grounded(f.comment, parsed_diff_file):
                old_sev = f.severity
                f.severity = {"P0": "P1", "P1": "P2", "P2": "P3", "P3": "P3"}.get(f.severity, "P3")
                print(f"[Validator] Ungrounded symbol reference at position {f.position}. Demoted {old_sev} -> {f.severity}.")
        if SEVERITY_RANK.get(f.severity, 99) > REPORT_THRESHOLD:
            continue
        valid_comments.append(f)

    grouped_by_pos = {}
    for c in valid_comments:
        grouped_by_pos.setdefault(c.position, []).append(c)
        
    deduped = []
    for pos, group in grouped_by_pos.items():
        if len(group) == 1:
            deduped.append(group[0])
        else:
            highest_severity = min((c.severity for c in group), key=lambda s: SEVERITY_RANK.get(s, 99))
            merged_lines = ["Multiple review concerns identified on this line:\n"]
            any_ref_id = False
            highest_confidence = 0.0
            for item in group:
                merged_lines.append(f"* **[{item.role}]**: {item.comment.strip()}")
                if item.references_specific_identifier:
                    any_ref_id = True
                highest_confidence = max(highest_confidence, item.confidence)
            merged_comment = CodeComment(
                file=file_path, position=pos, severity=highest_severity, role="Multiple",
                comment="\n".join(merged_lines), confidence=highest_confidence, references_specific_identifier=any_ref_id
            )
            deduped.append(merged_comment)
    return deduped