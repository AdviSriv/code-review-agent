# ===== app/llm_client.py =====

import os
import time
import threading
import requests
import copy
from google import genai
from google.genai import types
from app.config import get_secret, is_debug_mode, get_config
from app.profiler import PipelineProfiler

class OllamaTruncatedResponseError(RuntimeError):
    """Raised when Ollama reports done_reason == 'length', meaning generation was
    cut off before completion (usually because num_predict was too small for the
    requested JSON object)."""
    pass

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
                print(f"[DEBUG] [get_lines Tool] LLM requested get_lines('{file}', {start}, {end}) but limit is reached.")
            return f"Error: Call limit exceeded (maximum {get_lines_limit} invocations of get_lines allowed)."
        get_lines_counter += 1
        
    if is_debug_mode():
        print(f"[DEBUG] [get_lines Tool] LLM invoked get_lines(file='{file}', start={start}, end={end}) [Call #{get_lines_counter}]")
    content = ""
    
    workspace = _context.get("workspace")
    target_path = os.path.join(workspace, file) if workspace else file
    
    if os.path.exists(target_path):
        try:
            with open(target_path, 'r', encoding='utf-8', errors='ignore') as f:
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
    if get_config().get("LLM_BACKEND") == "ollama":
        return content
    content_tokens = int(len(content) / 4)
    if _gatekeeper_ref:
        profiler = PipelineProfiler()
        start_gatekeeper_wait = time.perf_counter()
        while True:
            can_go, wait_time = _gatekeeper_ref.can_consume(content_tokens)
            if can_go:
                _gatekeeper_ref.record_call(content_tokens)
                break
            if is_debug_mode():
                print(f"[DEBUG] [get_lines Tool] Quota threshold near. Delaying tool content return. Pausing thread for {wait_time:.1f}s...")
            time.sleep(wait_time)
        gatekeeper_wait_duration = time.perf_counter() - start_gatekeeper_wait
        if gatekeeper_wait_duration > 0.01:
            profiler.record("Gatekeeper Wait: Tool API throttling", gatekeeper_wait_duration)
            
    return content

def simplify_schema_for_local_llm(schema: dict) -> dict:
    """
    Recursively inlines all $ref definitions from $defs and simplifies anyOf Union types with null.
    This resolves compatibility issues with local LLMs (e.g. qwen2.5-coder) parsing complex schemas.
    """
    schema = copy.deepcopy(schema)
    defs = schema.pop('$defs', {})
    
    def resolve_refs(subschema):
        if isinstance(subschema, dict):
            if '$ref' in subschema:
                ref_path = subschema['$ref']
                def_name = ref_path.split('/')[-1]
                if def_name in defs:
                    resolved = resolve_refs(defs[def_name])
                    merged = {k: v for k, v in subschema.items() if k != '$ref'}
                    merged.update(resolved)
                    return merged
                return subschema
            
            new_schema = {}
            for k, v in subschema.items():
                new_schema[k] = resolve_refs(v)
                
            if 'anyOf' in new_schema:
                any_of_list = new_schema['anyOf']
                if len(any_of_list) == 2:
                    first, second = any_of_list[0], any_of_list[1]
                    if isinstance(first, dict) and first.get('type') == 'null':
                        resolved = resolve_refs(second)
                        if isinstance(resolved, dict):
                            t = resolved.get('type', 'object')
                            types = [t] if isinstance(t, str) else list(t)
                            if 'null' not in types:
                                types.append('null')
                            merged = {k: v for k, v in resolved.items() if k != 'type'}
                            merged['type'] = types
                            return merged
                    if isinstance(second, dict) and second.get('type') == 'null':
                        resolved = resolve_refs(first)
                        if isinstance(resolved, dict):
                            t = resolved.get('type', 'object')
                            types = [t] if isinstance(t, str) else list(t)
                            if 'null' not in types:
                                types.append('null')
                            merged = {k: v for k, v in resolved.items() if k != 'type'}
                            merged['type'] = types
                            return merged
                return {"anyOf": [resolve_refs(item) for item in any_of_list]}
            return new_schema
            
        elif isinstance(subschema, list):
            return [resolve_refs(item) for item in subschema]
        return subschema
    return resolve_refs(schema)

def reset_telemetry_counters(limit: int = 2):
    global get_lines_counter, get_lines_limit
    with _lock:
        get_lines_counter = 0
        get_lines_limit = limit

