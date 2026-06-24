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

# Config and parsers
from app.config import get_secret, get_repo_pat, set_debug_mode, is_debug_mode
from app.diff_parser import parse_patch, parse_full_diff, is_ignored_file, identify_language
from app.prompt_builder import chunk_file_diffs
import app.llm_client as llm_client
from app.models import CodeComment
from app.output_parser import build_github_review_payload
from app.repo_checks import get_repo_conventions, fetch_and_validate_commits

# Import Phase 2 Modules
from app.repo_intelligence import load_repo_intelligence, build_repo_intelligence
from app.triage import evaluate_docs_only_skip, run_native_compile_check
from app.chunker import compile_dependency_bundle
from app.gatekeeper import RateGatekeeper
from app.orchestrator import orchestrate_chunk_review
from app.validator import validate_and_deduplicate_comments

gatekeeper = RateGatekeeper()

def fetch_repo_metadata(owner: str, repo: str, token: str) -> dict:
    url = f"https://api.github.com/repos/{owner}/{repo}"
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json"
    }
    try:
        response = requests.get(url, headers=headers)
        if response.status_code == 200:
            return response.json()
    except Exception:
        pass
    return {}

def fetch_pr_metadata(owner: str, repo: str, pr_number: str, token: str) -> dict:
    url = f"https://api.github.com/repos/{owner}/{repo}/pulls/{pr_number}"
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json"
    }
    response = requests.get(url, headers=headers)
    if response.status_code != 200:
        raise RuntimeError(f"Failed to fetch PR metadata. API Status: {response.status_code}")
    return response.json()

def fetch_all_pr_files(owner: str, repo: str, pr_number: str, token: str) -> list:
    all_files = []
    page = 1
    while True:
        url = f"https://api.github.com/repos/{owner}/{repo}/pulls/{pr_number}/files"
        params = {"page": page, "per_page": 100}
        headers = {
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json"
        }
        response = requests.get(url, headers=headers, params=params)
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
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github.v3+json",
        "Content-Type": "application/json"
    }
    
    MAX_COMMENTS_PER_BATCH = 8
    
    if not comments:
        payload = {
            "commit_id": commit_id,
            "event": "COMMENT",
            "body": body_summary,
            "comments": []
        }
        response = requests.post(url, headers=headers, json=payload)
        if response.status_code not in [200, 201]:
            print(f"Failed to post review. Status: {response.status_code}, Info: {response.text}")
        else:
            print("Summary review posted successfully.")
        return

    comment_batches = [comments[i:i + MAX_COMMENTS_PER_BATCH] for i in range(0, len(comments), MAX_COMMENTS_PER_BATCH)]
    print(f"[Publisher] Splitting review into {len(comment_batches)} paced batches of max {MAX_COMMENTS_PER_BATCH} inline comments to satisfy API rate limits.")
    
    for idx, batch in enumerate(comment_batches, 1):
        batch_body = body_summary if idx == 1 else f"### 🛡️ Phase 2 Inline Code Review [Batch {idx}/{len(comment_batches)}]"
        
        payload = {
            "commit_id": commit_id,
            "event": "COMMENT",
            "body": batch_body,
            "comments": batch
        }
        
        response = requests.post(url, headers=headers, json=payload)
        
        if response.status_code not in [200, 201]:
            print(f"[Publisher] Failed to post batch {idx}. Status: {response.status_code}, Info: {response.text}")
            if response.status_code in [403, 429]:
                print("[Publisher] Hit secondary rate limits mid-write. Sleeping for 10s before retry...")
                time.sleep(10.0)
                response = requests.post(url, headers=headers, json=payload)
                if response.status_code not in [200, 201]:
                    print(f"[Publisher] Retry failed for batch {idx}. Skipping this batch.")
        else:
            print(f"[Publisher] Inline Comment Batch {idx}/{len(comment_batches)} submitted successfully.")
        
        if idx < len(comment_batches):
            time.sleep(2.0)

