import json
import os
import re
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from app.models import SubagentResponse, CodeComment
from app.llm_client import call_gemini, call_local_llm
from app.prompt_builder import build_subagent_system_instruction
from app.chunker import get_symbol_signature_or_content
from app.config import get_config, is_debug_mode
from app.profiler import PipelineProfiler

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

# --- Path Alignment Helpers ---

def make_rel_path(path_str: str, workspace_dir: str) -> str:
    """Normalizes an absolute path to a relative representation."""
    if not path_str:
        return ""
    try:
        rel = os.path.relpath(path_str, workspace_dir)
        return rel.replace("\\", "/")
    except ValueError:
        return path_str.replace("\\", "/")

def make_rel_qual_name(qual_name: str, workspace_dir: str) -> str:
    """Converts absolute qualified names from SQLite nodes to relative strings."""
    if "::" in qual_name:
        parts = qual_name.split("::", 1)
        rel_part = make_rel_path(parts[0], workspace_dir)
        return f"{rel_part}::{parts[1]}"
    else:
        return make_rel_path(qual_name, workspace_dir)

def make_abs_qual_name(qual_name: str, workspace_dir: str) -> str:
    """Converts relative in-memory qualified names back to absolute for database indexing."""
    if "::" in qual_name:
        parts = qual_name.split("::", 1)
        abs_part = os.path.abspath(os.path.join(workspace_dir, parts[0])).replace("\\", "/")
        return f"{abs_part}::{parts[1]}"
    else:
        return os.path.abspath(os.path.join(workspace_dir, qual_name)).replace("\\", "/")

# --- Database & Ast Index Queries ---

def get_changed_symbols_in_file(db_path: str, filepath_abs: str, modified_lines: list) -> list:
    changed_symbols = []
    if not os.path.exists(db_path) or not modified_lines:
        return changed_symbols
    
    workspace_dir = os.path.dirname(os.path.dirname(os.path.abspath(db_path)))
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
                # Ensure the qualified name returned is relative to match symbol_index keys
                rel_qual = make_rel_qual_name(node["qualified_name"], workspace_dir)
                changed_symbols.append({
                    "name": node["name"],
                    "qualified_name": rel_qual,
                    "line_range": [l_start, l_end],
                    "kind": node["kind"].lower()
                })
        conn.close()
    except Exception as e:
        if is_debug_mode():
            print(f"[DEBUG] [Orchestrator] Changed symbol matching failed: {e}")
    return changed_symbols

def get_symbol_neighbors(db_path: str, qual_name: str, max_hops: int = 1) -> dict:
    neighbors = {"callers": [], "callees": [], "classes": []}
    if not os.path.exists(db_path):
        return neighbors
    
    workspace_dir = os.path.dirname(os.path.dirname(os.path.abspath(db_path)))
    # Convert incoming relative qualified name to absolute to match SQLite contents
    abs_qual_name = make_abs_qual_name(qual_name, workspace_dir)
    max_hops = max(1, int(max_hops or 1))
    
    try:
        conn = sqlite3.connect(db_path)
        cursor = conn.cursor()
        
        # Keep classes (IMPORTS_FROM) single-hop only
        cursor.execute("SELECT DISTINCT target_qualified FROM edges WHERE source_qualified = ? AND kind = 'IMPORTS_FROM'", (abs_qual_name,))
        neighbors["classes"] = [make_rel_qual_name(r[0], workspace_dir) for r in cursor.fetchall() if r[0]]
        
        # Traverse callers and callees (CALLS, DEPENDS_ON) up to max_hops via BFS
        def bfs(direction_sql: str) -> set:
            visited, frontier = {abs_qual_name}, {abs_qual_name}
            for _ in range(max_hops):
                nxt = set()
                for sym in frontier:
                    cursor.execute(direction_sql, (sym,))
                    for r in cursor.fetchall():
                        if r[0] and r[0] not in visited:
                            visited.add(r[0])
                            nxt.add(r[0])
                if not nxt:
                    break
                frontier = nxt
            visited.discard(abs_qual_name)
            return visited
            
        callees = bfs("SELECT DISTINCT target_qualified FROM edges WHERE source_qualified = ? AND kind IN ('CALLS', 'DEPENDS_ON')")
        callers = bfs("SELECT DISTINCT source_qualified FROM edges WHERE target_qualified = ? AND kind IN ('CALLS', 'DEPENDS_ON')")
        
        neighbors["callees"] = [make_rel_qual_name(s, workspace_dir) for s in callees]
        neighbors["callers"] = [make_rel_qual_name(s, workspace_dir) for s in callers]
        
        conn.close()
    except Exception:
        pass
    return neighbors

