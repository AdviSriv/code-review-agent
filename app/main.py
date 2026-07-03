# ===== /root/code-review-agent/app/main.py =====
import os
import sys
import argparse
import requests
import json
import threading
import time
import subprocess
import shutil
from pathlib import Path
from http.server import HTTPServer, BaseHTTPRequestHandler
from concurrent.futures import ThreadPoolExecutor

# Config and parsers
from app.config import get_secret, get_repo_pat, set_debug_mode, is_debug_mode
from app.diff_parser import parse_patch, parse_full_diff, is_ignored_file, identify_language
from app.prompt_builder import chunk_file_diffs
import app.llm_client as llm_client
from app.models import CodeComment
from app.output_parser import build_github_review_payload
from app.repo_checks import get_repo_conventions

# Import Phase 2 Modules
from app.repo_intelligence import load_repo_intelligence, build_repo_intelligence
from app.triage import evaluate_docs_only_skip, run_native_compile_check
from app.chunker import compile_dependency_bundle
from app.gatekeeper import RateGatekeeper
from app.orchestrator import (
    orchestrate_chunk_review, 
    orchestrate_symbol_review, 
    SharedContextCache, 
    get_changed_symbols_in_file
)
from app.validator import validate_and_deduplicate_comments
from app.profiler import PipelineProfiler

gatekeeper = RateGatekeeper()

def fetch_repo_metadata(owner: str, repo: str, token: str) -> dict:
    url = f"https://api.github.com/repos/{owner}/{repo}"
    headers = {"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"}
    try:
        response = requests.get(url, headers=headers)
        if response.status_code == 200:
            return response.json()
    except Exception:
        pass
    return {}

def fetch_pr_metadata(owner: str, repo: str, pr_number: str, token: str) -> dict:
    url = f"https://api.github.com/repos/{owner}/{repo}/pulls/{pr_number}"
    headers = {"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"}
    response = requests.get(url, headers=headers)
    if response.status_code != 200:
        raise RuntimeError(f"Failed to fetch PR metadata. API Status: {response.status_code}")
    return response.json()

def fetch_all_pr_files(owner: str, repo: str, pr_number: str, token: str) -> list:
    all_files, page = [], 1
    while True:
        url = f"https://api.github.com/repos/{owner}/{repo}/pulls/{pr_number}/files"
        response = requests.get(url, headers={"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"}, params={"page": page, "per_page": 100})
        if response.status_code != 200:
            raise RuntimeError(f"Failed to fetch PR files. API Status: {response.status_code}")
        data = response.json()
        if not data:
            break
        all_files.extend(data)
        if len(data) < 100:
            break
        page += 1
    return all_files

def post_github_review(owner: str, repo: str, pr_number: str, token: str, commit_id: str, comments: list, body_summary: str):
    url = f"https://api.github.com/repos/{owner}/{repo}/pulls/{pr_number}/reviews"
    headers = {"Authorization": f"Bearer {token}", "Accept": "application/vnd.github.v3+json", "Content-Type": "application/json"}
    if not comments:
        response = requests.post(url, headers=headers, json={"commit_id": commit_id, "event": "COMMENT", "body": body_summary, "comments": []})
        if response.status_code not in [200, 201]:
            print(f"Failed to post review summary. Status: {response.status_code}")
        return
    comment_batches = [comments[i:i + 8] for i in range(0, len(comments), 8)]
    for idx, batch in enumerate(comment_batches, 1):
        batch_body = body_summary if idx == 1 else f"### 🛡️ Phase 2 Inline Code Review [Batch {idx}/{len(comment_batches)}]"
        response = requests.post(url, headers=headers, json={"commit_id": commit_id, "event": "COMMENT", "body": batch_body, "comments": batch})
        if response.status_code not in [200, 201]:
            print(f"Failed to post batch {idx}. Status: {response.status_code}")
        if idx < len(comment_batches):
            time.sleep(2.0)

