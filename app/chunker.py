import os
import requests
from app.config import get_config, is_debug_mode

def get_symbol_signature_or_content(filepath: str, line_range: list, fallback_only: bool = False) -> str:
    """
    Fetches the signature or source segment of a symbol. Resolves relative paths
    against the checked out workspace directory to prevent falling back to GitHub API.
    """
    from app.llm_client import _context
    workspace = _context.get("workspace")
    
    # Resolve relative paths against the checked out workspace directory
    local_path = os.path.join(workspace, filepath) if (workspace and not os.path.isabs(filepath)) else filepath
    
    if os.path.exists(local_path):
        try:
            with open(local_path, "r", encoding="utf-8", errors="ignore") as f:
                lines = f.readlines()
            start = max(0, line_range[0] - 1)
            end = min(len(lines), line_range[1])
            symbol_lines = lines[start:end]
            if fallback_only:
                return symbol_lines[0].strip() if symbol_lines else ""
            return "".join(symbol_lines)
        except Exception:
            pass
            
    # Fallback to remote API only if local checkout file is missing
    owner, repo, token, ref = _context.get("owner"), _context.get("repo"), _context.get("token"), _context.get("commit_sha")
    if owner and repo and token and ref:
        url = f"https://api.github.com/repos/{owner}/{repo}/contents/{filepath}"
        headers = {"Authorization": f"Bearer {token}", "Accept": "application/vnd.github.v3.raw"}
        try:
            response = requests.get(url, headers=headers, params={"ref": ref})
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

def get_symbol_neighbors_local(db_path: str, qual_name: str) -> list:
    """
    Query-driven helper to fetch direct caller, callee, and dependency targets 
    from the SQLite database, avoiding circular imports.
    """
    if not os.path.exists(db_path):
        return []
    try:
        import sqlite3
        conn = sqlite3.connect(db_path)
        cursor = conn.cursor()
        cursor.execute("""
            SELECT DISTINCT target_qualified FROM edges 
            WHERE source_qualified = ? AND kind IN ('CALLS', 'DEPENDS_ON', 'IMPORTS_FROM')
            UNION
            SELECT DISTINCT source_qualified FROM edges 
            WHERE target_qualified = ? AND kind IN ('CALLS', 'DEPENDS_ON')
        """, (qual_name, qual_name))
        rows = [r[0] for r in cursor.fetchall() if r[0]]
        conn.close()
        return rows
    except Exception:
        return []

def get_symbol_neighbors_bfs(db_path: str, seed_symbols: list, max_hops: int = 1) -> set:
    """
    BFS outward from seed symbols up to max_hops edges. Returns newly
    discovered symbols only (seeds excluded).
    """
    max_hops = max(1, int(max_hops or 1))
    visited = set(seed_symbols)
    frontier = set(seed_symbols)
    discovered = set()
    for _ in range(max_hops):
        next_frontier = set()
        for sym in frontier:
            for neighbor in get_symbol_neighbors_local(db_path, sym):
                if neighbor not in visited:
                    visited.add(neighbor)
                    next_frontier.add(neighbor)
                    discovered.add(neighbor)
        if not next_frontier:
            break
        frontier = next_frontier
    return discovered

def compile_dependency_bundle(filepath: str, parsed_diff: dict, symbol_index: dict, dependency_graph: dict, max_hops: int = 1) -> str:
    config = get_config()
    token_budget = config.get("DEP_TOKEN_BUDGET", 5000)
    
    # Reconstruct the file's diff text safely to verify references
    diff_text = " ".join(
        list(parsed_diff.get("added_lines", {}).values()) + 
        list(parsed_diff.get("context_lines", {}).values())
    )
    
    # Find all symbols defined inside the file under review
    local_symbols = [name for name, info in symbol_index.items() if info["file_path"] == filepath]
    if not local_symbols:
        return ""
        
    retrieved_symbols = set()
    bundle_lines = ["\n--- TRANSITIVE MODULE CONTEXT LOG ---"]
    accumulated_chars = sum(len(x) for x in bundle_lines)
    fallback_mode = False
    
    from app.llm_client import _context
    db_path = os.path.join(os.path.abspath(_context.get("workspace") or "."), ".code-review-graph", "graph.db")
    
    # BFS lookup for neighbors up to max_hops
    candidate_neighbors = get_symbol_neighbors_bfs(db_path, local_symbols, max_hops=max_hops)
    
    for neighbor_sym in candidate_neighbors:
        if (accumulated_chars / 4) >= token_budget:
            break
            
        if neighbor_sym in retrieved_symbols or neighbor_sym in local_symbols:
            continue
            
        # Filter: only pull the symbol's body if it is referenced inside the diff text
        simple_name = neighbor_sym.split("::")[-1]
        if simple_name not in diff_text:
            continue
            
        if neighbor_sym in symbol_index:
            retrieved_symbols.add(neighbor_sym)
            meta = symbol_index[neighbor_sym]
            line_range = meta.get("line_range", [1, 20])
            content = get_symbol_signature_or_content(meta["file_path"], line_range, fallback_only=fallback_mode)
            
            if (accumulated_chars + len(content)) / 4 > token_budget:
                fallback_mode = True
                content = get_symbol_signature_or_content(meta["file_path"], line_range, fallback_only=True)
                payload = f"\nDependency Signature '{neighbor_sym}' ({line_range[0]}-{line_range[1]}):\n{content}"
            else:
                payload = f"\nDependency '{neighbor_sym}' ({line_range[0]}-{line_range[1]}):\n{content}"
            bundle_lines.append(payload)
            accumulated_chars += len(payload)
                
    if is_debug_mode():
        print(f"[DEBUG] [Chunker] Context compile complete. Size: {accumulated_chars} chars (~{int(accumulated_chars/4)} tokens).")
    return "\n".join(bundle_lines) if len(bundle_lines) > 1 else ""
