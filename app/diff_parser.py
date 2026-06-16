import re

def parse_diff(diff_text: str)->dict:
    """
    Parses a unified git diff.
    Returns a dictionary structured as:
    {
        "file_path": {
            "added_lines": {line_num: content},
            "context_lines": {line_num: content},
            "line_to_position": {line_num: diff_position}
        }
    }
    """

    files = {}
    lines = diff_text.splitlines()

    current_file = None
    diff_position = 0
    new_line_num = 0
    first_hunk_seen = False

    i=0
    while i<len(lines):
        line = lines[i]

        if line.startswith('diff --git'):
            current_file = None
            diff_position = 0
            first_hunk_seen = False

            while i<len(lines) and not lines[i].startswith('@@'):
                if lines[i].startswith('+++ b/'):
                    current_file = lines[i][6:]
                i += 1
            
            if current_file:
                files[current_file] = {'added_lines':{}, 'context_lines': {}, 'line_to_position': {}}
            continue

        if current_file and line.startswith('@@'):
            try:
                parts = line.split(' ')
                new_info = parts[2]
                if ',' in new_info:
                    new_line_num = int(new_info.split(',')[0][1:])
                else:
                    new_line_num = int(new_info[1:])
            except Exception:
                pass

            if not first_hunk_seen:
                first_hunk_seen = True
                diff_position = 0
            else:
                diff_position += 1
            
        elif current_file and first_hunk_seen:
            diff_position += 1
            if line.startswith('+'):
                content = line[1:]
                files[current_file]['added_lines'][new_line_num] = content
                files[current_file]['line_to_position'][new_line_num] = diff_position
                new_line_num += 1
            elif line.startswith('-'):
                pass
                
            elif line.startswith(' '):
                content = line[1:]
                files[current_file]['context_lines'][new_line_num] = content
                files[current_file]['line_to_position'][new_line_num] = diff_position
                new_line_num += 1

            else:
                pass
        i += 1
    return files

        