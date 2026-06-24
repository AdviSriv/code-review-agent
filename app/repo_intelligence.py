import os
import ast
import json
import argparse
from pathlib import Path

INTEL_DIR = Path.home() / ".config" / "code_review_agent" / "repo_intel"

class RepoIndexer(ast.NodeVisitor):
    def __init__(self, filepath: str):
        self.filepath = filepath
        self.symbols = []
        self.imports = []

    def visit_ClassDef(self, node):
        self.symbols.append({
            "name": node.name,
            "type": "class",
            "file_path": self.filepath,
            "line_range": [node.lineno, getattr(node, "end_lineno", node.lineno)]
        })
        self.generic_visit(node)

    def visit_FunctionDef(self, node):
        self.symbols.append({
            "name": node.name,
            "type": "function",
            "file_path": self.filepath,
            "line_range": [node.lineno, getattr(node, "end_lineno", node.lineno)]
        })
        self.generic_visit(node)

    def visit_AsyncFunctionDef(self, node):
        self.visit_FunctionDef(node)

    def visit_Import(self, node):
        for name in node.names:
            self.imports.append(name.name)
        self.generic_visit(node)

    def visit_ImportFrom(self, node):
        # Handle relative imports (from .ctx import AppContext or from . import cli)
        if node.level and node.level > 0:
            parts = self.filepath.replace("\\", "/").split("/")
            # Verify we do not traverse out of the repository bounds
            if len(parts) >= node.level:
                dir_parts = parts[:-node.level]
                if node.module:
                    # e.g., from .ctx -> src/flask/ctx
                    resolved_path = "/".join(dir_parts + [node.module])
                    import_name = resolved_path.replace("/", ".")
                    self.imports.append(import_name)
                else:
                    # e.g., from . import cli -> src/flask/cli
                    for alias in node.names:
                        resolved_path = "/".join(dir_parts + [alias.name])
                        import_name = resolved_path.replace("/", ".")
                        self.imports.append(import_name)
        else:
            # Standard absolute imports (import os, from django.conf import settings)
            if node.module:
                self.imports.append(node.module)
        self.generic_visit(node)

def build_repo_intelligence(repo_dir: str, commit_sha: str) -> str:
    symbol_index = {}
    dependency_graph = {}
    
    repo_path = Path(repo_dir).resolve()
    
    for root, _, files in os.walk(repo_path):
        for file in files:
            if not file.endswith(".py"):
                continue
                
            full_path = Path(root) / file
            rel_path = str(full_path.relative_to(repo_path))
            
            if any(p in f"/{rel_path}/" for p in ["/.venv/", "/node_modules/", "/.git/", "/build/"]):
                continue
                
            try:
                with open(full_path, "r", encoding="utf-8", errors="ignore") as f:
                    tree = ast.parse(f.read(), filename=str(full_path))
                
                indexer = RepoIndexer(rel_path)
                indexer.visit(tree)
                
                for sym in indexer.symbols:
                    symbol_index[sym["name"]] = {
                        "file_path": sym["file_path"],
                        "line_range": sym["line_range"],
                        "type": sym["type"]
                    }
                
                dependency_graph[rel_path] = {
                    "imports": indexer.imports,
                    "imported_by": []
                }
            except Exception as e:
                print(f"Failed parsing file: {rel_path}. Error: {e}")

    # Prevent writing empty index files if the clone directory was empty/failed
    if not symbol_index and not dependency_graph:
        print("[RepoIntel] Warning: No source files located. Aborting write to prevent caching corrupt empty index.")
        return ""

    for filepath, data in list(dependency_graph.items()):
        for imp in data["imports"]:
            imp_path = imp.replace(".", "/") + ".py"
            for potential_match in dependency_graph.keys():
                if potential_match.endswith(imp_path):
                    dependency_graph[potential_match]["imported_by"].append(filepath)
                    break

    INTEL_DIR.mkdir(parents=True, exist_ok=True)
    out_file = INTEL_DIR / f"{commit_sha}.json"
    
    payload = {
        "commit_sha": commit_sha,
        "symbol_index": symbol_index,
        "dependency_graph": dependency_graph
    }
    
    with open(out_file, "w") as f:
        json.dump(payload, f, indent=4)
        
    print(f"Repo Intelligence generated successfully for Commit: {commit_sha}")
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