from typing import Dict

def build_review_prompt(parsed_diff: Dict[str,dict]) -> str:
    prompt_sections = []

    for file_path, data in parsed_diff.items():
        added_lines = data.get('added_lines',{})
        if not added_lines:
            continue
        
        prompt_sections.append(f"File: {file_path}")
        prompt_sections.append("---")

        sorted_added_lines = sorted(added_lines.keys())
        blocks = []

        if sorted_added_lines:
            current_block = [sorted_added_lines[0]]
            for ln in sorted_added_lines[1:]:
                if ln - current_block[-1] <= 5:
                    current_block.append(ln)
                else:
                    blocks.append(current_block)
                    current_block = [ln]
            blocks.append(current_block)

        for block in blocks:
            start_line = max(1, min(block)-5)
            end_line = max(block)+5

            block_lines = []
            for ln in range(start_line, end_line+1):
                if ln in added_lines:
                    block_lines.append(f"+ {ln}: {added_lines[ln]}")
                elif ln in data.get('context_lines',{}):
                    block_lines.append(f"{ln}: {data['context_lines'][ln]}")

            prompt_sections.append(f"Section [Lines {start_line}-{end_line}]:")
            prompt_sections.extend(block_lines)
            prompt_sections.append("")    
        prompt_sections.append("---")

    instructions = """
You are an expert software engineer performing a code review.
Analyze the provided code changes (marked with "+") and flag critical errors.

Strict priorities for review comments:
- P0: Security vulnerabilities or immediate, unhandled code crashes.
- P1: Code correctness, major logical bugs, or incorrect API contracts.
- P2: Performance overheads, expensive DB calls, or memory leaks.
- P3: Simple style consistency or minor code cleanups.

Constraints:
1. ONLY write comments pointing to lines that were changed (marked with "+").
2. Your response must be valid JSON matching the schema: {"comments": [{"file": "path", "line": 42, "severity": "P1", "comment": "Feedback..."}]}.
"""

    return "\n".join(prompt_sections) + "\n" + instructions
