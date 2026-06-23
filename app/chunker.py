import os
import requests
from app.config import get_config

def get_symbol_signature_or_content(filepath: str, line_range: list, fallback_only: bool = False) -> str:
    """
    Fetches the content or signature of a symbol. Falls back to GitHub if the VM has no local checkout.
    """
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
            
    from app.llm_client import _context
    owner = _context.get("owner")
    repo = _context.get("repo")
    token = _context.get("token")
    ref = _context.get("commit_sha")
    
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

def get_recursive_dependencies(filepath: str, dependency_graph: dict, max_hops: int = 2) -> set:
    """
    Traverses dependency trees recursively (BFS) to gather transitive dependencies (A -> B -> C).
    If max_hops is set to -1, it traverses all connected components (unlimited depth).
    """
    resolved_files = set()
    queue = [(filepath, 0)]
    visited = {filepath}
    
    while queue:
        curr_file, hop = queue.pop(0)
        
        # Enforce BFS boundary checks unless max_hops is -1 (unlimited)
        if max_hops != -1 and hop >= max_hops:
            continue
            
        imports = dependency_graph.get(curr_file, {}).get("imports", [])
        for imp in imports:
            # Map import path notations (e.g. app.database) to actual file paths (e.g. app/database.py)
            imp_file = imp.replace(".", "/") + ".py"
            if imp_file in dependency_graph and imp_file not in visited:
                visited.add(imp_file)
                resolved_files.add(imp_file)
                queue.append((imp_file, hop + 1))
                
    return resolved_files

def compile_dependency_bundle(filepath: str, parsed_diff: dict, symbol_index: dict, dependency_graph: dict, max_hops: int = 2) -> str:
    """
    Aggregates transitive definitions up to N hops, switching to fallback mode (signatures only) if budget bounds approach.
    """
    config = get_config()
    token_budget = config.get("DEP_TOKEN_BUDGET", 1500)
    
    bundle_lines = ["\n--- TRANSITIVE MODULE CONTEXT LOG ---"]
    
    # Run BFS traversal to resolve transitive modules
    transitive_deps = get_recursive_dependencies(filepath, dependency_graph, max_hops=max_hops)
    
    accumulated_chars = sum(len(x) for x in bundle_lines)
    fallback_mode = False
    
    for dep_file in transitive_deps:
        matching_symbols = [name for name, info in symbol_index.items() if info["file_path"] == dep_file]
        
        for sym_name in matching_symbols:
            meta = symbol_index[sym_name]
            
            content = get_symbol_signature_or_content(dep_file, meta["line_range"], fallback_only=fallback_mode)
            payload = f"\nDependency definition for '{sym_name}' (inside '{dep_file}'):\n{content}"
            
            # Simple token estimation rule (characters divided by 4)
            if (accumulated_chars + len(payload)) / 4 > token_budget:
                fallback_mode = True
                content = get_symbol_signature_or_content(dep_file, meta["line_range"], fallback_only=True)
                payload = f"\nDependency definition Signature for '{sym_name}' (inside '{dep_file}'):\n{content}"
                
            bundle_lines.append(payload)
            accumulated_chars += len(payload)
            
    if len(bundle_lines) <= 1:
        return ""
        
    return "\n".join(bundle_lines)