def ensure_local_checkout(owner: str, repo_name: str, commit_sha: str, token: str) -> str:
    """
    Dynamically clones or updates a local copy of the target repository 
    and checks out the specified commit SHA. Returns the path to the workspace root.
    """
    cache_dir = Path.home() / ".cache" / "code_review_agent" / "repos" / owner / repo_name
    workspace_path = str(cache_dir)
    
    if cache_dir.exists() and not (cache_dir / ".git").exists():
        print(f"[Workspace] Cleaning up invalid/empty clone directory: {workspace_path}")
        try:
            shutil.rmtree(cache_dir)
        except Exception as e:
            print(f"[Workspace] Failed to clean directory: {e}")
            
    cache_dir.mkdir(parents=True, exist_ok=True)
    
    if not (cache_dir / ".git").exists():
        print(f"[Workspace] Cloning repository '{owner}/{repo_name}' dynamically...")
        clone_url = f"https://{token}@github.com/{owner}/{repo_name}.git"
        res = subprocess.run(["git", "clone", clone_url, workspace_path], capture_output=True, text=True)
        
        if res.returncode != 0:
            print(f"[Workspace] Authenticated clone failed: {res.stderr.strip()}")
            print("[Workspace] Attempting fallback clone as a public repository (no token)...")
            
            try:
                shutil.rmtree(cache_dir)
            except Exception:
                pass
            cache_dir.mkdir(parents=True, exist_ok=True)
            
            public_url = f"https://github.com/{owner}/{repo_name}.git"
            res = subprocess.run(["git", "clone", public_url, workspace_path], capture_output=True, text=True)
            
            if res.returncode != 0:
                try:
                    shutil.rmtree(cache_dir)
                except Exception:
                    pass
                raise RuntimeError(f"Clone failed under both authenticated and public scopes: {res.stderr.strip()}")
    else:
        subprocess.run(["git", "reset", "--hard"], cwd=workspace_path, capture_output=True)
        subprocess.run(["git", "clean", "-fd"], cwd=workspace_path, capture_output=True)
        res = subprocess.run(["git", "fetch", "origin"], cwd=workspace_path, capture_output=True, text=True)
        if res.returncode != 0:
            print(f"[Workspace] Fetch failed: {res.stderr.strip()}")
            
    print(f"[Workspace] Checking out commit '{commit_sha}'...")
    res = subprocess.run(["git", "checkout", commit_sha], cwd=workspace_path, capture_output=True, text=True)
    if res.returncode != 0:
        subprocess.run(["git", "fetch", "--unshallow"], cwd=workspace_path, capture_output=True)
        res = subprocess.run(["git", "checkout", commit_sha], cwd=workspace_path, capture_output=True, text=True)
        if res.returncode != 0:
            raise RuntimeError(f"Checkout failed completely: {res.stderr.strip()}")
            
    return workspace_path

