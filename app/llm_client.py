import os
import time
import threading
import requests
from google import genai
from google.genai import types
from app.config import get_secret, is_debug_mode  # <--- Updated Import

_context = {
    "owner": "",
    "repo": "",
    "token": "",
    "commit_sha": ""
}

_lock = threading.Lock()
get_lines_counter = 0
get_lines_limit = 2
_gatekeeper_ref = None

token_stats = {
    "prompt_tokens": 0,
    "candidates_tokens": 0,
    "total_tokens": 0
}

def get_lines(file: str, start: int, end: int) -> str:
    """
    Escalation option to fetch full file content lines.
    """
    global get_lines_counter, get_lines_limit
    
    with _lock:
        if get_lines_limit != -1 and get_lines_counter >= get_lines_limit:
            if is_debug_mode():
                print(f"[DEBUG] [get_lines Tool] LLM requested get_lines('{file}', {start}, {end}) but execution limit is reached ({get_lines_limit}).")
            return f"Error: Call limit exceeded (maximum {get_lines_limit} invocations of get_lines allowed)."
        get_lines_counter += 1
        
    if is_debug_mode():
        print(f"[DEBUG] [get_lines Tool] LLM invoked get_lines(file='{file}', start={start}, end={end}) [Call #{get_lines_counter}]")

    content = ""
    if os.path.exists(file):
        try:
            with open(file, 'r', encoding='utf-8', errors='ignore') as f:
                lines = f.readlines()
            start_idx = max(0, start - 1)
            end_idx = min(len(lines), end)
            content = "".join(lines[start_idx:end_idx])
        except Exception:
            pass
            
    if not content:
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
                    content = "".join(lines[start_idx:end_idx])
            except Exception:
                pass
                
    if not content:
        return f"Error: File '{file}' not found locally or remote parameters missing."

    content_tokens = int(len(content) / 4)
    if _gatekeeper_ref:
        while True:
            can_go, wait_time = _gatekeeper_ref.can_consume(content_tokens)
            if can_go:
                _gatekeeper_ref.record_call(content_tokens)
                break
            if is_debug_mode():
                print(f"[DEBUG] [get_lines Tool] Quota threshold near. Delaying tool content return. Pausing thread for {wait_time:.1f}s...")
            time.sleep(wait_time)
            
    return content

def reset_telemetry_counters(limit: int = 2):
    global get_lines_counter, get_lines_limit
    with _lock:
        get_lines_counter = 0
        get_lines_limit = limit
        token_stats["prompt_tokens"] = 0
        token_stats["candidates_tokens"] = 0
        token_stats["total_tokens"] = 0

def call_gemini(prompt: str, system_instruction: str, response_schema) -> str:
    api_key = get_secret("GEMINI_API_KEY")
    if not api_key:
        raise ValueError("GEMINI_API_KEY is not defined. Run `python -m app.config --set-gemini` to configure it.")
        
    client = genai.Client(api_key=api_key)
    model_name = os.getenv("LLM_MODEL", "gemini-3.1-flash-lite")
    
    config = types.GenerateContentConfig(
        temperature=0.1,
        system_instruction=system_instruction,
        tools=[get_lines]
    )
    
    max_retries = 3
    retry_delay = 3
    
    if is_debug_mode():
        print(f"[DEBUG] [LLM Client] Sending generation call to model '{model_name}' (Payload size: {len(prompt)} chars).")
        
    for attempt in range(max_retries):
        try:
            response = client.models.generate_content(
                model=model_name,
                contents=prompt,
                config=config
            )
            break
        except Exception as e:
            error_str = str(e)
            is_transient = "503" in error_str or "429" in error_str or "UNAVAILABLE" in error_str or "RESOURCE_EXHAUSTED" in error_str
            
            if is_transient and attempt < max_retries - 1:
                print(f"[LLM Client] Transient error encountered ({error_str.splitlines()[0]}). Retrying in {retry_delay} seconds...")
                time.sleep(retry_delay)
                retry_delay *= 2
            else:
                raise e
    
    if not response.text:
        raise RuntimeError("LLM returned an empty response.")
        
    usage = response.usage_metadata
    if usage:
        token_stats["prompt_tokens"] += getattr(usage, "prompt_token_count", 0) or 0
        token_stats["candidates_tokens"] += getattr(usage, "candidates_token_count", 0) or 0
        token_stats["total_tokens"] += getattr(usage, "total_token_count", 0) or 0
        if is_debug_mode():
            print(f"[DEBUG] [LLM Client] Call finished. Tokens used in turn: {getattr(usage, 'total_token_count', 0)}")
        
    return response.text