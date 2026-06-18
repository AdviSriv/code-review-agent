import sys
import argparse
import requests
import json
import threading
from http.server import HTTPServer, BaseHTTPRequestHandler

from app.config import get_secret
from app.diff_parser import parse_patch, parse_full_diff, is_ignored_file, identify_language
from app.prompt_builder import build_system_instruction, chunk_file_diffs
from app.llm_client import call_gemini, reset_telemetry_counters, token_stats  # <--- Updated Import
from app.models import CodeReviewResponse
from app.output_parser import parse_and_sort_comments, format_terminal_output, build_github_review_payload
from app.repo_checks import get_repo_conventions, fetch_and_validate_commits

def fetch_repo_metadata(owner: str, repo: str, token: str) -> dict:
    """
    Fetches core repository metadata (like size in KB).
    """
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
        if page > 10:
            break
    return all_files

def post_github_review(owner: str, repo: str, pr_number: str, token: str, commit_id: str, comments: list, body_summary: str):
    url = f"https://api.github.com/repos/{owner}/{repo}/pulls/{pr_number}/reviews"
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github.v3+json",
        "Content-Type": "application/json"
    }
    payload = {
        "commit_id": commit_id,
        "event": "COMMENT",
        "body": body_summary,
        "comments": comments
    }
    response = requests.post(url, headers=headers, json=payload)
    if response.status_code not in [200, 201]:
        print(f"Failed to post grouped review. Status: {response.status_code}, Info: {response.text}")
        sys.exit(1)
    print("Grouped Pull Request review posted successfully.")

def run_review_pipeline(repo_full_name: str, pr_number: str):
    print(f"\n[Worker] Starting review pipeline for {repo_full_name} PR #{pr_number}")
    try:
        token = get_secret("GITHUB_PAT")
        if not token:
            print(f"[Worker] Error: No GITHUB_PAT mapped for '{repo_full_name}'. Execution aborted.")
            return
            
        owner, repo = repo_full_name.split("/")
        
        # Gather repository statistics (size / metadata)
        repo_meta = fetch_repo_metadata(owner, repo, token)
        repo_size_kb = repo_meta.get("size", 0)
        
        # 1. Inspect metadata for Draft PR status
        pr_data = fetch_pr_metadata(owner, repo, pr_number, token)
        if pr_data.get("draft") is True:
            print(f"[Worker] PR #{pr_number} is a draft. Terminating execution immediately.")
            return
            
        commit_id = pr_data.get("head", {}).get("sha")
        
        # Configure tool fallback variables and reset stats
        from app.llm_client import _context
        _context["owner"] = owner
        _context["repo"] = repo
        _context["token"] = token
        _context["commit_sha"] = commit_id
        reset_telemetry_counters()
        
        # 2. Fetch modified PR files (handling pagination)
        pr_files = fetch_all_pr_files(owner, repo, pr_number, token)
        
        all_comments = []
        conventions = get_repo_conventions(owner, repo, token)
        system_inst = build_system_instruction(conventions)
        
        total_diff_lines = 0
        
        for f in pr_files:
            filename = f.get("filename")
            patch = f.get("patch")
            
            # Skip ignored extensions, generated files, and third-party vendor folders
            if is_ignored_file(filename):
                print(f"[Worker] Skipping filtered file: {filename}")
                continue
                
            if not patch:
                print(f"[Worker] Skipping {filename} (no patch context available)")
                continue
                
            total_diff_lines += len(patch.splitlines())
            lang = identify_language(filename)
            parsed_diff = parse_patch(patch)
            chunks = chunk_file_diffs(filename, lang, parsed_diff)
            
            for chunk in chunks:
                try:
                    raw_response = call_gemini(chunk, system_inst, CodeReviewResponse)
                    comments = parse_and_sort_comments(raw_response)
                    all_comments.extend(comments)
                except Exception as e:
                    print(f"[Worker] Chunk execution failed: {e}")
                    
        # 3. Perform programmatic Zero-LLM commit conventions validation
        commit_suggestions = fetch_and_validate_commits(owner, repo, pr_number, token)
        gh_comments = build_github_review_payload(all_comments)
        
        summary_body = "### 🛡️ Automated Code Review Completed\n\nAll inline comments are batched and posted below."
        if commit_suggestions:
            summary_body += "\n\n### ⚠️ Commit Message Conventions Suggestions (Low Severity)\n"
            for s in commit_suggestions:
                summary_body += f"- {s}\n"
                
        if gh_comments or commit_suggestions:
            print(f"[Worker] Submitting batched review...")
            post_github_review(
                owner=owner,
                repo=repo,
                pr_number=pr_number,
                token=token,
                commit_id=commit_id,
                comments=gh_comments,
                body_summary=summary_body
            )
        else:
            print("[Worker] No review actions generated.")
            
        # Print Consolidated Performance metrics
        print("\n================== PERFORMANCE METRICS ==================")
        if repo_size_kb:
            print(f"Repository size: {repo_size_kb:,} KB")
        else:
            print("Repository size: Unknown/Unavailable")
        print(f"PR Diff size: {total_diff_lines:,} total lines processed")
        print(f"Tokens consumed for evaluation:")
        print(f"  - Input (Prompt) Tokens: {token_stats['prompt_tokens']:,}")
        print(f"  - Output (Completion) Tokens: {token_stats['candidates_tokens']:,}")
        print(f"  - Total Tokens consumed: {token_stats['total_tokens']:,}")
        print("=========================================================\n")
            
    except Exception as e:
        print(f"[Worker] Pipeline crashed: {e}")