def reset_token_stats():
    with _lock:
        token_stats["prompt_tokens"] = 0
        token_stats["candidates_tokens"] = 0
        token_stats["total_tokens"] = 0

def call_local_llm(prompt: str, system_instruction: str, response_schema) -> str:
    """Invokes local Ollama server chat API with structured format support and retry safety."""
    profiler = PipelineProfiler()
    config = get_config()
    ollama_host = config.get("OLLAMA_HOST", "http://localhost:11434")
    model = os.getenv("LLM_MODEL", "deepseek-r1:8b")  # Replaced default value with pulled model

    # Ollama does NOT automatically infer context window from prompt length.
    # Set explicit num_ctx options here to prevent silent front/back context truncation.
    num_predict = int(config.get("OLLAMA_NUM_PREDICT", 2048))
    ctx_floor = int(config.get("OLLAMA_NUM_CTX_MIN", 4096))
    ctx_ceiling = int(config.get("OLLAMA_NUM_CTX_MAX", 8192))
    
    estimated_input_tokens = int((len(system_instruction) + len(prompt)) / 4)
    # Headroom for output generation and safety buffer, aligned to nearest 1024.
    needed_ctx = estimated_input_tokens + num_predict + 512
    needed_ctx = ((needed_ctx // 1024) + 1) * 1024
    num_ctx = max(ctx_floor, min(needed_ctx, ctx_ceiling))

    if is_debug_mode() and needed_ctx > ctx_ceiling:
        print(f"[DEBUG] [Local LLM Client] Estimated prompt (~{estimated_input_tokens} tokens) plus output budget "
              f"exceeds OLLAMA_NUM_CTX_MAX ({ctx_ceiling}). Using the ceiling; prompt might get truncated.")

    url = f"{ollama_host}/api/chat"
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": system_instruction},
            {"role": "user", "content": prompt}
        ],
        "options": {
            "temperature": 0.1,
            "num_ctx": num_ctx,
            "num_predict": num_predict
        },
        "stream": False
    }
    
    if response_schema is not None:
        # DeepSeek-R1 models use reasoning traces (<think> tags).
        # Passing a JSON schema constraint to Ollama's 'format' parameter uses GBNF grammars
        # which force JSON from token 1, completely blocking the <think> tag and disabling R1's reasoning.
        # Bypass 'format' constraint for deepseek-r1 to let it reason naturally,
        # and rely on our robust regex parser in orchestrator.py to extract JSON.
        if "deepseek-r1" in model.lower():
            if is_debug_mode():
                print("[DEBUG] [Local LLM Client] DeepSeek-R1 detected. Bypassing Ollama 'format' constraint to enable reasoning (<think> tags).", flush=True)
        else:
            raw_schema = response_schema.model_json_schema()
            payload["format"] = simplify_schema_for_local_llm(raw_schema)
        
    if is_debug_mode():
        print("\n" + "="*40 + " OLLAMA LOCAL PROMPT " + "="*40, flush=True)
        print(f"[System Instruction]\n{system_instruction}\n", flush=True)
        print(f"[Prompt Payload]\n{prompt}", flush=True)
        print(f"[Options] num_ctx={num_ctx} num_predict={num_predict} (estimated input ~{estimated_input_tokens} tokens)", flush=True)
        print("="*98 + "\n", flush=True)
        
    max_retries = 3
    retry_delay = 3
    response_data = None
    tid = threading.get_ident()
    profiler.start("LLM API Call: Local Network Duration", thread_id=f"api_{tid}")
    
    for attempt in range(max_retries):
        try:
            r = requests.post(url, json=payload, timeout=120)
            if r.status_code != 200:
                raise RuntimeError(f"Ollama returned status code {r.status_code}: {r.text}")
            response_data = r.json()
            break
        except Exception as e:
            error_str = str(e)
            if attempt < max_retries - 1:
                print(f"[Local LLM Client] Connection error on attempt {attempt+1}. Retrying in {retry_delay} seconds... Error: {error_str.splitlines()[0]}")
                time.sleep(retry_delay)
                retry_delay *= 2
            else:
                profiler.stop("LLM API Call: Local Network Duration", thread_id=f"api_{tid}")
                raise e
    profiler.stop("LLM API Call: Local Network Duration", thread_id=f"api_{tid}")
    
    content = response_data.get("message", {}).get("content", "")
    done_reason = response_data.get("done_reason")
    
    prompt_tokens = response_data.get("prompt_eval_count", 0)
    candidates_tokens = response_data.get("eval_count", 0)
    
    with _lock:
        token_stats["prompt_tokens"] += prompt_tokens
        token_stats["candidates_tokens"] += candidates_tokens
        token_stats["total_tokens"] += (prompt_tokens + candidates_tokens)
        if is_debug_mode():
            print(f"[DEBUG] [Local LLM Client] Call finished. Tokens used in turn: {prompt_tokens + candidates_tokens}, done_reason={done_reason}", flush=True)
            
    if done_reason == "length":
        raise OllamaTruncatedResponseError(
            f"Ollama generation stopped due to length (num_predict={num_predict}) before completing the response. "
            f"Output was likely truncated mid-JSON. Raw content: {content[:500]}"
        )
        
    return content

