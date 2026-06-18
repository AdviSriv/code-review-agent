from typing import List

def build_system_instruction(conventions_text: str = "") -> str:
    instruction = """
You are an expert software engineer performing an objective, critical code review on provided code change patches.

Your feedback must strictly group comments using these priorities:
- P0: Blocker (exploits, leaks, severe vulnerabilities, or crashes)
- P1: Must-fix-before-merge (correctness flaws, test bugs, logical errors)
- P2: Suggestion (performance gaps, structural issues)
- P3: Suggestion (style updates, minor readability enhancements)

You MUST structure your final response strictly as a single JSON object matching the schema below.
Do not write any natural language explanations, markdown text, or descriptions outside of the JSON block.

Target JSON Schema:
{
  "comments": [
    {
      "file": "string (relative file path)",
      "position": integer (exact 1-based diff position index from the hunk where the issue is),
      "severity": "string (P0, P1, P2, or P3)",
      "comment": "string (constructive feedback with a clear concrete suggestion)",
      "references_specific_identifier": boolean (true if feedback references a specific variable name, function name, or class in the diff)
    }
  ]
}

ESCALATION TOOL OPTION:
You have access to a tool named `get_lines(file, start, end)`.
If you require more surrounding file context to analyze security or correctness, call this tool. Use it only when critical context is missing.
"""
    if conventions_text:
        instruction += f"\nCustom Repository Rules to enforce:\n{conventions_text}\n"
        
    return instruction

def chunk_file_diffs(filepath: str, language: str, parsed_diff: dict) -> List[str]:
    """
    Groups all modified hunks of a single file into one prompt call.
    Reduces total API requests by ~80% and extends daily free-tier quotas.
    """
    hunks = parsed_diff.get("hunks", [])
    if not hunks:
        return []
        
    lines_repr = []
    lines_repr.append(f"File: {filepath}")
    lines_repr.append(f"Language: {language}")
    
    for idx, hunk in enumerate(hunks):
        lines_repr.append(f"\nHunk {idx+1}: {hunk['header']}")
        for diff_pos, char, line_num, content in hunk['lines']:
            if line_num is not None:
                lines_repr.append(f"Line {line_num} (DiffPos {diff_pos}): {char} {content}")
            else:
                lines_repr.append(f"DiffPos {diff_pos}: {char} {content}")
                
    return ["\n".join(lines_repr)]