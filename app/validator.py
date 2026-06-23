import re
from app.models import CodeComment

SEVERITY_ORDER = {"P0": 0, "P1": 1, "P2": 2, "P3": 3}
ROLE_PRIORITY = {"Security": 0, "Logic": 1, "Architecture": 2, "Maintainability": 3}

def get_word_set(text: str) -> set:
    """Normalizes string inputs to a cleaned set of words."""
    return set(re.findall(r'\w+', text.lower()))

def calculate_jaccard_similarity(text1: str, text2: str) -> float:
    """Calculates keyword overlap between two feedback blocks."""
    words1 = get_word_set(text1)
    words2 = get_word_set(text2)
    if not words1 or not words2:
        return 0.0
    intersection = words1.intersection(words2)
    union = words1.union(words2)
    return len(intersection) / len(union)

def validate_and_deduplicate_comments(findings: list, file_path: str, parsed_diff_file: dict) -> list:
    """
    Module G: Ensures comments map to valid lines, and programmatically filters out 
    redundant/overlapping comments using Jaccard Similarity to prevent noise.
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
        if len(group) == 1:
            deduped.append(group[0])
            continue
            
        # Group duplicates semantically using word-set overlaps
        semantic_groups = []
        for item in group:
            matched = False
            for s_group in semantic_groups:
                representative = s_group[0]
                # If finding has > 35% word overlap, treat it as a semantic duplicate
                if calculate_jaccard_similarity(item.comment, representative.comment) > 0.35:
                    s_group.append(item)
                    matched = True
                    break
            if not matched:
                semantic_groups.append([item])
                
        # Resolve each semantic group into a single high-quality comment
        for s_group in semantic_groups:
            # Sort semantic group to place highest priority item first
            s_group.sort(key=lambda x: (SEVERITY_ORDER.get(x.severity, 99), ROLE_PRIORITY.get(x.role, 99)))
            
            best_finding = s_group[0]
            concurring_roles = [item.role for item in s_group[1:]]
            
            if concurring_roles:
                best_finding.comment += f"\n\n*(Note: Also flagged by alignment audit as a {', '.join(concurring_roles)} concern).* "
                
            deduped.append(best_finding)
            
    return deduped