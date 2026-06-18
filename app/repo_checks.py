import os
import re
import requests
from typing import List

def read_local_file(filename: str) -> str:
    if os.path.exists(filename):
        try:
            with open(filename, "r", encoding="utf-8") as f:
                return f.read()
        except Exception:
            pass
    return ""

def fetch_github_file(owner: str, repo: str, filename: str, token: str) -> str:
    url = f"https://api.github.com/repos/{owner}/{repo}/contents/{filename}"
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github.v3.raw"
    }
    try:
        response = requests.get(url, headers=headers)
        if response.status_code == 200:
            return response.text
    except Exception:
        pass
    return ""

def get_repo_conventions(owner: str = None, repo: str = None, token: str = None) -> str:
    for name in ["AGENTS.md", "CLAUDE.md"]:
        content = read_local_file(name)
        if content:
            return f"--- REPO CONVENTIONS ({name}) ---\n{content}\n"
            
    if owner and repo and token:
        for name in ["AGENTS.md", "CLAUDE.md"]:
            content = fetch_github_file(owner, repo, name, token)
            if content:
                return f"--- REPO CONVENTIONS ({name}) ---\n{content}\n"
    return ""

def validate_commit_format(commit_message: str) -> bool:
    if not commit_message:
        return False
    first_line = commit_message.splitlines()[0].strip()
    pattern = r"^(feat|fix|chore|docs|refactor|style|test|ci)(\(.+\))?:\s+.{3,}"
    return bool(re.match(pattern, first_line, re.IGNORECASE))

def fetch_and_validate_commits(owner: str, repo: str, pr_number: str, token: str) -> List[str]:
    url = f"https://api.github.com/repos/{owner}/{repo}/pulls/{pr_number}/commits"
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json"
    }
    suggestions = []
    try:
        response = requests.get(url, headers=headers)
        if response.status_code == 200:
            commits = response.json()
            for c in commits:
                msg = c.get("commit", {}).get("message", "")
                sha = c.get("sha", "")[:7]
                if not validate_commit_format(msg):
                    suggestions.append(
                        f"Commit `{sha}` ('{msg.splitlines()[0]}') is non-compliant. Format requires `<type>: <desc>` (what and why)."
                    )
    except Exception as e:
        print(f"Warning: Commit checking errored: {e}")
    return suggestions