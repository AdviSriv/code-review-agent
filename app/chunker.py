import os
import requests
from app.config import get_config, is_debug_mode

def get_symbol_signature_or_content(filepath: str, line_range: list, fallback_only: bool = False) -> str:
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

def get_recursive_dependencies(filepath: str, dependency_graph: dict, max_hops: int = 1) -> set:
    resolved_files = set()
    if is_debug_mode():
        print(f"[DEBUG] [Chunker] Resolving dependencies (Max Hops: {max_hops})")
        
    if dependency_graph:
        queue = [(filepath, 0)]
        visited = {filepath}
        while queue:
            curr_file, hop = queue.pop(0)
            if max_hops != -1 and hop >= max_hops:
                continue
            imports = dependency_graph.get(curr_file, {}).get("imports", [])
            for imp in imports:
                imp_file = imp.replace(".", "/") + ".py" if ("." in imp or not imp.endswith(".py")) else imp
                if imp_file in dependency_graph and imp_file not in visited:
                    visited.add(imp_file)
                    if imp_file != filepath:
                        resolved_files.add(imp_file)
                    queue.append((imp_file, hop + 1))
        return resolved_files

    from app.llm_client import _context
    workspace_abs = os.path.abspath(_context.get("workspace") or ".")
    db_path = os.path.join(workspace_abs, ".code-review-graph", "graph.db")
    if os.path.exists(db_path):
        try:
            import sqlite3
            conn = sqlite3.connect(db_path)
            cursor = conn.cursor()
            filepath_abs = os.path.abspath(os.path.join(workspace_abs, filepath)).replace("\\", "/")
            queue = [(filepath_abs, 0)]
            visited = {filepath_abs}
            while queue:
                curr_target, hop = queue.pop(0)
                if max_hops != -1 and hop >= max_hops:
                    continue
                cursor.execute("""
                    SELECT DISTINCT e.target_qualified, n.file_path FROM edges e
                    JOIN nodes n ON n.qualified_name = e.target_qualified
                    WHERE (e.source_qualified = ? OR e.file_path = ?) AND e.kind = 'IMPORTS_FROM' AND n.kind = 'File'
                    UNION
                    SELECT DISTINCT e.target_qualified, n.file_path FROM edges e
                    JOIN nodes n ON n.file_path = e.target_qualified
                    WHERE (e.source_qualified = ? OR e.file_path = ?) AND e.kind = 'IMPORTS_FROM' AND n.kind = 'File'
                """, (curr_target, curr_target, curr_target, curr_target))
                for row in cursor.fetchall():
                    t_qual, t_file = row
                    if t_qual and t_qual not in visited:
                        visited.add(t_qual)
                        queue.append((t_qual, hop + 1))
                    if t_file and t_file not in visited:
                        visited.add(t_file)
                        queue.append((t_file, hop + 1))
                    if t_file and t_file != filepath_abs:
                        resolved_files.add(os.path.relpath(t_file, workspace_abs).replace("\\", "/"))
            conn.close()
        except Exception as e:
            if is_debug_mode():
                print(f"[DEBUG] [Chunker] SQLite fallback failed: {e}")
    return resolved_files

def compile_dependency_bundle(filepath: str, parsed_diff: dict, symbol_index: dict, dependency_graph: dict, max_hops: int = 1) -> str:
    config = get_config()
    token_budget = config.get("DEP_TOKEN_BUDGET", 5000)
    bundle_lines = ["\n--- TRANSITIVE MODULE CONTEXT LOG ---"]
    transitive_deps = get_recursive_dependencies(filepath, dependency_graph, max_hops=max_hops)
    accumulated_chars = sum(len(x) for x in bundle_lines)
    fallback_mode = False
    
    for dep_file in transitive_deps:
        # Exit the outer dependency-file loop immediately if the budget ceiling is breached
        if (accumulated_chars / 4) >= token_budget:
            break
        matching_symbols = [name for name, info in symbol_index.items() if info["file_path"] == dep_file]
        for sym_name in matching_symbols:
            # Exit the inner symbol loop immediately if the budget ceiling is breached
            if (accumulated_chars / 4) >= token_budget:
                break
            meta = symbol_index[sym_name]
            line_range = meta.get("line_range", [1, 20])
            content = get_symbol_signature_or_content(dep_file, line_range, fallback_only=fallback_mode)
            
            if (accumulated_chars + len(content)) / 4 > token_budget:
                fallback_mode = True
                content = get_symbol_signature_or_content(dep_file, line_range, fallback_only=True)
                payload = f"\nDependency definition Signature for '{sym_name}' (inside '{dep_file}' on lines {line_range[0]}-{line_range[1]}):\n{content}"
            else:
                payload = f"\nDependency definition for '{sym_name}' (inside '{dep_file}' on lines {line_range[0]}-{line_range[1]}):\n{content}"
            bundle_lines.append(payload)
            accumulated_chars += len(payload)
            
    if is_debug_mode():
        print(f"[DEBUG] [Chunker] Context compile complete. Size: {accumulated_chars} chars (~{int(accumulated_chars/4)} tokens).")
    return "\n".join(bundle_lines) if len(bundle_lines) > 1 else ""