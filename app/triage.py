import os
import sys
import subprocess
from app.config import get_config
from app.diff_parser import is_ignored_file

def evaluate_docs_only_skip(changed_files: list) -> bool:
    """
    Returns True if 100% of files fall under document, lockfile, or ignored extensions.
    """
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

# Extensible compiler registry mapping extensions to commands
COMPILER_REGISTRY = {
    ".py": {
        "lang": "Python",
        "cmd": lambda files: [sys.executable or "python3", "-m", "py_compile"] + files
    },
    ".js": {
        "lang": "JavaScript",
        "cmd": lambda files: ["node", "--check"] + files
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
    """
    Verifies if a specific compiler executable exists on the VM's path.
    """
    try:
        # Cross-platform check using standard command-line tools
        subprocess.run(["which", name], capture_output=True, check=True)
        return True
    except Exception:
        return False

def run_native_compile_check(changed_files: list) -> dict:
    """
    Runs language-specific compiler or syntax checks on changed files.
    Collects errors dynamically and generates a generic syntax-failure report.
    """
    errors = []
    failed_languages = set()
    
    # 1. Group changed files by their extension
    grouped_files = {}
    for f in changed_files:
        if not os.path.exists(f):
            continue
        ext = os.path.splitext(f)[1].lower()
        if ext in COMPILER_REGISTRY:
            grouped_files.setdefault(ext, []).append(f)
            
    # 2. Run checks for each detected language group
    for ext, files in grouped_files.items():
        rule = COMPILER_REGISTRY[ext]
        lang_name = rule["lang"]
        cmd_generator = rule["cmd"]
        
        # Formulate execution command
        cmd = cmd_generator(files)
        executable = cmd[0]
        
        # Guard: Check if compiler/linter is installed on the host VM
        if not is_executable_available(executable):
            print(f"[Triage] Skipping {lang_name} check: '{executable}' is not installed on VM.")
            continue
            
        res = subprocess.run(cmd, capture_output=True, text=True)
        if res.returncode != 0:
            err = res.stderr.strip() or res.stdout.strip()
            errors.append(f"[{lang_name} Errors]\n{err}")
            failed_languages.add(lang_name)

    # 3. Compile the language-specific feedback
    if errors:
        langs_str = " & ".join(sorted(failed_languages))
        header = f"### ❌ Syntax Check Failed\nCompilation/syntax checks failed on changed **{langs_str}** files. Code review has been skipped to prevent generating hallucinated feedback."
        body = "\n\n".join(errors[:10])
        if len(errors) > 10:
            body += f"\n\n*+ {len(errors) - 10} more errors output truncated*"
            
        full_message = f"{header}\n\n```text\n{body[:1800]}\n```"
        return {"passed": False, "error_msg": full_message}

    return {"passed": True, "error_msg": ""}