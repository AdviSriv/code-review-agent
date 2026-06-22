from app.models import CodeComment

def validate_and_deduplicate_comments(findings: list, file_path: str, parsed_diff_file: dict) -> list:
    """
    Module G: Ensures comments map to valid lines, deduplicates overlapping ones, and filters by severity.
    """
    valid_comments = []
    line_map = parsed_diff_file.get("position_to_line", {})
    
    # 1. Filter out hallucinated line positions
    for f in findings:
        f.file = file_path
        if f.position in line_map:
            valid_comments.append(f)
        else:
            print(f"[Validator] Rejected hallucinated comment position {f.position} on {file_path}")

    # 2. Deduplicate overlapping comments on the same line
    deduped = []
    seen_positions = {}
    
    for c in valid_comments:
        pos = c.position
        if pos not in seen_positions:
            seen_positions[pos] = c
            deduped.append(c)
        else:
            existing = seen_positions[pos]
            # Merge logic: Append findings from same line under higher severity
            if c.severity < existing.severity:
                existing.severity = c.severity
            existing.comment += f"\n\n*Alternative finding:* {c.comment}"
            
    return deduped