def run_review_pipeline(repo_full_name: str, pr_number: str, dep_hops: int = 2, get_lines_limit: int = 2):
    print(f"\n[Pipeline] Initializing Review for {repo_full_name} PR #{pr_number}")
    try:
        token = get_repo_pat(repo_full_name)
        if not token:
            print(f"[Pipeline] Error: GITHUB_PAT not configured for '{repo_full_name}'.")
            return
            
        owner, repo = repo_full_name.split("/")
        
        # Fetch repository statistics (size / metadata)
        repo_meta = fetch_repo_metadata(owner, repo, token)
        repo_size_kb = repo_meta.get("size", 0)
        
        # 1. Inspect metadata for Draft PR status
        pr_data = fetch_pr_metadata(owner, repo, pr_number, token)
        if pr_data.get("draft") is True:
            print(f"[Pipeline] PR #{pr_number} is a draft. Skipped.")
            return
            
        commit_id = pr_data.get("head", {}).get("sha")
        base_sha = pr_data.get("base", {}).get("sha")
        
        # --- Dynamic Local Workspace Resolution ---
        workspace_path = ensure_local_checkout(owner, repo, commit_id, token)
        
        # Configure tool fallback variables and register workspace path
        llm_client._context["owner"] = owner
        llm_client._context["repo"] = repo
        llm_client._context["token"] = token
        llm_client._context["commit_sha"] = commit_id
        llm_client._context["workspace"] = workspace_path
        llm_client._gatekeeper_ref = gatekeeper
        llm_client.reset_telemetry_counters(get_lines_limit)
        
        # 2. Ensure base commit is checked out and indexed dynamically
        intel_data = load_repo_intelligence(base_sha)
        
        # If the index is empty/corrupt, delete and force rebuild
        if not intel_data.get("symbol_index") or not intel_data.get("dependency_graph"):
            print(f"[RepoIntel] Index for base commit '{base_sha}' is missing or empty. Forcing checkout and rebuild...")
            
            # Clean up corrupted blank index file from disk if it exists
            intel_file = Path.home() / ".config" / "code_review_agent" / "repo_intel" / f"{base_sha}.json"
            if intel_file.exists():
                try:
                    os.remove(intel_file)
                except Exception:
                    pass
            
            ensure_local_checkout(owner, repo, base_sha, token)
            build_repo_intelligence(workspace_path, base_sha)
            
            # Checkout HEAD commit again to continue review pipeline
            ensure_local_checkout(owner, repo, commit_id, token)
            intel_data = load_repo_intelligence(base_sha)
            
        symbol_index = intel_data.get("symbol_index", {})
        dependency_graph = intel_data.get("dependency_graph", {})
        
        # 3. Fetch modified files
        pr_files = fetch_all_pr_files(owner, repo, pr_number, token)
        changed_file_paths = [os.path.join(workspace_path, f.get("filename")) for f in pr_files]
        
        # --- Module B: Triage Gate ---
        if evaluate_docs_only_skip(changed_file_paths):
            print("[Pipeline] Triage: Skipped. Docs-only changes detected.")
            post_github_review(owner, repo, pr_number, token, commit_id, [], "### ℹ️ Review Skipped\nThis PR contains documentation, configurations, or lockfiles only.")
            return
            
        # Run syntax compile and linter check inside the active workspace
        os.chdir(workspace_path)
        compile_status = run_native_compile_check(changed_file_paths)
        if not compile_status["passed"]:
            print("[Pipeline] Triage: Skipped due to compilation syntax/linter errors.")
            post_github_review(owner, repo, pr_number, token, commit_id, [], compile_status["error_msg"])
            return
            
        all_comments = []
        total_diff_lines = 0
        conventions = get_repo_conventions(owner, repo, token)
        
        # 4. Process changes per file
        for f in pr_files:
            filename = f.get("filename")
            patch = f.get("patch")
            
            if is_ignored_file(filename) or not patch:
                continue
                
            total_diff_lines += len(patch.splitlines())
            lang = identify_language(filename)
            parsed_diff = parse_patch(patch)
            
            # --- Module C: Diff Chunker ---
            chunks = chunk_file_diffs(filename, lang, parsed_diff)
            
            for chunk in chunks:
                # Compile recursive dependency structures
                dep_bundle = compile_dependency_bundle(filename, parsed_diff, symbol_index, dependency_graph, max_hops=dep_hops)
                payload = f"{chunk}\n\n{dep_bundle}"
                
                # --- Module D: Rate Gatekeeper ---
                estimated_tokens = int(len(payload) / 4)
                
                if is_debug_mode():
                    print(f"\n[DEBUG] [Pipeline] Chunk Payload size: {len(payload)} chars (~{estimated_tokens} tokens).")
                    print(f"[DEBUG] [Pipeline] Chunk Payload Header:\n{payload[:300]}\n...")
                
                while True:
                    can_go, wait_time = gatekeeper.can_consume(estimated_tokens)
                    if can_go:
                        break
                    print(f"[Gatekeeper] Quota limits approaching. Throttling review thread, waiting {wait_time:.1f}s...")
                    time.sleep(wait_time)
                
                # --- Module E & F: Subagent Orchestrator ---
                print(f"[Pipeline] Analysing file chunks: '{filename}'...")
                chunk_comments = orchestrate_chunk_review(payload, conventions, symbol_index)
                
                gatekeeper.record_call(estimated_tokens)
                
                # --- Module G: Aggregation & Validation ---
                validated_comments = validate_and_deduplicate_comments(chunk_comments, filename, parsed_diff)
                all_comments.extend(validated_comments)

        # 6. Programmatic Zero-LLM commit conventions validation
        commit_suggestions = fetch_and_validate_commits(owner, repo, pr_number, token)
        gh_comments = build_github_review_payload(all_comments)
        
        summary_body = f"### 🛡️ Phase 2 Code Review Complete\n\nAll findings have been batched and mapped directly to lines."
        if commit_suggestions:
            summary_body += "\n\n### ⚠️ Commit Message Conventions Suggestions\n"
            for s in commit_suggestions:
                summary_body += f"- {s}\n"
                
        # --- Module H: Batch Post Review ---
        if gh_comments or commit_suggestions:
            post_github_review(owner, repo, pr_number, token, commit_id, gh_comments, summary_body)
        else:
            print("[Pipeline] No findings. Skipping empty review posting.")
            
        # Display telemetry panel
        print("\n================== PERFORMANCE METRICS ==================")
        if repo_size_kb:
            print(f"Repository size: {repo_size_kb:,} KB")
        else:
            print("Repository size: Unknown/Unavailable")
        print(f"PR Diff size: {total_diff_lines:,} total lines processed")
        print(f"Tokens consumed for evaluation:")
        print(f"  - Input (Prompt) Tokens: {llm_client.token_stats['prompt_tokens']:,}")
        print(f"  - Output (Completion) Tokens: {llm_client.token_stats['candidates_tokens']:,}")
        print(f"  - Total Tokens consumed: {llm_client.token_stats['total_tokens']:,}")
        print("=========================================================\n")
            
    except Exception as e:
        print(f"[Pipeline] Crash: {e}")