def ensure_local_checkout(owner: str, repo_name: str, commit_sha: str, token: str) -> str:
    cache_dir = Path.home() / ".cache" / "code_review_agent" / "repos" / owner / repo_name
    workspace_path = str(cache_dir)
    if cache_dir.exists() and not (cache_dir / ".git").exists():
        try:
            shutil.rmtree(cache_dir)
        except Exception:
            pass
    cache_dir.mkdir(parents=True, exist_ok=True)
    if not (cache_dir / ".git").exists():
        clone_url = f"https://{token}@github.com/{owner}/{repo_name}.git"
        res = subprocess.run(["git", "clone", clone_url, workspace_path], capture_output=True, text=True)
        if res.returncode != 0:
            shutil.rmtree(cache_dir, ignore_errors=True)
            cache_dir.mkdir(parents=True, exist_ok=True)
            subprocess.run(["git", "clone", f"https://github.com/{owner}/{repo_name}.git", workspace_path], check=True)
    else:
        subprocess.run(["git", "reset", "--hard"], cwd=workspace_path, capture_output=True)
        subprocess.run(["git", "clean", "-fd", "-e", ".code-review-graph"], cwd=workspace_path, capture_output=True)
        subprocess.run(["git", "fetch", "origin"], cwd=workspace_path, capture_output=True)
    res = subprocess.run(["git", "checkout", commit_sha], cwd=workspace_path, capture_output=True, text=True)
    if res.returncode != 0:
        subprocess.run(["git", "fetch", "--unshallow"], cwd=workspace_path, capture_output=True)
        subprocess.run(["git", "checkout", commit_sha], cwd=workspace_path, check=True)
    return workspace_path

