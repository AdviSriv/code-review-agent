import json
import os
import re
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from app.models import SubagentResponse, CodeComment
from app.llm_client import call_gemini
from app.prompt_builder import build_subagent_system_instruction
from app.chunker import get_symbol_signature_or_content
from app.config import is_debug_mode

STATIC_ANALYSIS_BASELINE = """
This diff has ALREADY passed: ruff-format/ruff-lint, pyright, semgrep, gitleaks,
pip-audit, pip-licenses, and >90% test coverage. Do NOT re-flag anything those
tools catch mechanically (formatting, unused code, known CVE patterns, type
errors, secrets, license issues). Only raise something if it requires actual
reasoning about intent, cross-file effects, or runtime behavior that no
pattern-matcher could catch.
"""

SUBAGENT_PROMPTS = {
    "Architecture": (
        "Focus ONLY on file/module decoupling, leaky abstractions, interface "
        "boundary violations broken by this diff, and design patterns misapplied. "
        "Ignore naming, import order, or anything formatting-adjacent."
    ),
    "Logic": (
        "Focus ONLY on loop bounds, off-by-one errors, state management bugs, "
        "race conditions, and algorithmic correctness given the visible diff. "
        "Do not comment on types -- pyright already verified those."
    ),
    "Security": (
        "Focus ONLY on runtime security issues such as access bypasses, "
        "authentication bypasses, privilege escalations, and severe logical vulnerabilities."
    )
}

class SharedContextCache:
    def __init__(self, workspace_path: str):
        self.workspace_path = workspace_path
        self.cache = {}

    def contains(self, qual_name: str) -> bool:
        return qual_name in self.cache

    def get_body(self, qual_name: str) -> str:
        return self.cache.get(qual_name, {}).get("body", "")

    def get_metadata(self, qual_name: str) -> dict:
        return self.cache.get(qual_name, {}).get("metadata", {})

    def add(self, qual_name: str, metadata: dict, body: str):
        self.cache[qual_name] = {"metadata": metadata, "body": body}

def get_changed_symbols_in_file(db_path: str, filepath_abs: str, modified_lines: list) -> list:
    changed_symbols = []
    if not os.path.exists(db_path) or not modified_lines:
        return changed_symbols
    try:
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        cursor.execute("""
            SELECT name, qualified_name, line_start, line_end, kind 
            FROM nodes WHERE file_path = ? AND kind != 'File'
        """, (filepath_abs,))
        for node in cursor.fetchall():
            l_start, l_end = node["line_start"] or 1, node["line_end"] or 1
            if any(l_start <= line <= l_end for line in modified_lines):
                changed_symbols.append({
                    "name": node["name"],
                    "qualified_name": node["qualified_name"],
                    "line_range": [l_start, l_end],
                    "kind": node["kind"].lower()
                })
        conn.close()
    except Exception as e:
        if is_debug_mode():
            print(f"[DEBUG] [Orchestrator] Changed symbol matching failed: {e}")
    return changed_symbols

def get_symbol_neighbors(db_path: str, qual_name: str) -> dict:
    neighbors = {"callers": [], "callees": [], "classes": []}
    if not os.path.exists(db_path):
        return neighbors
    try:
        conn = sqlite3.connect(db_path)
        cursor = conn.cursor()
        cursor.execute("SELECT DISTINCT target_qualified FROM edges WHERE source_qualified = ? AND kind IN ('CALLS', 'DEPENDS_ON')", (qual_name,))
        neighbors["callees"] = [r[0] for r in cursor.fetchall() if r[0]]
        cursor.execute("SELECT DISTINCT source_qualified FROM edges WHERE target_qualified = ? AND kind IN ('CALLS', 'DEPENDS_ON')", (qual_name,))
        neighbors["callers"] = [r[0] for r in cursor.fetchall() if r[0]]
        cursor.execute("SELECT DISTINCT target_qualified FROM edges WHERE source_qualified = ? AND kind = 'IMPORTS_FROM'", (qual_name,))
        neighbors["classes"] = [r[0] for r in cursor.fetchall() if r[0]]
        conn.close()
    except Exception:
        pass
    return neighbors