class WebhookHandler(BaseHTTPRequestHandler):
    def do_POST(self):
        if self.path != "/webhook":
            self.send_response(404)
            self.end_headers()
            return
            
        content_length = int(self.headers.get('Content-Length', 0) or 0)
        post_data = self.rfile.read(content_length)
        
        try:
            event = self.headers.get('X-GitHub-Event', '')
            if event != 'pull_request':
                self.send_response(200)
                self.end_headers()
                return
                
            payload = json.loads(post_data.decode('utf-8'))
            action = payload.get("action")
            
            if action not in ["opened", "synchronize", "ready_for_review"]:
                self.send_response(200)
                self.end_headers()
                return
                
            pr_data = payload.get("pull_request", {})
            if pr_data.get("draft") is True:
                self.send_response(200)
                self.end_headers()
                return
                
            repo_full_name = payload.get("repository", {}).get("full_name")
            pr_number = payload.get("number")
            
            if repo_full_name and pr_number:
                self.send_response(202)
                self.end_headers()
                
                # Fetch default parameters from global server scope
                thread = threading.Thread(
                    target=run_review_pipeline,
                    args=(repo_full_name, str(pr_number), server_dep_hops, server_lines_limit)
                )
                thread.start()
            else:
                self.send_response(400)
                self.end_headers()
        except Exception as e:
            self.send_response(500)
            self.end_headers()
            print(f"Error handling webhook: {e}")

# Globals to share parsed parameters across threads in server mode
server_dep_hops = 2
server_lines_limit = 2

def main():
    global server_dep_hops, server_lines_limit
    
    parser = argparse.ArgumentParser(description="Phase 2 Code Review Agent")
    parser.add_argument("--repo", help="Target repository 'owner/repo'")
    parser.add_argument("--pr", help="Pull request ID number")
    parser.add_argument("--diff-file", help="Path to local diff file")
    parser.add_argument("--server", action="store_true", help="Start background Webhook Server")
    parser.add_argument("--port", type=int, default=8000, help="Webhook server port")
    parser.add_argument("--build-index", action="store_true", help="Manually trigger Module A indexing on local workspace")
    parser.add_argument("--commit", help="Commit SHA key (required for --build-index)")
    
    # Hyperparameters
    parser.add_argument("--dep-hops", type=int, default=2, help="Dependency graph BFS depth. Set to -1 to crawl all connected nodes.")
    parser.add_argument("--max-context-calls", type=int, default=2, help="Max context escalations (get_lines) allowed per run. Set to -1 for unlimited.")
    parser.add_argument("--debug", action="store_true", help="Enable detailed trace logging throughout the execution flow.")
    args = parser.parse_args()
    
    if args.build_index:
        if not args.commit:
            print("Error: --commit <SHA> is required when building index.")
            sys.exit(1)
        build_repo_intelligence(".", args.commit)
        return

    # Map CLI parameters globally for server thread reuse
    server_dep_hops = args.dep_hops
    server_lines_limit = args.max_context_calls
    
    # Configure global debug trace parameter
    if args.debug:
        set_debug_mode(True)

    if args.server:
        server = HTTPServer(('0.0.0.0', args.port), WebhookHandler)
        print(f"Webhook Receiver active on port {args.port} (Hops: {server_dep_hops}, Context Calls: {server_lines_limit})...")
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            print("Server shutting down.")
        return

    if args.diff_file:
        print(f"Reading local diff file: {args.diff_file}...")
        try:
            with open(args.diff_file, "r") as f:
                diff_text = f.read()
        except Exception as e:
            print(f"Failed to read file: {e}")
            sys.exit(1)
            
        llm_client.reset_telemetry_counters(args.max_context_calls)
        parsed_files = parse_full_diff(diff_text)
        all_comments = []
        conventions = get_repo_conventions()
        
        total_diff_lines = len(diff_text.splitlines())
        
        for filepath, data in parsed_files.items():
            if is_ignored_file(filepath):
                continue
            lang = identify_language(filepath)
            chunks = chunk_file_diffs(filepath, lang, data)
            
            for chunk in chunks:
                try:
                    # Pass conventions directly into the offline orchestrator path
                    comments = orchestrate_chunk_review(chunk, conventions, symbol_index={})
                    all_comments.extend(comments)
                except Exception as e:
                    print(f"Error reviewing chunk: {e}")
                    
        print("\n================== PERFORMANCE METRICS ==================")
        print("Repository size: N/A (offline local execution mode)")
        print(f"PR Diff size: {total_diff_lines:,} total lines processed")
        print(f"Tokens consumed for evaluation:")
        print(f"  - Input (Prompt) Tokens: {llm_client.token_stats['prompt_tokens']:,}")
        print(f"  - Output (Completion) Tokens: {llm_client.token_stats['candidates_tokens']:,}")
        print(f"  - Total Tokens consumed: {llm_client.token_stats['total_tokens']:,}")
        print("=========================================================\n")
        return
        
    if args.repo and args.pr:
        run_review_pipeline(args.repo, args.pr, dep_hops=args.dep_hops, get_lines_limit=args.max_context_calls)
    else:
        parser.print_help()

if __name__ == "__main__":
    main()