def run_review_pipeline(repo_full_name: str, pr_number: str, dep_hops: int = 1, max_escalation_rounds: int = 1):
    print(f"\n[Pipeline] Initializing Review for {repo_full_name} PR #{pr_number}", flush=True)
    start_time = time.time()
    
    profiler = PipelineProfiler()
    profiler.start("Pipeline: Full Process Execution")
    
    try:
        # Reset token telemetry variables once per PR pipeline execution
        llm_client.reset_token_stats()

        token = get_repo_pat(repo_full_name)
        if not token:
            print(f"[Pipeline] Error: GITHUB_PAT not configured.", flush=True)
            return
        owner, repo = repo_full_name.split("/")
        
        # Profile repo metadata parsing
        profiler.start("GitHub API: Retrieve PR/Repository Metadata")
        repo_meta = fetch_repo_metadata(owner, repo, token)
        repo_size_kb = repo_meta.get("size", 0)
        pr_data = fetch_pr_metadata(owner, repo, pr_number, token)
        if pr_data.get("draft") is True:
            profiler.stop("GitHub API: Retrieve PR/Repository Metadata")
            profiler.stop("Pipeline: Full Process Execution")
            return
        commit_id = pr_data.get("head", {}).get("sha")
        base_sha = pr_data.get("base", {}).get("sha")
        pr_files = fetch_all_pr_files(owner, repo, pr_number, token)
        profiler.stop("GitHub API: Retrieve PR/Repository Metadata")
        
        # Profile Git Checkout of Head Commit
        profiler.start("Git Checkout: HEAD Commit Retrieval")
        workspace_path = ensure_local_checkout(owner, repo, commit_id, token)
        profiler.stop("Git Checkout: HEAD Commit Retrieval")
        
        # Populate context keys cleanly
        llm_client._context.update({
            "owner": owner, 
            "repo": repo, 
            "token": token, 
            "commit_sha": commit_id, 
            "workspace": workspace_path,
            "base_sha": base_sha
        })
        llm_client._gatekeeper_ref = gatekeeper
        
        # Fetch / build the HEAD commit index
        profiler.start("RepoIntel Build: Head Commit AST Index")
        intel_data = load_repo_intelligence(commit_id)
        if not intel_data.get("symbol_index") or not intel_data.get("dependency_graph"):
            print(f"[RepoIntel] Index for HEAD commit '{commit_id}' is missing. Generating...", flush=True)
            build_repo_intelligence(workspace_path, commit_id)
            intel_data = load_repo_intelligence(commit_id)
        profiler.stop("RepoIntel Build: Head Commit AST Index")
            
        # Also ensure base_sha database is indexed so we can query base callers during deletion escalations
        profiler.start("RepoIntel Build: Base Commit AST Index")
        base_intel = load_repo_intelligence(base_sha)
        if not base_intel.get("symbol_index") or not base_intel.get("dependency_graph"):
            print(f"[RepoIntel] Index for BASE commit '{base_sha}' is missing. Generating...", flush=True)
            ensure_local_checkout(owner, repo, base_sha, token)
            build_repo_intelligence(workspace_path, base_sha)
            ensure_local_checkout(owner, repo, commit_id, token)
        profiler.stop("RepoIntel Build: Base Commit AST Index")
            
        symbol_index = intel_data.get("symbol_index", {})
        dependency_graph = intel_data.get("dependency_graph", {})
        changed_file_paths = [os.path.join(workspace_path, f.get("filename")) for f in pr_files]
        
        if evaluate_docs_only_skip(changed_file_paths):
            post_github_review(owner, repo, pr_number, token, commit_id, [], "### ℹ️ Review Skipped\nThis PR contains docs only.")
            profiler.stop("Pipeline: Full Process Execution")
            return
            
        # Profile compilation step using explicit thread-safe cwd pathways (no os.chdir)
        profiler.start("Triage validation: Local Compilers & Linters")
        compile_status = run_native_compile_check(changed_file_paths, workspace_path)
        profiler.stop("Triage validation: Local Compilers & Linters")
        
        if not compile_status["passed"]:
            post_github_review(owner, repo, pr_number, token, commit_id, [], compile_status["error_msg"])
            profiler.stop("Pipeline: Full Process Execution")
            return
            
        conventions = get_repo_conventions(owner, repo, token)
        db_path = os.path.join(workspace_path, ".code-review-graph", "graph.db")
        
        # Instantiate shared cache context
        cache = SharedContextCache(workspace_path)
        review_tasks = []
        total_diff_lines = 0
        
        # 1. Map changed files to task payloads
        for f in pr_files:
            filename = f.get("filename")
            patch = f.get("patch")
            if is_ignored_file(filename) or not patch:
                continue
                
            total_diff_lines += len(patch.splitlines())
            lang = identify_language(filename)
            parsed_diff = parse_patch(patch)
            
            filepath_abs = os.path.abspath(os.path.join(workspace_path, filename)).replace("\\", "/")
            modified_lines = list(parsed_diff.get("added_lines", {}).keys())
            
            changed_symbols = get_changed_symbols_in_file(db_path, filepath_abs, modified_lines)
            
            if changed_symbols:
                for sym in changed_symbols:
                    review_tasks.append({
                        "type": "symbol",
                        "filename": filename,
                        "parsed_diff": parsed_diff,
                        "sym": sym
                    })
            else:
                chunks = chunk_file_diffs(filename, lang, parsed_diff)
                for chunk in chunks:
                    dep_bundle = compile_dependency_bundle(filename, parsed_diff, symbol_index, dependency_graph, max_hops=dep_hops)
                    payload = f"{chunk}\n\n{dep_bundle}"
                    review_tasks.append({
                        "type": "chunk",
                        "filename": filename,
                        "parsed_diff": parsed_diff,
                        "payload": payload
                    })

        # 2. Worker task executor
        def execute_review_task(task):
            filename = task["filename"]
            parsed_diff = task["parsed_diff"]
            
            if task["type"] == "symbol":
                sym = task["sym"]
                print(f"[Pipeline] Analysing symbol: '{sym['qualified_name']}' in '{filename}'...", flush=True)
                symbol_comments = orchestrate_symbol_review(
                    sym, symbol_index, conventions, cache, db_path,
                    dep_hops=dep_hops, max_escalation_rounds=max_escalation_rounds
                )
                return validate_and_deduplicate_comments(symbol_comments, filename, parsed_diff)
            else:
                payload = task["payload"]
                print(f"[Pipeline] Analysing file chunks: '{filename}'...", flush=True)
                chunk_comments = orchestrate_chunk_review(
                    payload, conventions, symbol_index, db_path, cache,
                    max_escalation_rounds=max_escalation_rounds
                )
                return validate_and_deduplicate_comments(chunk_comments, filename, parsed_diff)

        # 3. Parallelize the reviews across thread pools safely protected by central RateGatekeeper
        all_comments = []
        if review_tasks:
            profiler.start("Pipeline: Parallel LLM Task Execution")
            with ThreadPoolExecutor(max_workers=min(len(review_tasks), 4)) as executor:
                task_results = executor.map(execute_review_task, review_tasks)
                for res in task_results:
                    all_comments.extend(res)
            profiler.stop("Pipeline: Parallel LLM Task Execution")

        gh_comments = build_github_review_payload(all_comments)
        summary_body = "### 🛡️ Phase 2 Code Review Complete\n\nAll findings have been batched and mapped directly to lines."
                
        if gh_comments:
            profiler.start("GitHub API: Post review comments payload")
            post_github_review(owner, repo, pr_number, token, commit_id, gh_comments, summary_body)
            profiler.stop("GitHub API: Post review comments payload")
            
        elapsed_time = time.time() - start_time
        profiler.stop("Pipeline: Full Process Execution")
        
        profiler.report()
        
        print("\n================== PERFORMANCE METRICS ==================", flush=True)
        print(f"Repository size: {repo_size_kb:,} KB" if repo_size_kb else "Repository size: Unknown", flush=True)
        print(f"PR Diff size: {total_diff_lines:,} total lines", flush=True)
        print(f"Execution Latency: {elapsed_time:.2f} seconds", flush=True)
        print(f"Tokens consumed:\n  - Input: {llm_client.token_stats['prompt_tokens']:,}\n  - Output: {llm_client.token_stats['candidates_tokens']:,}\n  - Total: {llm_client.token_stats['total_tokens']:,}", flush=True)
        print("=========================================================\n", flush=True)
    except Exception as e:
        profiler.stop("Pipeline: Full Process Execution")
        print(f"[Pipeline] Crash: {e}", flush=True)