def populate_cache_for_symbol(db_path: str, qual_name: str, symbol_index: dict, cache: SharedContextCache):
    if qual_name in symbol_index and not cache.contains(qual_name):
        meta = symbol_index[qual_name]
        body = get_symbol_signature_or_content(meta["file_path"], meta["line_range"], fallback_only=False)
        cache.add(qual_name, meta, body)
    neighbors = get_symbol_neighbors(db_path, qual_name)
    for caller in neighbors["callers"]:
        if caller in symbol_index and not cache.contains(caller):
            meta = symbol_index[caller]
            body = get_symbol_signature_or_content(meta["file_path"], meta["line_range"], fallback_only=False)
            cache.add(caller, meta, body)
    for callee in neighbors["callees"]:
        if callee in symbol_index and not cache.contains(callee):
            meta = symbol_index[callee]
            body = get_symbol_signature_or_content(meta["file_path"], meta["line_range"], fallback_only=False)
            cache.add(callee, meta, body)
    for cls in neighbors["classes"]:
        if cls in symbol_index and not cache.contains(cls):
            meta = symbol_index[cls]
            body = get_symbol_signature_or_content(meta["file_path"], meta["line_range"], fallback_only=True)
            cache.add(cls, meta, body)

def compile_symbol_bundle(qual_name: str, cache: SharedContextCache, neighbors: dict) -> str:
    lines = [f"\n--- SYMBOL-CENTRIC CONTEXT: {qual_name} ---"]
    if cache.contains(qual_name):
        lines.append(f"\nChanged Symbol implementation:\n{cache.get_body(qual_name)}")
    for caller in neighbors["callers"]:
        if cache.contains(caller):
            lines.append(f"\nDirect Caller '{caller}':\n{cache.get_body(caller)}")
    for callee in neighbors["callees"]:
        if cache.contains(callee):
            lines.append(f"\nDirect Callee '{callee}':\n{cache.get_body(callee)}")
    for cls in neighbors["classes"]:
        if cache.contains(cls):
            lines.append(f"\nRelated Class interface '{cls}':\n{cache.get_body(cls)}")
    return "\n".join(lines)

def run_single_subagent(role: str, chunk_payload: str, conventions_text: str) -> SubagentResponse:
    if is_debug_mode():
        print(f"[DEBUG] [Orchestrator] Spawning worker: '{role}'", flush=True)
    role_focus = SUBAGENT_PROMPTS.get(role, "")
    role_instruction = build_subagent_system_instruction(role, role_focus, conventions_text, STATIC_ANALYSIS_BASELINE)
    prompt = f"Analyze this diff chunk payload matching your role requirements:\n\n{chunk_payload}"
    try:
        raw_out = call_gemini(prompt, role_instruction, SubagentResponse)
        cleaned = raw_out.strip()
        if cleaned.startswith("```json"):
            cleaned = cleaned[7:]
        if cleaned.startswith("```"):
            cleaned = cleaned[3:]
        if cleaned.endswith("```"):
            cleaned = cleaned[:-3]
        
        resp = SubagentResponse(**json.loads(cleaned.strip()))
        
        if is_debug_mode():
            print(f"\n" + "-"*35 + f" SUBAGENT RAW RESPONSE: {role} " + "-"*35, flush=True)
            print(raw_out, flush=True)
            print("-"*98 + "\n", flush=True)
            print(f"[DEBUG] [Orchestrator] Subagent '{role}' evaluated successfully. Findings: {len(resp.findings)}", flush=True)
            
        return resp
    except Exception as e:
        print(f"Subagent {role} failed: {e}", flush=True)
        return SubagentResponse(status="SUCCESS", findings=[])

def run_escalation_routing_llm(escalated_symbols: list, index_candidates: str) -> str:
    system_inst = "You are a code symbol mapping router. Match requested vague expressions to exact symbol paths."
    prompt = f"Symbols requested: {escalated_symbols}\nCandidates list:\n{index_candidates}\nIdentify and return exact match candidates as a simple list."
    try:
        return call_gemini(prompt, system_inst, None)
    except Exception:
        return ""

