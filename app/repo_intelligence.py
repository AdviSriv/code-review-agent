import os
import json
import argparse
import sqlite3
import subprocess
import shutil
from pathlib import Path
from app.config import is_debug_mode
from app.profiler import PipelineProfiler

INTEL_DIR = Path.home() / ".config" / "code_review_agent" / "repo_intel"

def make_rel(path_str: str, base_dir: str) -> str:
    """
    Safely converts an absolute path string to a relative path string 
    relative to base_dir, normalizing all slashes.
    """
    if not path_str:
        return ""
    try:
        rel = os.path.relpath(path_str, base_dir)
        return rel.replace("\\", "/")
    except ValueError:
        return path_str.replace("\\", "/")

def build_repo_intelligence(repo_dir: str, commit_sha: str) -> str:
    """
    Invokes code-review-graph (CRG) to index the repository. Leverages sub-second 
    incremental updates if a database is already present.
    """
    profiler = PipelineProfiler()
    repo_path = Path(repo_dir).resolve()
    repo_path_str = str(repo_path)
    db_path = repo_path / ".code-review-graph" / "graph.db"
    
    print(f"[RepoIntel] Building repository intelligence for Commit: {commit_sha} inside {repo_path}")
    
    stdout_dest = None if is_debug_mode() else subprocess.PIPE
    stderr_dest = None if is_debug_mode() else subprocess.PIPE
    
    # Profile external CLI tool executions
    profiler.start("RepoIntel: code-review-graph CLI")
    try:
        if db_path.exists():
            print(f"[RepoIntel] Database found at {db_path}. Executing fast incremental update...", flush=True)
            res = subprocess.run(
                ["code-review-graph", "update"], 
                cwd=repo_path_str, 
                stdout=stdout_dest, 
                stderr=stderr_dest, 
                text=True
            )
        else:
            print(f"[RepoIntel] No database found. Executing full codebase build...", flush=True)
            res = subprocess.run(
                ["code-review-graph", "build"], 
                cwd=repo_path_str, 
                stdout=stdout_dest, 
                stderr=stderr_dest, 
                text=True
            )
            
        if res.returncode != 0:
            print(f"[RepoIntel] code-review-graph command failed with exit code: {res.returncode}")
    except Exception as e:
        print(f"[RepoIntel] Failed to execute code-review-graph CLI operation: {e}")
        profiler.stop("RepoIntel: code-review-graph CLI")
        return ""
    profiler.stop("RepoIntel: code-review-graph CLI")

    if not db_path.exists():
        print(f"[RepoIntel] Error: SQLite graph database not found at {db_path}")
        return ""

    symbol_index = {}
    dependency_graph = {}

    # Profile parsing the AST graph tables inside SQLite
    profiler.start("RepoIntel: DB Node/Edge Parsing")
    try:
        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()

        cursor.execute("""
            SELECT name, qualified_name, file_path, line_start, line_end, kind 
            FROM nodes 
            WHERE kind != 'File'
        """)
        nodes = cursor.fetchall()
        for node in nodes:
            line_range = [node["line_start"] or 1, node["line_end"] or 1]
            rel_file = make_rel(node["file_path"], repo_path_str)
            
            qual_name = node["qualified_name"]
            if "::" in qual_name:
                parts = qual_name.split("::", 1)
                rel_part = make_rel(parts[0], repo_path_str)
                qual_name = f"{rel_part}::{parts[1]}"
            else:
                qual_name = make_rel(qual_name, repo_path_str)
                
            symbol_meta = {
                "file_path": rel_file,
                "line_range": line_range,
                "type": node["kind"].lower()
            }
            symbol_index[qual_name] = symbol_meta

        cursor.execute("SELECT DISTINCT file_path FROM nodes WHERE file_path IS NOT NULL")
        files = cursor.fetchall()
        for f in files:
            rel_f = make_rel(f["file_path"], repo_path_str)
            dependency_graph[rel_f] = {
                "imports": [],
                "imported_by": []
            }

        node_to_file = {}
        cursor.execute("SELECT qualified_name, file_path FROM nodes WHERE file_path IS NOT NULL")
        for row in cursor.fetchall():
            node_to_file[row["qualified_name"]] = row["file_path"]

        cursor.execute("""
            SELECT 
                e.source_qualified,
                e.target_qualified,
                n_src.file_path AS src_file,
                n_tgt.file_path AS tgt_file
            FROM edges e
            LEFT JOIN nodes n_src ON e.source_qualified = n_src.qualified_name
            LEFT JOIN nodes n_tgt ON e.target_qualified = n_tgt.qualified_name
            WHERE e.kind IN ('IMPORTS_FROM', 'DEPENDS_ON')
        """)
        edges = cursor.fetchall()
        for edge in edges:
            src_file_abs = edge["src_file"] or node_to_file.get(edge["source_qualified"])
            tgt_file_abs = edge["tgt_file"] or node_to_file.get(edge["target_qualified"])

            if src_file_abs and tgt_file_abs:
                src_file = make_rel(src_file_abs, repo_path_str)
                tgt_file = make_rel(tgt_file_abs, repo_path_str)
                
                if src_file != tgt_file:
                    dependency_graph.setdefault(src_file, {"imports": [], "imported_by": []})
                    dependency_graph.setdefault(tgt_file, {"imports": [], "imported_by": []})

                    if tgt_file not in dependency_graph[src_file]["imports"]:
                        dependency_graph[src_file]["imports"].append(tgt_file)
                    if src_file not in dependency_graph[tgt_file]["imported_by"]:
                        dependency_graph[tgt_file]["imported_by"].append(src_file)

                    if tgt_file.endswith(".py"):
                        dot_tgt = tgt_file[:-3].replace("/", ".")
                    else:
                        dot_tgt = tgt_file.replace("/", ".")
                        
                    if src_file.endswith(".py"):
                        dot_src = src_file[:-3].replace("/", ".")
                    else:
                        dot_src = src_file.replace("/", ".")

                    if dot_tgt not in dependency_graph[src_file]["imports"]:
                        dependency_graph[src_file]["imports"].append(dot_tgt)
                    if dot_src not in dependency_graph[tgt_file]["imported_by"]:
                        dependency_graph[tgt_file]["imported_by"].append(dot_src)

        conn.close()
    except Exception as e:
        print(f"[RepoIntel] Failed to process database nodes and edges: {e}")
        profiler.stop("RepoIntel: DB Node/Edge Parsing")
        return ""
    profiler.stop("RepoIntel: DB Node/Edge Parsing")

    if not symbol_index and not dependency_graph:
        print("[RepoIntel] Warning: Empty symbol index. Aborting write.")
        return ""

    INTEL_DIR.mkdir(parents=True, exist_ok=True)
    out_file = INTEL_DIR / f"{commit_sha}.json"
    
    # Save a permanent copy of the SQLite database inside the cache folder
    cache_db_file = INTEL_DIR / f"{commit_sha}.db"
    if db_path.exists():
        try:
            shutil.copy(str(db_path), str(cache_db_file))
            print(f"[RepoIntel] Saved database copy to {cache_db_file}", flush=True)
        except Exception as e:
            print(f"[RepoIntel] Failed to copy database to cache: {e}", flush=True)
    
    payload = {
        "commit_sha": commit_sha,
        "symbol_index": symbol_index,
        "dependency_graph": dependency_graph
    }
    
    with open(out_file, "w") as f:
        json.dump(payload, f, indent=4)
        
    print(f"Repo Intelligence generated successfully via CRG for Commit: {commit_sha}")
    return str(out_file)

def load_repo_intelligence(commit_sha: str) -> dict:
    intel_file = INTEL_DIR / f"{commit_sha}.json"
    if intel_file.exists():
        try:
            with open(intel_file, "r") as f:
                return json.load(f)
        except Exception:
            pass
    return {"symbol_index": {}, "dependency_graph": {}}

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Build repository-wide static indices.")
    parser.add_argument("--repo-dir", default=".", help="Root of local workspace")
    parser.add_argument("--commit", required=True, help="Target Commit SHA key")
    args = parser.parse_args()
    build_repo_intelligence(args.repo_dir, args.commit)