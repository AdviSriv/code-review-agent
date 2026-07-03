# ===== /root/code-review-agent/app/triage.py =====
import os
import sys
import subprocess
from app.config import get_config
from app.diff_parser import is_ignored_file
from app.profiler import PipelineProfiler

def evaluate_docs_only_skip(changed_files: list) -> bool:
    if not changed_files:
        return True
        
    config = get_config()
    skip_extensions = config.get("TRIAGE_SKIP_PATTERNS", [])
    
    for f in changed_files:
        if is_ignored_file(f):
            continue
        ext = os.path.splitext(f)[1].lower()
        if ext not in skip_extensions:
            return False
            
    return True

# Comprehensive compiler and static logic checks
COMPILER_REGISTRY = {
    ".py": {
        "lang": "Python",
        "cmd": lambda files: [sys.executable or "python3", "-m", "py_compile"] + files,
        # Secondary static rules to execute if available on the VM
        "linters": [
            {"exec": "ruff", "args": lambda files: ["ruff", "check", "--no-fix"] + files},
            {"exec": "mypy", "args": lambda files: ["mypy", "--ignore-missing-imports"] + files}
        ]
    },
    ".js": {
        "lang": "JavaScript",
        "cmd": lambda files: ["node", "--check"] + files,
        "linters": [
            {"exec": "eslint", "args": lambda files: ["npx", "eslint"] + files}
        ]
    },
    ".ts": {
        "lang": "TypeScript",
        "cmd": lambda files: ["tsc", "--noEmit"] + files
    },
    ".go": {
        "lang": "Go",
        "cmd": lambda files: ["go", "vet", "./..."]
    },
    ".rs": {
        "lang": "Rust",
        "cmd": lambda files: ["cargo", "check"]
    }
}

def is_executable_available(name: str) -> bool:
    try:
        subprocess.run(["which", name], capture_output=True, check=True)
        return True
    except Exception:
        return False

def run_native_compile_check(changed_files: list, workspace_path: str = ".") -> dict:
    """
    Runs compiler/syntax passes, followed by local lint/static checkers if available.
    """
    profiler = PipelineProfiler()
    errors = []
    failed_languages = set()
    
    grouped_files = {}
    for f in changed_files:
        if not os.path.exists(f):
            continue
        ext = os.path.splitext(f)[1].lower()
        if ext in COMPILER_REGISTRY:
            grouped_files.setdefault(ext, []).append(f)
            
    for ext, files in grouped_files.items():
        rule = COMPILER_REGISTRY[ext]
        lang_name = rule["lang"]
        cmd_generator = rule["cmd"]
        
        # 1. Run mandatory base compilation checks
        cmd = cmd_generator(files)
        executable = cmd[0]
        
        if not is_executable_available(executable):
            print(f"[Triage] Compiler '{executable}' not found on VM. Skipping check.")
            continue
            
        profiler.start(f"Triage Compiler: {lang_name} native check")
        res = subprocess.run(cmd, capture_output=True, text=True, cwd=workspace_path)
        profiler.stop(f"Triage Compiler: {lang_name} native check")
        
        if res.returncode != 0:
            err = res.stderr.strip() or res.stdout.strip()
            errors.append(f"[{lang_name} Syntax Error]\n{err}")
            failed_languages.add(lang_name)
            continue  # Re-raise compile errors immediately
            
        # 2. Run runtime static assertions (e.g. Ruff/Mypy/Eslint) if present
        for linter in rule.get("linters", []):
            lexec = linter["exec"]
            if is_executable_available(lexec):
                profiler.start(f"Triage Linter: {lang_name} {lexec} check")
                lcmd = linter["args"](files)
                lres = subprocess.run(lcmd, capture_output=True, text=True, cwd=workspace_path)
                profiler.stop(f"Triage Linter: {lang_name} {lexec} check")
                if lres.returncode != 0:
                    lerr = lres.stderr.strip() or lres.stdout.strip()
                    errors.append(f"[{lang_name} {lexec.capitalize()} Logic/Type Error]\n{lerr}")
                    failed_languages.add(lang_name)

    if errors:
        langs_str = " & ".join(sorted(failed_languages))
        header = f"### ❌ Local Triage Verification Failed\nStatic validation failed on changed **{langs_str}** files. Build-level issues must be resolved before triggering AI code reviews."
        body = "\n\n".join(errors[:10])
        if len(errors) > 10:
            body += f"\n\n*+ {len(errors) - 10} more errors output truncated*"
            
        full_message = f"{header}\n\n```text\n{body[:1800]}\n```"
        return {"passed": False, "error_msg": full_message}

    return {"passed": True, "error_msg": ""}