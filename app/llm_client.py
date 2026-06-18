import os
import time
import requests
from google import genai
from google.genai import types
from app.config import get_secret

_context = {
    "owner": "",
    "repo": "",
    "token": "",
    "commit_sha": ""
}

get_lines_counter = 0

token_stats = {
    "prompt_tokens": 0,
    "candidates_tokens": 0,
    "total_tokens": 0
}

def get_lines(file: str, start: int, end: int) -> str:
    """
    Escalation option to get the actual file content lines between start and end (inclusive)
    for extra surrounding context if needed. Max 2 calls allowed per review run.
    """
    global get_lines_counter
    get_lines_counter += 1
    if get_lines_counter > 2:
        return "Error: Call limit exceeded (maximum 2 invocations of get_lines allowed per review run)."
        
    # 1. Check local file system
    if os.path.exists(file):
        try:
            with open(file, 'r', encoding='utf-8', errors='ignore') as f:
                lines = f.readlines()
            start_idx = max(0, start - 1)
            end_idx = min(len(lines), end)
            return "".join(lines[start_idx:end_idx])
        except Exception:
            pass
            
    # 2. Remote fallback over API
    owner = _context.get("owner")
    repo = _context.get("repo")
    token = _context.get("token")
    ref = _context.get("commit_sha")
    
    if owner and repo and token and ref:
        url = f"https://api.github.com/repos/{owner}/{repo}/contents/{file}"
        headers = {
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github.v3.raw"
        }
        params = {"ref": ref}
        try:
            response = requests.get(url, headers=headers, params=params)
            if response.status_code == 200:
                lines = response.text.splitlines(keepends=True)
                start_idx = max(0, start - 1)
                end_idx = min(len(lines), end)
                return "".join(lines[start_idx:end_idx])
        except Exception:
            pass
            
    return f"Error: File '{file}' not found locally or remote parameters missing."

def reset_telemetry_counters():
    global get_lines_counter
    get_lines_counter = 0
    token_stats["prompt_tokens"] = 0
    token_stats["candidates_tokens"] = 0
    token_stats["total_tokens"] = 0

def call_gemini(prompt: str, system_instruction: str, response_schema) -> str:
    api_key = get_secret("GEMINI_API_KEY")
    if not api_key:
        raise ValueError("GEMINI_API_KEY is not defined. Run `python -m app.config --set-gemini` to configure it.")
        
    client = genai.Client(api_key=api_key)
    model_name = os.getenv("LLM_MODEL", "gemini-3.5-flash")
    
    config = types.GenerateContentConfig(
        temperature=0.1,
        system_instruction=system_instruction,
        tools=[get_lines]
    )
    
    # Retry engine parameters
    max_retries = 3
    retry_delay = 3  # Wait 3 seconds initially, doubling each attempt
    
    for attempt in range(max_retries):
        try:
            response = client.models.generate_content(
                model=model_name,
                contents=prompt,
                config=config
            )
            break
        except Exception as e:
            # Check for transient server issues (503) or rate limits (429)
            error_str = str(e)
            is_transient = "503" in error_str or "429" in error_str or "UNAVAILABLE" in error_str or "RESOURCE_EXHAUSTED" in error_str
            
            if is_transient and attempt < max_retries - 1:
                print(f"[LLM Client] Transient error encountered ({error_str.splitlines()[0]}). Retrying in {retry_delay} seconds...")
                time.sleep(retry_delay)
                retry_delay *= 2
            else:
                # Re-raise the exception if the retry attempts are exhausted
                raise e
    
    if not response.text:
        raise RuntimeError("LLM returned an empty response.")
        
    usage = response.usage_metadata
    if usage:
        token_stats["prompt_tokens"] += getattr(usage, "prompt_token_count", 0) or 0
        token_stats["candidates_tokens"] += getattr(usage, "candidates_token_count", 0) or 0
        token_stats["total_tokens"] += getattr(usage, "total_token_count", 0) or 0
        
    return response.text