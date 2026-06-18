import os
import json
import argparse
import getpass
from pathlib import Path

CONFIG_DIR = Path.home() / ".config" / "code_review_agent"
CONFIG_FILE = CONFIG_DIR / "config.json"

def get_config() -> dict:
    if not CONFIG_FILE.exists():
        return {}
    try:
        with open(CONFIG_FILE, "r") as f:
            return json.load(f)
    except Exception:
        return {}

def save_config(data: dict):
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    current_data = get_config()
    current_data.update(data)
    with open(CONFIG_FILE, "w") as f:
        json.dump(current_data, f, indent=4)
    # Restrict read/write permissions exclusively to the VM owner user
    os.chmod(CONFIG_FILE, 0o600)

def get_secret(key: str) -> str:
    # Prioritize active shell environment variables first, then fallback to stored config
    val = os.getenv(key)
    if val:
        return val
    return get_config().get(key, "")

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Configure local review secrets securely.")
    # Converted parameters to boolean flags to prevent secret leakage in process arguments
    parser.add_argument("--set-pat", action="store_true", help="Interactively prompt for and save your GitHub Personal Access Token (PAT)")
    parser.add_argument("--set-gemini", action="store_true", help="Interactively prompt for and save your Gemini API Key")
    args = parser.parse_args()

    updates = {}
    
    if args.set_pat:
        # Prompt securely without echoing input to the terminal
        pat = getpass.getpass("Enter GitHub PAT (input will be hidden): ")
        if pat.strip():
            updates["GITHUB_PAT"] = pat.strip()
            print("GitHub Personal Access Token updated locally.")
        else:
            print("Empty input. GitHub PAT update skipped.")

    if args.set_gemini:
        # Prompt securely without echoing input to the terminal
        gemini_key = getpass.getpass("Enter Gemini API Key (input will be hidden): ")
        if gemini_key.strip():
            updates["GEMINI_API_KEY"] = gemini_key.strip()
            print("Gemini API Key updated locally.")
        else:
            print("Empty input. Gemini API Key update skipped.")

    if updates:
        save_config(updates)
    elif not args.set_pat and not args.set_gemini:
        print("No updates requested. Use --set-pat or --set-gemini flags to securely configure keys.")