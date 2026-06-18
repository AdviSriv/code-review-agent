import os
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
            
    # 2. Remote fallback over API (if repository is not checked out locally on the VM)
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

def reset_tool_counter():
    global get_lines_counter
    get_lines_counter = 0

def call_gemini(prompt: str, system_instruction: str, response_schema) -> str:
    api_key = get_secret("GEMINI_API_KEY")
    if not api_key:
        raise ValueError("GEMINI_API_KEY is not defined. Run `python -m app.config --set-gemini` to configure it.")
        
    client = genai.Client(api_key=api_key)
    model_name = os.getenv("LLM_MODEL", "gemini-2.5-flash")
    
    config = types.GenerateContentConfig(
        response_mime_type="application/json",
        response_schema=response_schema,
        temperature=0.1,
        system_instruction=system_instruction,
        tools=[get_lines]  # Automatically configures tool calling pipeline
    )
    
    response = client.models.generate_content(
        model=model_name,
        contents=prompt,
        config=config
    )
    
    if not response.text:
        raise RuntimeError("LLM returned an empty response.")
        
    return response.text