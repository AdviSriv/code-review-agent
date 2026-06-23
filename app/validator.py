from app.models import CodeComment

SEVERITY_ORDER = {"P0": 0, "P1": 1, "P2": 2, "P3": 3}

def validate_and_deduplicate_comments(findings: list, file_path: str, parsed_diff_file: dict) -> list:
    """
    Module G: Ensures comments map to valid lines, deduplicates overlapping issues,
    and cleanly groups multiple subagent findings on the exact same line into bullet points.
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

    # 2. Group findings by their diff position
    grouped_by_pos = {}
    for c in valid_comments:
        grouped_by_pos.setdefault(c.position, []).append(c)
        
    deduped = []
    for pos, group in grouped_by_pos.items():
        # Identify the highest severity in the group
        highest_severity = min((c.severity for c in group), key=lambda s: SEVERITY_ORDER.get(s, 99))
        
        if len(group) == 1:
            # Simple single-agent finding
            deduped.append(group[0])
        else:
            # Multiple findings on the same line: merge into a clean, bulleted format
            merged_lines = ["Multiple review concerns identified on this line:\n"]
            any_ref_id = False
            
            for item in group:
                merged_lines.append(f"* **[{item.role}]**: {item.comment.strip()}")
                if item.references_specific_identifier:
                    any_ref_id = True
                    
            merged_comment = CodeComment(
                file=file_path,
                position=pos,
                severity=highest_severity,
                role="Multiple",  # Group key to format the heading accordingly
                comment="\n".join(merged_lines),
                references_specific_identifier=any_ref_id
            )
            deduped.append(merged_comment)
            
    return deduped