def crg_search_symbols(db_path: str, query: str, limit: int = 10) -> list:
    results = []
    if not os.path.exists(db_path):
        return results
    try:
        conn = sqlite3.connect(db_path)
        cursor = conn.cursor()
        sanitized = "".join([c if c.isalnum() or c in " _-" else " " for c in query]).strip()
        try:
            cursor.execute("""
                SELECT n.name, n.file_path, n.line_start, n.line_end, n.qualified_name FROM nodes n
                JOIN nodes_fts fts ON n.id = fts.rowid WHERE fts.nodes_fts MATCH ? LIMIT ?
            """, (sanitized, limit))
            for r in cursor.fetchall():
                results.append({"name": r[0], "file_path": r[1], "line_range": [r[2] or 1, r[3] or 1], "qualified_name": r[4]})
        except Exception:
            cursor.execute("""
                SELECT name, file_path, line_start, line_end, qualified_name FROM nodes
                WHERE kind != 'File' AND (name LIKE ? OR qualified_name LIKE ?) LIMIT ?
            """, (f"%{query}%", f"%{query}%", limit))
            for r in cursor.fetchall():
                results.append({"name": r[0], "file_path": r[1], "line_range": [r[2] or 1, r[3] or 1], "qualified_name": r[4]})
        conn.close()
    except Exception:
        pass
    return results

def resolve_escalation(escalated_symbols: list, symbol_index: dict) -> str:
    resolved_findings = []
    vague_requests = []
    if is_debug_mode():
        print(f"[DEBUG] [Resolver] Resolving escalated symbols: {escalated_symbols}")
    for sym in escalated_symbols:
        if sym in symbol_index:
            info = symbol_index[sym]
            content = get_symbol_signature_or_content(info["file_path"], info["line_range"])
            resolved_findings.append(f"--- Resolved Symbol Definition ({sym}) ---\n{content}")
        else:
            vague_requests.append(sym)
    if vague_requests:
        from app.llm_client import _context
        workspace = _context.get("workspace") or "."
        db_path = os.path.join(workspace, ".code-review-graph", "graph.db")
        candidates = []
        for v_req in vague_requests:
            search_results = crg_search_symbols(db_path, v_req, limit=5)
            for res in search_results:
                rel_file = os.path.relpath(res["file_path"], workspace).replace("\\", "/")
                qual_name = res["qualified_name"]
                if "::" in qual_name:
                    parts = qual_name.split("::", 1)
                    try:
                        rel_part = os.path.relpath(parts[0], workspace).replace("\\", "/")
                        qual_name = f"{rel_part}::{parts[1]}"
                    except Exception:
                        pass
                else:
                    try:
                        qual_name = os.path.relpath(qual_name, workspace).replace("\\", "/")
                    except Exception:
                        pass
                candidates.append(f"Symbol: {qual_name} | File: {rel_file} | Range: {res['line_range']}")
        if candidates:
            router_match = run_escalation_routing_llm(vague_requests, "\n".join(candidates[:10]))
            resolved_findings.append(f"--- Router Resolved Candidates ---\n{router_match}")
    return "\n\n".join(resolved_findings)

