import json
from concurrent.futures import ThreadPoolExecutor
from app.models import SubagentResponse, CodeComment
from app.llm_client import call_gemini
from app.prompt_builder import build_subagent_system_instruction  # <--- Updated Import
from app.repo_intelligence import load_repo_intelligence
from app.chunker import get_symbol_signature_or_content

def run_single_subagent(role: str, chunk_payload: str, conventions_text: str) -> SubagentResponse:
    # Build a dedicated instruction set with the correct JSON schema
    role_instruction = build_subagent_system_instruction(role, conventions_text)
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
        
        data = json.loads(cleaned.strip())
        return SubagentResponse(**data)
    except Exception as e:
        print(f"Subagent {role} failed: {e}")
        return SubagentResponse(status="SUCCESS", confidence=0.0, findings=[])

def run_escalation_routing_llm(escalated_symbols: list, index_candidates: str) -> str:
    system_inst = "You are a code symbol mapping router. Match requested vague expressions to exact symbol paths."
    prompt = f"Symbols requested: {escalated_symbols}\nCandidates list:\n{index_candidates}\nIdentify and return exact match candidates as a simple list."
    try:
        return call_gemini(prompt, system_inst, None)
    except Exception:
        return ""

def resolve_escalation(escalated_symbols: list, symbol_index: dict) -> str:
    resolved_findings = []
    vague_requests = []
    
    for sym in escalated_symbols:
        if sym in symbol_index:
            info = symbol_index[sym]
            content = get_symbol_signature_or_content(info["file_path"], info["line_range"])
            resolved_findings.append(f"--- Resolved Symbol Definition ({sym}) ---\n{content}")
        else:
            vague_requests.append(sym)
            
    if vague_requests:
        candidates = []
        for name, info in symbol_index.items():
            for v_req in vague_requests:
                if v_req.lower() in name.lower() or name.lower() in v_req.lower():
                    candidates.append(f"Symbol: {name} | File: {info['file_path']} | Range: {info['line_range']}")
                    
        if candidates:
            router_match = run_escalation_routing_llm(vague_requests, "\n".join(candidates[:10]))
            resolved_findings.append(f"--- Router Resolved Candidates ---\n{router_match}")
            
    return "\n\n".join(resolved_findings)

def orchestrate_chunk_review(chunk_payload: str, conventions_text: str, symbol_index: dict) -> list:
    roles = ["Security", "Architecture", "Logic", "Maintainability"]
    
    # Run the 4 subagents concurrently
    with ThreadPoolExecutor(max_workers=4) as executor:
        futures = {executor.submit(run_single_subagent, role, chunk_payload, conventions_text): role for role in roles}
        results = {futures[f]: f.result() for f in futures}
        
    escalated_symbols = []
    valid_findings = []
    escalated_roles = []
    
    for role, resp in results.items():
        if resp.status == "NEEDS_CONTEXT":
            escalated_roles.append(role)
            for f in resp.findings:
                if f.escalated_symbol:
                    escalated_symbols.append(f.escalated_symbol)
        else:
            valid_findings.extend(resp.findings)

    if escalated_symbols and len(escalated_roles) > 0:
        print(f"[Orchestrator] Escalation triggered for symbols: {escalated_symbols}")
        augmented_context = resolve_escalation(escalated_symbols, symbol_index)
        retry_payload = f"{chunk_payload}\n\n--- Escalated Resolved Context ---\n{augmented_context}"
        
        with ThreadPoolExecutor(max_workers=len(escalated_roles)) as executor:
            retry_futures = {executor.submit(run_single_subagent, role, retry_payload, conventions_text): role for role in escalated_roles}
            for rf in retry_futures:
                resp = rf.result()
                valid_findings.extend(resp.findings)

    final_comments = []
    for f in valid_findings:
        final_comments.append(CodeComment(
            file="",
            position=f.position,
            severity=f.severity,
            comment=f.comment,
            references_specific_identifier=f.references_specific_identifier
        ))
        
    return final_comments