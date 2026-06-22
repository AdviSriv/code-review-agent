import os
import json
import argparse
import getpass
from pathlib import Path

CONFIG_DIR = Path.home() / ".config" / "code_review_agent"
CONFIG_FILE = CONFIG_DIR / "config.json"

# Phase 2 Default System Settings
DEFAULT_CONFIG = {
    # Rate Gatekeeper Margins (Gemini Free tier: 15 RPM / 250K TPM / 500 RPD)
    "MAX_RPM": 12,
    "MAX_TPM": 200000,
    "MAX_RPD": 400,
    
    # Token budgets
    "DEP_TOKEN_BUDGET": 1500,
    "CHUNK_TOKEN_TARGET": 5000,
    
    # Escalation Caps
    "MAX_ESCALATION_RETRIES": 1,
    "MAX_ROUTER_CALLS_PER_CHUNK": 1,
    
    # Triage skip file extensions
    "TRIAGE_SKIP_PATTERNS": [".md", ".txt", ".lock", "json", "yaml", "yml", ".png", ".jpg", ".jpeg"]
}

def get_config() -> dict:
    if not CONFIG_FILE.exists():
        save_config(DEFAULT_CONFIG)
        return DEFAULT_CONFIG
    try:
        with open(CONFIG_FILE, "r") as f:
            data = json.load(f)
            # Ensure defaults are populated
            for k, v in DEFAULT_CONFIG.items():
                data.setdefault(k, v)
            return data
    except Exception:
        return DEFAULT_CONFIG

def save_config(data: dict):
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    current_data = get_config()
    current_data.update(data)
    with open(CONFIG_FILE, "w") as f:
        json.dump(current_data, f, indent=4)
    os.chmod(CONFIG_FILE, 0o600)

def get_secret(key: str) -> str:
    val = os.getenv(key)
    if val:
        return val
    return get_config().get(key, "")

def get_repo_pat(repo_name: str) -> str:
    config = get_config()
    repos = config.get("REPOS", {})
    return repos.get(repo_name) or get_secret("GITHUB_PAT")

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Configure Phase 2 Secrets & Parameters.")
    parser.add_argument("--set-pat", action="store_true", help="Interactively save GITHUB_PAT")
    parser.add_argument("--set-gemini", action="store_true", help="Interactively save GEMINI_API_KEY")
    parser.add_argument("--set-repo-pat", help="Set repository-specific GITHUB_PAT")
    args = parser.parse_args()

    updates = {}
    if args.set_pat:
        pat = getpass.getpass("Enter General GITHUB_PAT (input hidden): ")
        if pat.strip():
            updates["GITHUB_PAT"] = pat.strip()
            print("General GITHUB_PAT updated.")
            
    if args.set_gemini:
        gemini_key = getpass.getpass("Enter GEMINI_API_KEY (input hidden): ")
        if gemini_key.strip():
            updates["GEMINI_API_KEY"] = gemini_key.strip()
            print("GEMINI_API_KEY updated.")

    if args.set_repo_pat:
        repo_name = args.set_repo_pat.strip()
        pat = getpass.getpass(f"Enter GITHUB_PAT for '{repo_name}' (input hidden): ")
        if pat.strip():
            config = get_config()
            repos = config.setdefault("REPOS", {})
            repos[repo_name] = pat.strip()
            save_config(config)
            print(f"PAT registered successfully for '{repo_name}'.")

    if updates:
        save_config(updates)