def orchestrate_symbol_review(changed_symbol: dict, symbol_index: dict, conventions_text: str, cache: SharedContextCache, db_path: str) -> list:
    qual_name = changed_symbol["qualified_name"]
    populate_cache_for_symbol(db_path, qual_name, symbol_index, cache)
    neighbors = get_symbol_neighbors(db_path, qual_name)
    symbol_bundle = compile_symbol_bundle(qual_name, cache, neighbors)
    chunk_payload = f"Changed Code Block: {changed_symbol['name']}\nRange: {changed_symbol['line_range']}\nKind: {changed_symbol['kind']}\n\n{symbol_bundle}"
    
    roles = ["Architecture", "Logic", "Security"]
    with ThreadPoolExecutor(max_workers=3) as executor:
        futures = {executor.submit(run_single_subagent, role, chunk_payload, conventions_text): role for role in roles}
        results = {futures[f]: f.result() for f in futures}
        
    escalated_symbols, escalated_roles, tracked_findings = [], [], {}
    for role, resp in results.items():
        for f in resp.findings:
            tracked_findings[(role, f.position)] = f
        if resp.status == "NEEDS_CONTEXT" and resp.context_request:
            req = resp.context_request
            functions_req = list(req.functions)
            classes_req = list(req.classes)
            
            # Deterministic natural-language fallback parser to recover missing items
            if not functions_req and not classes_req and req.why:
                why_symbols = re.findall(r"`([A-Za-z_][A-Za-z0-9_]*)`", req.why)
                why_symbols += re.findall(r"\b([a-z_][a-z0-9_]{3,})\b", req.why)
                for w_sym in why_symbols:
                    if w_sym not in ["functions", "classes", "why"]:
                        functions_req.append(w_sym)
            
            escalated_roles.append(role)
            escalated_symbols.extend(functions_req + classes_req + req.configs)

    if escalated_symbols and escalated_roles:
        newly_retrieved_context = []
        for sym in escalated_symbols:
            if cache.contains(sym):
                newly_retrieved_context.append(f"--- Cached Symbol Definition ({sym}) ---\n{cache.get_body(sym)}")
            elif sym in symbol_index:
                meta = symbol_index[sym]
                body = get_symbol_signature_or_content(meta["file_path"], meta["line_range"], fallback_only=False)
                cache.add(sym, meta, body)
                newly_retrieved_context.append(f"--- Resolved Symbol Definition ({sym}) ---\n{body}")
            else:
                search_results = crg_search_symbols(db_path, sym, limit=5)
                exact_match = None
                for res in search_results:
                    if res["name"] == sym:
                        exact_match = res
                        break
                target_node = exact_match if exact_match else (search_results[0] if search_results else None)
                if target_node:
                    rel_file = os.path.relpath(target_node["file_path"], cache.workspace_path).replace("\\", "/")
                    body = get_symbol_signature_or_content(rel_file, target_node["line_range"], fallback_only=False)
                    cache.add(sym, {"file_path": rel_file, "line_range": target_node["line_range"]}, body)
                    newly_retrieved_context.append(f"--- Search Resolved Symbol Definition ({sym}) ---\n{body}")

        augmented = "\n\n".join(newly_retrieved_context)
        retry_payload = f"{chunk_payload}\n\n--- Escalated Resolved Context (Pass 2) ---\n{augmented}"
        with ThreadPoolExecutor(max_workers=len(escalated_roles)) as executor:
            retry_futures = {executor.submit(run_single_subagent, role, retry_payload, conventions_text): role for role in escalated_roles}
            for rf in retry_futures:
                role = retry_futures[rf]
                resp = rf.result()
                for f in resp.findings:
                    tracked_findings[(role, f.position)] = f

    final_comments = []
    for (role, position), f in tracked_findings.items():
        if f.comment and f.comment.strip():
            final_comments.append(CodeComment(
                file="", position=f.position, severity=f.severity, role=role,
                comment=f.comment, confidence=f.confidence, references_specific_identifier=f.references_specific_identifier
            ))
    return final_comments

def orchestrate_chunk_review(chunk_payload: str, conventions_text: str, symbol_index: dict) -> list:
    roles = ["Architecture", "Logic", "Security"]
    with ThreadPoolExecutor(max_workers=3) as executor:
        futures = {executor.submit(run_single_subagent, role, chunk_payload, conventions_text): role for role in roles}
        results = {futures[f]: f.result() for f in futures}
    tracked_findings = {}
    for role, resp in results.items():
        for f in resp.findings:
            tracked_findings[(role, f.position)] = f
    final_comments = []
    for (role, position), f in tracked_findings.items():
        if f.comment and f.comment.strip():
            final_comments.append(CodeComment(
                file="", position=f.position, severity=f.severity, role=role,
                comment=f.comment, confidence=f.confidence, references_specific_identifier=f.references_specific_identifier
            ))
    return final_comments