def populate_cache_for_symbol(db_path: str, qual_name: str, symbol_index: dict, cache: SharedContextCache, dep_hops: int = 1):
    if qual_name in symbol_index and not cache.contains(qual_name):
        meta = symbol_index[qual_name]
        body = get_symbol_signature_or_content(meta["file_path"], meta["line_range"], fallback_only=False)
        cache.add(qual_name, meta, body)
    neighbors = get_symbol_neighbors(db_path, qual_name, max_hops=dep_hops)
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

def get_proactive_semantic_context(query_text: str, symbol_index: dict, cache: SharedContextCache, db_path: str, limit: int = 5) -> str:
    """
    Executes an automatic (Stage 1) proactive semantic search seeded from the query/diff text.
    Applies a 1-hop Graph Expansion (callers and callees) on the semantically discovered matches.
    Injects the matches and expanded neighbor signatures directly into the cache.
    """
    from app.llm_client import _context
    owner = _context.get("owner")
    repo = _context.get("repo")
    if not (owner and repo and symbol_index):
        return ""
        
    collection_name = f"{owner}_{repo}".lower().replace("-", "_").replace(".", "_")
    try:
        from app.semantic_index import search_semantic
        # Safety Truncation: Prevents massive diff payloads from exceeding model embedding context limits
        truncated_query = query_text[:8000]
        matches = search_semantic(truncated_query, collection_name, limit=limit)
        semantic_lines = []
        
        for match in matches:
            payload_data = match.get("payload", {})
            m_qual = payload_data.get("symbol")
            if m_qual and m_qual in symbol_index:
                m_meta = symbol_index[m_qual]
                if not cache.contains(m_qual):
                    m_body = get_symbol_signature_or_content(m_meta["file_path"], m_meta["line_range"], fallback_only=False)
                    cache.add(m_qual, m_meta, m_body)
                
                semantic_lines.append(f"\nSemantically Related Pattern Code '{m_qual}':\n{cache.get_body(m_qual)}")
                
                # 1-hop Graph Expansion on the semantic match to surface structural relevance
                neighbors = get_symbol_neighbors(db_path, m_qual, max_hops=1)
                for caller in neighbors["callers"][:2]:  # Limit to avoid bloating token budget
                    if caller in symbol_index and not cache.contains(caller):
                        n_meta = symbol_index[caller]
                        n_body = get_symbol_signature_or_content(n_meta["file_path"], n_meta["line_range"], fallback_only=True)
                        cache.add(caller, n_meta, n_body)
                        semantic_lines.append(f"  └─ Caller of match '{caller}': {n_body}")
                        
                for callee in neighbors["callees"][:2]:  # Limit to avoid bloating token budget
                    if callee in symbol_index and not cache.contains(callee):
                        n_meta = symbol_index[callee]
                        n_body = get_symbol_signature_or_content(n_meta["file_path"], n_meta["line_range"], fallback_only=True)
                        cache.add(callee, n_meta, n_body)
                        semantic_lines.append(f"  └─ Callee of match '{callee}': {n_body}")
                
        if semantic_lines:
            return "\n--- PROACTIVE SEMANTIC DISCOVERIES & EXPANDED GRAPH CONTEXT ---\n" + "\n".join(semantic_lines)
    except Exception as e:
        if is_debug_mode():
            print(f"[DEBUG] [Orchestrator] Proactive semantic lookups bypassed: {e}")
    return ""

