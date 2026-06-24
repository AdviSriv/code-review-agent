from app.models import CodeComment

SEVERITY_ORDER = {"P0": 0, "P1": 1, "P2": 2, "P3": 3}

def validate_and_deduplicate_comments(findings: list, file_path: str, parsed_diff_file: dict) -> list:
    """
    Module G: Ensures comments map to valid patch lines (including additions, context,
    and deletions) and collapses 100% of findings on the exact same line into a single card.
    """
    valid_comments = []
    valid_positions = parsed_diff_file.get("valid_positions", set())
    
    # 1. Verify positions exist within the valid patch bounds
    for f in findings:
        f.file = file_path
        if f.position in valid_positions:
            valid_comments.append(f)
        else:
            print(f"[Validator] Rejected hallucinated comment position {f.position} on {file_path}")

    # 2. Group findings strictly by their diff position
    grouped_by_pos = {}
    for c in valid_comments:
        grouped_by_pos.setdefault(c.position, []).append(c)
        
    deduped = []
    for pos, group in grouped_by_pos.items():
        if len(group) == 1:
            # Single finding on this line
            deduped.append(group[0])
        else:
            # Multiple findings: determine the highest severity
            highest_severity = min((c.severity for c in group), key=lambda s: SEVERITY_ORDER.get(s, 99))
            
            # Combine all comments on this line into a single, clean bulleted card
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
                role="Multiple",  # Triggers coalesced header formatting
                comment="\n".join(merged_lines),
                references_specific_identifier=any_ref_id
            )
            deduped.append(merged_comment)
            
    return deduped