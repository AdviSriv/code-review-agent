import os
from app.config import get_config

def get_symbol_signature_or_content(filepath: str, line_range: list, fallback_only: bool = False) -> str:
    """
    Fetches the content or the signature of a symbol from a file.
    """
    if not os.path.exists(filepath):
        return f"# File {filepath} not found locally."
        
    try:
        with open(filepath, "r", encoding="utf-8", errors="ignore") as f:
            lines = f.readlines()
            
        start = max(0, line_range[0] - 1)
        end = min(len(lines), line_range[1])
        symbol_lines = lines[start:end]
        
        if fallback_only:
            # Fall back to returning only the definition line
            return symbol_lines[0].strip() if symbol_lines else ""
            
        return "".join(symbol_lines)
    except Exception as e:
        return f"# Error reading symbol from {filepath}: {e}"

def compile_dependency_bundle(filepath: str, parsed_diff_file: dict, symbol_index: dict, dependency_graph: dict) -> str:
    """
    Collects 1-hop file dependencies and signatures under the token cap.
    """
    config = get_config()
    token_budget = config.get("DEP_TOKEN_BUDGET", 1500)
    
    bundle_parts = []
    # Fetch what this file imports and what imports this file
    graph_data = dependency_graph.get(filepath, {"imports": [], "imported_by": []})
    
    imports = graph_data["imports"]
    imported_by = graph_data["imported_by"]
    
    # 1. Pull import signatures
    bundle_parts.append(f"--- File Dependencies for '{filepath}' ---")
    bundle_parts.append(f"Imports from modules: {', '.join(imports)}")
    bundle_parts.append(f"Imported by active workspace files: {', '.join(imported_by)}")
    
    # Estimate token usage (~4 characters per token as a safe rule)
    accumulated_chars = sum(len(x) for x in bundle_parts)
    fallback_mode = False
    
    for dep in imports + imported_by:
        # Match dependency files to their defined symbols
        matching_symbols = [name for name, info in symbol_index.items() if info["file_path"] in dep]
        
        for sym_name in matching_symbols:
            info = symbol_index[sym_name]
            # Try fetching full definition
            sym_content = get_symbol_signature_or_content(info["file_path"], info["line_range"], fallback_only=fallback_mode)
            
            payload = f"\nSymbol Definition ({sym_name} in {info['file_path']}):\n{sym_content}"
            if (accumulated_chars + len(payload)) / 4 > token_budget:
                # If budget is exceeded, switch to fallback mode (signatures only)
                fallback_mode = True
                sym_content = get_symbol_signature_or_content(info["file_path"], info["line_range"], fallback_only=True)
                payload = f"\nSymbol Definition Signature ({sym_name} in {info['file_path']}):\n{sym_content}"
                
            bundle_parts.append(payload)
            accumulated_chars += len(payload)
            
    return "\n".join(bundle_parts)