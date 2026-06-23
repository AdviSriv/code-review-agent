import os
import requests
from app.config import get_config

def get_symbol_signature_or_content(filepath: str, line_range: list, fallback_only: bool = False) -> str:
    """
    Fetches the content or signature of a symbol from the VM's disk.
    If the file does not exist locally, falls back to the GitHub API using context.
    """
    # 1. Try local filesystem lookup
    if os.path.exists(filepath):
        try:
            with open(filepath, "r", encoding="utf-8", errors="ignore") as f:
                lines = f.readlines()
            start = max(0, line_range[0] - 1)
            end = min(len(lines), line_range[1])
            symbol_lines = lines[start:end]
            
            if fallback_only:
                return symbol_lines[0].strip() if symbol_lines else ""
            return "".join(symbol_lines)
        except Exception:
            pass
            
    # 2. Remote API Fallback (Enables cross-file reasoning in server webhook environments)
    from app.llm_client import _context
    owner = _context.get("owner")
    repo = _context.get("repo")
    token = _context.get("token")
    ref = _context.get("commit_sha")  # Resolves the modified HEAD commit on the branch
    
    if owner and repo and token and ref:
        url = f"https://api.github.com/repos/{owner}/{repo}/contents/{filepath}"
        headers = {
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github.v3.raw"
        }
        params = {"ref": ref}
        try:
            response = requests.get(url, headers=headers, params=params)
            if response.status_code == 200:
                lines = response.text.splitlines(keepends=True)
                start = max(0, line_range[0] - 1)
                end = min(len(lines), line_range[1])
                symbol_lines = lines[start:end]
                
                if fallback_only:
                    return symbol_lines[0].strip() if symbol_lines else ""
                return "".join(symbol_lines)
        except Exception as e:
            return f"# Remote fallback error loading {filepath}: {e}"
            
    return f"# File '{filepath}' not found locally or remote parameters missing."

def compile_dependency_bundle(filepath: str, parsed_diff: dict, symbol_index: dict, dependency_graph: dict) -> str:
    """
    Aggerves code context of imported dependencies to prevent false positives.
    """
    # Look up dependencies associated with the current file path
    dependencies = dependency_graph.get(filepath, {}).get("imports", [])
    if not dependencies:
        return ""
        
    bundle_lines = ["\n--- DEPENDENT CONTEXT LOG ---"]
    for dep in dependencies:
        # Check if the imported dependency is indexed in our codebase
        if dep in symbol_index:
            meta = symbol_index[dep]
            dep_file = meta.get("file_path")
            lines_range = meta.get("lines", [1, 20])
            
            content = get_symbol_signature_or_content(dep_file, lines_range)
            bundle_lines.append(f"\nImported symbol '{dep}' definition from '{dep_file}':")
            bundle_lines.append(content)
            
    return "\n".join(bundle_lines)