def run_single_subagent(role: str, chunk_payload: str, conventions_text: str) -> SubagentResponse:
    if is_debug_mode():
        print(f"[DEBUG] [Orchestrator] Spawning worker: '{role}'", flush=True)
    role_focus = SUBAGENT_PROMPTS.get(role, "")
    
    # Dynamically verify if static baseline is enabled
    config = get_config()
    baseline = STATIC_ANALYSIS_BASELINE if config.get("ENABLE_STATIC_BASELINE", True) else ""
    
    role_instruction = build_subagent_system_instruction(role, role_focus, conventions_text, baseline)
    prompt = f"Analyze this diff chunk payload matching your role requirements:\n\n{chunk_payload}"
    
    # Propagate exception up directly (No Swallow) to prevent silent empty-finding successes
    backend = config.get("LLM_BACKEND", "gemini")
    if backend == "ollama":
        raw_out = call_local_llm(prompt, role_instruction, SubagentResponse)
    else:
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

def run_escalation_routing_llm(escalated_symbols: list, index_candidates: str) -> str:
    system_inst = "You are a code symbol mapping router. Match requested vague expressions to exact symbol paths."
    prompt = f"Symbols requested: {escalated_symbols}\nCandidates list:\n{index_candidates}\nIdentify and return exact match candidates as a simple list."
    try:
        config = get_config()
        if config.get("LLM_BACKEND") == "ollama":
            return call_local_llm(prompt, system_inst, None)
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