class WebhookHandler(BaseHTTPRequestHandler):
    def do_POST(self):
        print(f"\n[Webhook] Received incoming POST request on '{self.path}'", flush=True)
        
        if self.path != "/webhook":
            print(f"[Webhook] Rejected: Path is not '/webhook' (received '{self.path}')", flush=True)
            self.send_response(404)
            self.end_headers()
            return
            
        event_type = self.headers.get('X-GitHub-Event', '')
        print(f"[Webhook] GitHub Event Header ('X-GitHub-Event'): '{event_type}'", flush=True)
        
        post_data = self.rfile.read(int(self.headers.get('Content-Length', 0) or 0))
        try:
            payload = json.loads(post_data.decode('utf-8'))
        except Exception as e:
            print(f"[Webhook] Error: Failed to parse JSON payload: {e}", flush=True)
            self.send_response(400)
            self.end_headers()
            return

        if event_type == 'ping':
            print("[Webhook] Received 'ping' event from GitHub. Connection is healthy!", flush=True)
            self.send_response(200)
            self.end_headers()
            return

        if event_type != 'pull_request':
            print(f"[Webhook] Ignored: Event is not 'pull_request' (received '{event_type}')", flush=True)
            self.send_response(200)
            self.end_headers()
            return
            
        action = payload.get("action")
        is_draft = payload.get("pull_request", {}).get("draft", False)
        print(f"[Webhook] Pull Request Action parsed correctly: '{action}', Draft: {is_draft}", flush=True)
        
        if action not in ["opened", "synchronize", "ready_for_review"] or is_draft is True:
            print(f"[Webhook] Ignored: Action '{action}' is not tracked or PR is currently in draft.", flush=True)
            self.send_response(200)
            self.end_headers()
            return
            
        repo_full_name = payload.get("repository", {}).get("full_name")
        pr_number = payload.get("number")
        if repo_full_name and pr_number:
            print(f"[Webhook] Triggering PR review pipeline for '{repo_full_name}' PR #{pr_number}", flush=True)
            self.send_response(202)
            self.end_headers()
            threading.Thread(target=run_review_pipeline, args=(repo_full_name, str(pr_number), server_dep_hops, server_lines_limit)).start()
        else:
            print(f"[Webhook] Bad Request: Repository name or PR number missing.", flush=True)
            self.send_response(400)
            self.end_headers()

