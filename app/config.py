# ===== app/config.py =====

import os
import json
import argparse
import getpass
from pathlib import Path

CONFIG_DIR = Path.home() / ".config" / "code_review_agent"
CONFIG_FILE = CONFIG_DIR / "config.json"

DEFAULT_CONFIG = {
    "MAX_RPM": 12,
    "MAX_TPM": 200000,
    "MAX_RPD": 400,
    "DEP_TOKEN_BUDGET": 5000,  # Calibrated for 4vCPU/8GB limits with 2 active roles
    "CHUNK_TOKEN_TARGET": 5000,
    "MAX_ESCALATION_RETRIES": 1,
    "MAX_ROUTER_CALLS_PER_CHUNK": 1,
    "TRIAGE_SKIP_PATTERNS": [".md", ".txt", ".lock", "json", "yaml", "yml", ".png", ".jpg", ".jpeg"],
    "ENABLE_STATIC_BASELINE": True,  # Toggle to include/exclude default static analysis baseline warnings
    "LLM_BACKEND": "gemini",         # Swappable: "gemini" or "ollama"
    "OLLAMA_HOST": "http://localhost:11434",
    "OLLAMA_NUM_CTX_MIN": 4096,       # Floor for Ollama's context window (num_ctx).
                                      # Ensures prompts aren't silently truncated by small Ollama defaults.
    "OLLAMA_NUM_CTX_MAX": 16384,      # Increased ceiling from 8192 to 16384 to handle larger context bundles.
    "OLLAMA_NUM_PREDICT": 4096,       # Output limit to ensure plenty of room for structured responses.
    "MAX_LOCAL_PARSE_RETRIES": 2,     # Retries for a single subagent call when Ollama returns invalid/truncated JSON.
    "QDRANT_HOST": "http://localhost:6333",
    "EMBEDDING_MODEL": "nomic-embed-text",
    "EMBEDDING_TIMEOUT": 30.0,
    "EMBEDDING_MAX_CHARS": 6000,     # Safety character limit to fit 2048-token constraints
    "SEMANTIC_SCORE_THRESHOLD": 0.30  # Safety similarity margin adjusted to 0.30
}

_debug_mode = True

def set_debug_mode(enabled: bool):
    global _debug_mode
    _debug_mode = enabled
    if enabled:
        print("[DEBUG] System-wide detailed pipeline trace logger: ENABLED.")

def is_debug_mode() -> bool:
    global _debug_mode
    return _debug_mode

def get_config() -> dict:
    if not CONFIG_FILE.exists():
        save_config(DEFAULT_CONFIG)
        return DEFAULT_CONFIG
    try:
        with open(CONFIG_FILE, "r") as f:
            data = json.load(f)
            for k, v in DEFAULT_CONFIG.items():
                data.setdefault(k, v)
            return data
    except Exception:
        return dict(DEFAULT_CONFIG)

def save_config(data: dict):
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    
    # Directly read existing values from file if it exists, bypass get_config() dependency
    current_data = {}
    if CONFIG_FILE.exists():
        try:
            with open(CONFIG_FILE, "r") as f:
                current_data = json.load(f)
        except Exception:
            current_data = dict(DEFAULT_CONFIG)
    else:
        current_data = dict(DEFAULT_CONFIG)
        
    current_data.update(data)
    with open(CONFIG_FILE, "w") as f:
        json.dump(current_data, f, indent=4)
        
    try:
        os.chmod(CONFIG_FILE, 0o600)
    except Exception:
        pass

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