class WebhookHandler(BaseHTTPRequestHandler):
    def do_POST(self):
        if self.path != "/webhook":
            self.send_response(404)
            self.end_headers()
            return
            
        content_length = int(self.headers.get('Content-Length', 0))
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
                print("Ignored draft Pull Request webhook event.")
                return
                
            repo_full_name = payload.get("repository", {}).get("full_name")
            pr_number = payload.get("number")
            
            if repo_full_name and pr_number:
                self.send_response(202)
                self.end_headers()
                
                thread = threading.Thread(
                    target=run_review_pipeline,
                    args=(repo_full_name, str(pr_number))
                )
                thread.start()
            else:
                self.send_response(400)
                self.end_headers()
        except Exception as e:
            self.send_response(500)
            self.end_headers()
            print(f"Error handling webhook: {e}")

def main():
    parser = argparse.ArgumentParser(description="VM Code Review Agent")
    parser.add_argument("--repo", help="Target repository 'owner/repo'")
    parser.add_argument("--pr", help="Pull request ID number")
    parser.add_argument("--diff-file", help="Path to local diff file for offline validation")
    parser.add_argument("--server", action="store_true", help="Start the background VM Webhook HTTP Server")
    parser.add_argument("--port", type=int, default=8000, help="Webhook server port (default: 8000)")
    args = parser.parse_args()
    
    if args.server:
        server = HTTPServer(('0.0.0.0', args.port), WebhookHandler)
        print(f"Webhook Receiver active on port {args.port}...")
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            print("Server shutting down.")
        return

    if args.diff_file:
        # Local Offline Execution Path
        print(f"Reading local diff file: {args.diff_file}...")
        try:
            with open(args.diff_file, "r") as f:
                diff_text = f.read()
        except Exception as e:
            print(f"Failed to read file: {e}")
            sys.exit(1)
            
        reset_telemetry_counters()
        parsed_files = parse_full_diff(diff_text)
        all_comments = []
        conventions = get_repo_conventions()
        system_inst = build_system_instruction(conventions)
        
        total_diff_lines = len(diff_text.splitlines())
        
        for filepath, data in parsed_files.items():
            if is_ignored_file(filepath):
                continue
            lang = identify_language(filepath)
            chunks = chunk_file_diffs(filepath, lang, data)
            
            for chunk in chunks:
                try:
                    raw_response = call_gemini(chunk, system_inst, CodeReviewResponse)
                    comments = parse_and_sort_comments(raw_response)
                    all_comments.extend(comments)
                except Exception as e:
                    print(f"Error reviewing chunk: {e}")
                    
        format_terminal_output(all_comments)
        
        # Display telemetry panel for offline runs
        print("\n================== PERFORMANCE METRICS ==================")
        print("Repository size: N/A (offline local execution mode)")
        print(f"PR Diff size: {total_diff_lines:,} total lines processed")
        print(f"Tokens consumed for evaluation:")
        print(f"  - Input (Prompt) Tokens: {token_stats['prompt_tokens']:,}")
        print(f"  - Output (Completion) Tokens: {token_stats['candidates_tokens']:,}")
        print(f"  - Total Tokens consumed: {token_stats['total_tokens']:,}")
        print("=========================================================\n")
        
    elif args.repo and args.pr:
        # Direct CLI execution
        run_review_pipeline(args.repo, args.pr)
    else:
        parser.print_help()

if __name__ == "__main__":
    main()