def call_gemini(prompt: str, system_instruction: str, response_schema) -> str:
    profiler = PipelineProfiler()
    api_key = get_secret("GEMINI_API_KEY")
    if not api_key:
        raise ValueError("GEMINI_API_KEY is not defined. Run `python -m app.config --set-gemini` to configure it.")
        
    client = genai.Client(api_key=api_key)
    model_name = os.getenv("LLM_MODEL", "gemini-3.1-flash-lite")
    
    config_kwargs = {
        "temperature": 0.1,
        "system_instruction": system_instruction,
    }
    
    if response_schema is not None:
        config_kwargs["response_mime_type"] = "application/json"
        config_kwargs["response_schema"] = response_schema
        
    config = types.GenerateContentConfig(**config_kwargs)
    
    max_retries = 3
    retry_delay = 5
    
    if is_debug_mode():
        # Display the full input context being sent to the LLM
        print("\n" + "="*40 + " LLM PROMPT INPUT " + "="*40, flush=True)
        print(f"[System Instruction]\n{system_instruction}\n", flush=True)
        print(f"[Prompt Payload]\n{prompt}", flush=True)
        print("="*98 + "\n", flush=True)
        
    # Rate gating is not checked if executing local tasks
    if get_config().get("LLM_BACKEND") == "ollama":
        pass
    else:
        # Standardize rate limit consumption check across all types of API requests
        prompt_tokens = int((len(prompt) + len(system_instruction)) / 4)
        if _gatekeeper_ref:
            start_gatekeeper_wait = time.perf_counter()
            while True:
                can_go, wait_time = _gatekeeper_ref.can_consume(prompt_tokens)
                if can_go:
                    _gatekeeper_ref.record_call(prompt_tokens)
                    break
                if is_debug_mode():
                    print(f"[DEBUG] [Gatekeeper] Throttling active. Pausing thread for {wait_time:.1f}s...")
                time.sleep(wait_time)
            gatekeeper_wait_duration = time.perf_counter() - start_gatekeeper_wait
            if gatekeeper_wait_duration > 0.01:
                profiler.record("Gatekeeper Wait: RPM/TPM throttling", gatekeeper_wait_duration)
            
    response = None
    tid = threading.get_ident()
    profiler.start("LLM API Call: Network Duration", thread_id=f"api_{tid}")
    
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
            is_transient = (
                "503" in error_str or 
                "429" in error_str or 
                "UNAVAILABLE" in error_str or 
                "RESOURCE_EXHAUSTED" in error_str or
                "quota" in error_str.lower()
            )
            
            if is_transient and attempt < max_retries - 1:
                print(f"[LLM Client] Quota/Transient error on attempt {attempt+1}. Retrying in {retry_delay} seconds... Error: {error_str.splitlines()[0]}")
                time.sleep(retry_delay)
                retry_delay *= 2
            else:
                profiler.stop("LLM API Call: Network Duration", thread_id=f"api_{tid}")
                raise e
                
    profiler.stop("LLM API Call: Network Duration", thread_id=f"api_{tid}")
    
    if not response or not response.text:
        raise RuntimeError("LLM returned an empty response.")
        
    usage = response.usage_metadata
    if usage:
        with _lock:
            token_stats["prompt_tokens"] += getattr(usage, "prompt_token_count", 0) or 0
            token_stats["candidates_tokens"] += getattr(usage, "candidates_token_count", 0) or 0
            token_stats["total_tokens"] += getattr(usage, "total_token_count", 0) or 0
            if is_debug_mode():
                print(f"[DEBUG] [LLM Client] Call finished. Tokens used in turn: {getattr(usage, 'total_token_count', 0)}", flush=True)
        
    return response.text