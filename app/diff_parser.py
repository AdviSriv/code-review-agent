import os

IGNORED_EXTENSIONS = {
    '.png', '.jpg', '.jpeg', '.gif', '.ico', '.pdf', '.zip', '.tar', '.gz', '.mp3', '.mp4', '.woff', '.woff2', '.ttf', '.eot'
}

IGNORED_FILENAMES = {
    'package-lock.json', 'yarn.lock', 'pnpm-lock.yaml', 'poetry.lock', 'Cargo.lock', 'Gemfile.lock', 'composer.lock', 
    'pydantic-lock.json', 'pnpm-workspace.yaml'
}

IGNORED_DIR_SUBSTRINGS = {
    '/node_modules/', '/vendor/', '/dist/', '/build/', '/venv/', '/.env', '/.git/', '/egg-info/'
}

def is_ignored_file(filepath: str) -> bool:
    name = os.path.basename(filepath)
    ext = os.path.splitext(filepath)[1].lower()
    
    if ext in IGNORED_EXTENSIONS:
        return True
    if name in IGNORED_FILENAMES:
        return True
    
    filepath_norm = "/" + filepath.replace("\\", "/").strip("/") + "/"
    for sub in IGNORED_DIR_SUBSTRINGS:
        if sub in filepath_norm:
            return True
            
    if name.endswith('.min.js') or name.endswith('.min.css'):
        return True
        
    return False

def identify_language(filepath: str) -> str:
    ext = os.path.splitext(filepath)[1].lower()
    mapping = {
        '.py': 'Python',
        '.js': 'JavaScript',
        '.ts': 'TypeScript',
        '.tsx': 'TypeScript React',
        '.jsx': 'JavaScript React',
        '.go': 'Go',
        '.java': 'Java',
        '.cpp': 'C++',
        '.cc': 'C++',
        '.h': 'C/C++ Header',
        '.c': 'C',
        '.cs': 'C#',
        '.rb': 'Ruby',
        '.php': 'PHP',
        '.rs': 'Rust',
        '.sh': 'Shell Script',
        '.yml': 'YAML',
        '.yaml': 'YAML',
        '.json': 'JSON',
        '.md': 'Markdown',
        '.html': 'HTML',
        '.css': 'CSS'
    }
    return mapping.get(ext, 'Unknown')

def parse_patch(patch_str: str) -> dict:
    """
    Parses a single file's patch block.
    Maps line numbers to the specific diff position index (required by GitHub API).
    """
    added_lines = {}
    context_lines = {}
    line_to_position = {}
    position_to_line = {}
    hunks = []
    
    if not patch_str:
        return {
            'added_lines': added_lines,
            'context_lines': context_lines,
            'line_to_position': line_to_position,
            'position_to_line': position_to_line,
            'hunks': hunks
        }
        
    lines = patch_str.splitlines()
    diff_position = 0
    new_line_num = 0
    current_hunk = None
    first_hunk_seen = False
    
    for line in lines:
        if line.startswith('@@'):
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
                diff_position = 0  # Position starts at 1 for the line immediately below the first @@
            else:
                diff_position += 1  # Subsequent @@ headers increment the diff position index
                
            current_hunk = {
                'header': line,
                'lines': []
            }
            hunks.append(current_hunk)
            continue
            
        if first_hunk_seen:
            diff_position += 1
            if line.startswith('+'):
                content = line[1:]
                added_lines[new_line_num] = content
                line_to_position[new_line_num] = diff_position
                position_to_line[diff_position] = new_line_num
                current_hunk['lines'].append((diff_position, '+', new_line_num, content))
                new_line_num += 1
            elif line.startswith('-'):
                current_hunk['lines'].append((diff_position, '-', None, line[1:]))
            elif line.startswith(' '):
                content = line[1:]
                context_lines[new_line_num] = content
                line_to_position[new_line_num] = diff_position
                position_to_line[diff_position] = new_line_num
                current_hunk['lines'].append((diff_position, ' ', new_line_num, content))
                new_line_num += 1
                
    return {
        'added_lines': added_lines,
        'context_lines': context_lines,
        'line_to_position': line_to_position,
        'position_to_line': position_to_line,
        'hunks': hunks
    }

def parse_full_diff(diff_text: str) -> dict:
    """
    Parses a combined multi-file unified git diff (re-uses patch logic for offline execution compatibility).
    """
    files = {}
    lines = diff_text.splitlines()
    current_file = None
    patch_lines = []
    
    i = 0
    while i < len(lines):
        line = lines[i]
        if line.startswith('diff --git'):
            if current_file and patch_lines:
                files[current_file] = parse_patch("\n".join(patch_lines))
            current_file = None
            patch_lines = []
            
            while i < len(lines) and not lines[i].startswith('@@'):
                if lines[i].startswith('+++ b/'):
                    current_file = lines[i][6:]
                i += 1
            continue
            
        if current_file:
            patch_lines.append(line)
        i += 1
        
    if current_file and patch_lines:
        files[current_file] = parse_patch("\n".join(patch_lines))
        
    return files