def run_escalation_rounds(chunk_payload: str, conventions_text: str, symbol_index: dict, db_path: str, cache: SharedContextCache, max_rounds: int = 1) -> list:
    """
    Consolidated multi-pass loop orchestrator. Loops dynamically through context escalations,
    resolving symbol and semantic query requests up to max_rounds [3].
    """
    profiler = PipelineProfiler()
    roles = ["Architecture", "Logic", "Security"]
    
    # Parity parallelization logic to protect Ollama CPU limits [3]
    backend = get_config().get("LLM_BACKEND", "gemini")
    
    profiler.start("Orchestrator: Pass 1 LLM Reviews")
    if backend == "ollama":
        # Sequential execution on local backends to prevent thread thrashing
        results = {}
        for role in roles:
            results[role] = run_single_subagent(role, chunk_payload, conventions_text)
    else:
        # Multi-threaded parallel execution for Cloud API endpoints
        with ThreadPoolExecutor(max_workers=3) as executor:
            futures = {executor.submit(run_single_subagent, role, chunk_payload, conventions_text): role for role in roles}
            results = {futures[f]: f.result() for f in futures}
    profiler.stop("Orchestrator: Pass 1 LLM Reviews")
        
    escalated_symbols, escalated_roles, tracked_findings = [], [], {}
    semantic_queries_map = {}

    for role, resp in results.items():
        for f in resp.findings:
            tracked_findings[(role, f.position)] = f
        if resp.status == "NEEDS_CONTEXT" and resp.context_request:
            req = resp.context_request
            functions_req = list(req.functions)
            classes_req = list(req.classes)
            
            config = get_config()
            max_calls = config.get("MAX_ROUTER_CALLS_PER_CHUNK", 1)
            
            subagent_queries = getattr(req, "semantic_queries", []) or []
            if subagent_queries:
                semantic_queries_map.setdefault(role, []).extend(subagent_queries)

            if not functions_req and not classes_req and not subagent_queries and req.why:
                why_symbols = re.findall(r"`([A-Za-z_][A-Za-z0-9_]*)`", req.why)
                candidate_words = re.findall(r"\b([A-Za-z_][A-Za-z0-9_]{4,})\b", req.why)
                for w in candidate_words:
                    if w.lower() not in ["functions", "classes", "why", "context", "symbol", "definition", "implementation"]:
                        why_symbols.append(w)
                
                for w_sym in why_symbols[:max_calls]:
                    functions_req.append(w_sym)
                        
            if functions_req or classes_req or req.configs or subagent_queries:
                escalated_roles.append(role)
                escalated_symbols.extend(functions_req + classes_req + req.configs)

    round_idx = 0
    payload = chunk_payload
    
    from app.llm_client import _context
    owner = _context.get("owner")
    repo = _context.get("repo")
    collection_name = f"{owner}_{repo}".lower().replace("-", "_").replace(".", "_") if (owner and repo) else ""

    # Dynamic multi-round escalation loop (Stage 2)
    while (escalated_symbols or semantic_queries_map) and escalated_roles and round_idx < max_rounds:
        profiler.start(f"Orchestrator: Escalation AST DB Resolving Round {round_idx + 1}")
        newly_retrieved_context = []

        if semantic_queries_map:
            from app.semantic_index import search_semantic
            for role, queries in list(semantic_queries_map.items()):
                for query in queries[:3]:
                    matches = search_semantic(query, collection_name, limit=3)
                    for match in matches:
                        meta_payload = match.get("payload", {})
                        m_qual = meta_payload.get("symbol")
                        if m_qual and m_qual in symbol_index:
                            m_meta = symbol_index[m_qual]
                            if not cache.contains(m_qual):
                                m_body = get_symbol_signature_or_content(m_meta["file_path"], m_meta["line_range"], fallback_only=False)
                                cache.add(m_qual, m_meta, m_body)
                            newly_retrieved_context.append(f"--- Semantic Vector Search Match ({m_qual}) ---\n{cache.get_body(m_qual)}")
            semantic_queries_map.clear()

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
                else:
                    base_sha = _context.get("base_sha")
                    from app.repo_intelligence import INTEL_DIR
                    base_db_path = os.path.join(INTEL_DIR, f"{base_sha}.db") if base_sha else ""
                    
                    base_callers = []
                    if base_db_path and os.path.exists(base_db_path):
                        try:
                            conn = sqlite3.connect(base_db_path)
                            cursor = conn.cursor()
                            cursor.execute("""
                                SELECT DISTINCT source_qualified FROM edges 
                                WHERE (target_qualified = ? OR target_qualified LIKE ?) 
                                  AND kind IN ('IMPORTS_FROM', 'CALLS', 'DEPENDS_ON')
                            """, (sym, f"%::{sym}"))
                            base_callers = [os.path.basename(r[0].split("::")[0]) for r in cursor.fetchall() if r[0]]
                            conn.close()
                        except Exception:
                            pass
                    
                    if base_callers:
                        clean_callers = list(set(base_callers))
                        err_msg = f"Notice: Symbol '{sym}' was DELETED in this commit. Pre-change (BASE) commit caller graph analysis shows it was imported/referenced only by these files: {clean_callers}."
                    else:
                        err_msg = f"Notice: Symbol '{sym}' does not exist in the active HEAD commit and had no registered baseline callers."
                        
                    newly_retrieved_context.append(f"--- Symbol Deletion Notice ({sym}) ---\n{err_msg}")

        augmented = "\n\n".join(newly_retrieved_context)
        payload = f"{payload}\n\n--- Escalated Resolved Context (Pass {round_idx + 2}) ---\n{augmented}"
        profiler.stop(f"Orchestrator: Escalation AST DB Resolving Round {round_idx + 1}")
        
        profiler.start(f"Orchestrator: Pass {round_idx + 2} LLM Reviews")
        next_roles, next_symbols = [], []
        
        if backend == "ollama":
            # Sequential re-evaluation
            for role in escalated_roles:
                resp = run_single_subagent(role, payload, conventions_text)
                for f in resp.findings:
                    tracked_findings[(role, f.position)] = f
                if resp.status == "NEEDS_CONTEXT" and resp.context_request:
                    req = resp.context_request
                    funcs, classes = list(req.functions), list(req.classes)
                    sub_q = getattr(req, "semantic_queries", []) or []
                    if sub_q:
                        semantic_queries_map.setdefault(role, []).extend(sub_q)
                    if funcs or classes or req.configs or sub_q:
                        next_roles.append(role)
                        next_symbols.extend(funcs + classes + req.configs)
        else:
            # Parallel re-evaluation
            with ThreadPoolExecutor(max_workers=len(escalated_roles)) as executor:
                retry_futures = {executor.submit(run_single_subagent, role, payload, conventions_text): role for role in escalated_roles}
                for rf in retry_futures:
                    role = retry_futures[rf]
                    resp = rf.result()
                    for f in resp.findings:
                        tracked_findings[(role, f.position)] = f
                    if resp.status == "NEEDS_CONTEXT" and resp.context_request:
                        req = resp.context_request
                        funcs, classes = list(req.functions), list(req.classes)
                        sub_q = getattr(req, "semantic_queries", []) or []
                        if sub_q:
                            semantic_queries_map.setdefault(role, []).extend(sub_q)
                        if funcs or classes or req.configs or sub_q:
                            next_roles.append(role)
                            next_symbols.extend(funcs + classes + req.configs)
                        
        profiler.stop(f"Orchestrator: Pass {round_idx + 2} LLM Reviews")
        escalated_roles, escalated_symbols = next_roles, next_symbols
        round_idx += 1

    final_comments = []
    for (role, position), f in tracked_findings.items():
        if f.comment and f.comment.strip():
            final_comments.append(CodeComment(
                file="", position=f.position, severity=f.severity, role=role,
                comment=f.comment, confidence=f.confidence, references_specific_identifier=f.references_specific_identifier
            ))
    return final_comments

