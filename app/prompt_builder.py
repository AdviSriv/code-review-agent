from typing import List

def build_system_instruction(conventions_text: str = "") -> str:
    instruction = """
You are an expert software engineer performing an objective, critical code review on provided code change patches.

Your feedback must strictly group comments using these priorities:
- P0: Blocker (exploits, leaks, severe vulnerabilities, or crashes)
- P1: Must-fix-before-merge (correctness flaws, test bugs, logical errors)
- P2: Suggestion (performance gaps, structural issues)
- P3: Suggestion (style updates, minor readability enhancements)

You MUST structure your response as JSON matching the CodeReviewResponse schema:
- position: The exact 1-based index (diff_pos) mapped directly from the hunk.
- references_specific_identifier: True if feedback highlights a specific function, class, or variable name.

ESCALATION TOOL OPTION:
You have access to a tool named `get_lines(file, start, end)`.
If you require more surrounding file context to analyze security or correctness, call this tool. Use it only when critical context is missing.
"""
    if conventions_text:
        instruction += f"\nCustom Repository Rules to enforce:\n{conventions_text}\n"
        
    return instruction

def chunk_file_diffs(filepath: str, language: str, parsed_diff: dict) -> List[str]:
    chunks = []
    hunks = parsed_diff.get("hunks", [])
    
    for idx, hunk in enumerate(hunks):
        lines_repr = []
        lines_repr.append(f"File: {filepath}")
        lines_repr.append(f"Language: {language}")
        lines_repr.append(f"Hunk: {hunk['header']}")
        
        for diff_pos, char, line_num, content in hunk['lines']:
            if line_num is not None:
                lines_repr.append(f"Line {line_num} (DiffPos {diff_pos}): {char} {content}")
            else:
                lines_repr.append(f"DiffPos {diff_pos}: {char} {content}")
                
        chunks.append("\n".join(lines_repr))
        
    return chunks