server_dep_hops, server_lines_limit = 1, 1  # Default to 1-hop and 1-round limits

def main():
    global server_dep_hops, server_lines_limit
    parser = argparse.ArgumentParser(description="Phase 2 Code Review Agent")
    parser.add_argument("--repo")
    parser.add_argument("--pr")
    parser.add_argument("--diff-file")
    parser.add_argument("--server", action="store_true")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--build-index", action="store_true")
    parser.add_argument("--commit")
    parser.add_argument("--dep-hops", type=int, default=1)  # Default to 1-hop for free tier limitations
    parser.add_argument("--max-context-calls", type=int, default=1) # Max additional escalation rounds a subagent may request (default 1)
    parser.add_argument("--debug", action="store_true")
    args = parser.parse_args()
    
    if args.build_index:
        if not args.commit:
            sys.exit(1)
        build_repo_intelligence(".", args.commit)
        return
    server_dep_hops = args.dep_hops
    server_lines_limit = args.max_context_calls
    if args.debug:
        set_debug_mode(True)
    if args.server:
        server = HTTPServer(('0.0.0.0', args.port), WebhookHandler)
        print(f"Webhook Receiver active on port {args.port} (Hops: {server_dep_hops}, Context Calls: {server_lines_limit})...", flush=True)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            print("Server shutting down.", flush=True)
        return
    if args.diff_file:
        start_time = time.time()
        try:
            with open(args.diff_file, "r") as f:
                diff_text = f.read()
        except Exception:
            sys.exit(1)
        llm_client.reset_telemetry_counters()
        parsed_files = parse_full_diff(diff_text)
        all_comments = []
        conventions = get_repo_conventions()
        total_diff_lines = len(diff_text.splitlines())
        
        # Instantiate a mock offline cache
        cache = SharedContextCache(".")
        
        for filepath, data in parsed_files.items():
            if is_ignored_file(filepath):
                continue
            lang = identify_language(filepath)
            chunks = chunk_file_diffs(filepath, lang, data)
            for chunk in chunks:
                try:
                    comments = orchestrate_chunk_review(
                        chunk, conventions, symbol_index={}, db_path=".", cache=cache,
                        max_escalation_rounds=args.max_context_calls
                    )
                    all_comments.extend(validate_and_deduplicate_comments(comments, filepath, data))
                except Exception:
                    pass
        elapsed_time = time.time() - start_time
        print("\n================== PERFORMANCE METRICS ==================", flush=True)
        print(f"PR Diff size: {total_diff_lines:,} total lines", flush=True)
        print(f"Execution Latency: {elapsed_time:.2f} seconds", flush=True)
        print(f"Tokens consumed:\n  - Input: {llm_client.token_stats['prompt_tokens']:,}\n  - Output: {llm_client.token_stats['candidates_tokens']:,}\n  - Total: {llm_client.token_stats['total_tokens']:,}", flush=True)
        print("=========================================================\n", flush=True)
        return
    if args.repo and args.pr:
        run_review_pipeline(args.repo, args.pr, dep_hops=args.dep_hops, max_escalation_rounds=args.max_context_calls)
    else:
        parser.print_help()

if __name__ == "__main__":
    main()