def orchestrate_symbol_review(changed_symbol: dict, symbol_index: dict, conventions_text: str, cache: SharedContextCache, db_path: str, dep_hops: int = 1, max_escalation_rounds: int = 1) -> list:
    qual_name = changed_symbol["qualified_name"]
    populate_cache_for_symbol(db_path, qual_name, symbol_index, cache, dep_hops=dep_hops)
    neighbors = get_symbol_neighbors(db_path, qual_name, max_hops=dep_hops)
    symbol_bundle = compile_symbol_bundle(qual_name, cache, neighbors)
    
    # ─── Stage 1: Automatic Proactive Semantic Context & Graph Expansion Pass ───
    proactive_context = ""
    changed_code = cache.get_body(qual_name)
    if changed_code:
        proactive_context = get_proactive_semantic_context(changed_code, symbol_index, cache, db_path, limit=5)
        
    chunk_payload = f"Changed Code Block: {changed_symbol['name']}\nRange: {changed_symbol['line_range']}\nKind: {changed_symbol['kind']}\n\n{symbol_bundle}"
    if proactive_context:
        chunk_payload = f"{chunk_payload}\n{proactive_context}"
    
    return run_escalation_rounds(chunk_payload, conventions_text, symbol_index, db_path, cache, max_rounds=max_escalation_rounds)

def orchestrate_chunk_review(chunk_payload: str, conventions_text: str, symbol_index: dict, db_path: str, cache: SharedContextCache, max_escalation_rounds: int = 1) -> list:
    # ─── Stage 1: Automatic Proactive Semantic Context & Graph Expansion Pass ───
    proactive_context = get_proactive_semantic_context(chunk_payload, symbol_index, cache, db_path, limit=5)
    payload = chunk_payload
    if proactive_context:
        payload = f"{chunk_payload}\n{proactive_context}"
        
    return run_escalation_rounds(payload, conventions_text, symbol_index, db_path, cache, max_rounds=max_escalation_rounds)