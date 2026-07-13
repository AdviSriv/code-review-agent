import uuid
import hashlib
import os
import requests
from concurrent.futures import ThreadPoolExecutor
from app.config import get_config, is_debug_mode
from app.chunker import get_symbol_signature_or_content

# Stable namespace for generating deterministic UUIDs across commits
UUID_NAMESPACE = uuid.UUID("d0b13bf2-972a-43c3-8e7c-2b6389f4bca9")

def get_symbol_uuid(repo_name: str, qual_name: str) -> str:
    """Generates a stable, deterministic UUID for a symbol mapping in Qdrant."""
    return str(uuid.uuid5(UUID_NAMESPACE, f"{repo_name}::{qual_name}"))

def ensure_qdrant_collection(collection_name: str) -> bool:
    """Ensures that the target collection exists in the local Qdrant instance."""
    qdrant_host = get_config().get("QDRANT_HOST", "http://localhost:6333")
    url = f"{qdrant_host}/collections/{collection_name}"
    
    try:
        r = requests.get(url, timeout=5)
        if r.status_code == 200:
            return True
        
        # Dynamically determine vector size from the configured embedding model
        test_vector = get_ollama_embedding("test", task="document")
        vector_size = len(test_vector) if test_vector else 768
        if is_debug_mode():
            print(f"[DEBUG] [SemanticIndex] Creating collection '{collection_name}' with vector size {vector_size}.")
        
        # Collection missing, create it
        payload = {
            "vectors": {
                "size": vector_size,
                "distance": "Cosine"
            }
        }
        create_res = requests.put(url, json=payload, timeout=10)
        return create_res.status_code == 200
    except Exception as e:
        if is_debug_mode():
            print(f"[DEBUG] [SemanticIndex] Qdrant connection error: {e}")
        return False

def scroll_active_points(collection_name: str) -> dict:
    """Scrolls and retrieves all existing points, hashes, and paths stored in the collection."""
    qdrant_host = get_config().get("QDRANT_HOST", "http://localhost:6333")
    url = f"{qdrant_host}/collections/{collection_name}/points/scroll"
    points = {}
    next_page = None
    
    while True:
        payload = {"limit": 100, "with_payload": ["hash", "path", "symbol"]}
        if next_page:
            payload["offset"] = next_page
            
        try:
            r = requests.post(url, json=payload, timeout=10)
            if r.status_code != 200:
                break
            data = r.json().get("result", {})
            for point in data.get("points", []):
                points[point["id"]] = {
                    "hash": point.get("payload", {}).get("hash"),
                    "path": point.get("payload", {}).get("path"),
                    "symbol": point.get("payload", {}).get("symbol")
                }
            next_page = data.get("next_page_offset")
            if not next_page:
                break
        except Exception:
            break
            
    return points

def get_ollama_embedding(text: str, task: str = "document") -> list:
    """Retrieves embedding vector from the local Ollama API, appending the correct task prefix."""
    config = get_config()
    ollama_host = config.get("OLLAMA_HOST", "http://localhost:11434")
    model = config.get("EMBEDDING_MODEL", "nomic-embed-text")
    timeout = config.get("EMBEDDING_TIMEOUT", 30.0)
    max_chars = config.get("EMBEDDING_MAX_CHARS", 6000)
    url = f"{ollama_host}/api/embeddings"
    
    # Client-side truncation to prevent "the input length exceeds the context length" Ollama 500 errors
    if len(text) > max_chars:
        text = text[:max_chars]
        
    prefix = "search_document: " if task == "document" else "search_query: "
    full_prompt = f"{prefix}{text}"
    
    try:
        r = requests.post(url, json={"model": model, "prompt": full_prompt}, timeout=timeout)
        if r.status_code == 200:
            return r.json().get("embedding", [])
        else:
            print(f"[SemanticIndex] Warning: Embedding request failed with status {r.status_code}: {r.text}")
    except Exception as e:
        print(f"[SemanticIndex] Error generating embedding vector: {e}")
    return []

def sync_semantic_index(owner: str, repo: str, commit_sha: str, symbol_index: dict, dependency_graph: dict, changed_files: list):
    """
    Builds or incrementally syncs local Qdrant vectors for changed files and direct dependencies.
    """
    collection_name = f"{owner}_{repo}".lower().replace("-", "_").replace(".", "_")
    
    if not ensure_qdrant_collection(collection_name):
        print(f"[SemanticIndex] Failed to establish Qdrant collection for '{collection_name}'. Skipping.")
        return

    # Filter indexing targets to only include: changed files + files that import/are imported by them
    files_to_index = set(changed_files)
    for f in changed_files:
        if f in dependency_graph:
            files_to_index.update(dependency_graph[f].get("imports", []))
            files_to_index.update(dependency_graph[f].get("imported_by", []))

    filtered_symbols = {
        qual_name: meta for qual_name, meta in symbol_index.items()
        if meta["file_path"] in files_to_index
    }

    print(f"[SemanticIndex] Syncing Qdrant index: '{collection_name}' ({len(filtered_symbols)} active symbols across {len(files_to_index)} files)...")

    # Fetch existing mappings
    existing_vectors = scroll_active_points(collection_name)
    active_uuids = set()
    updates = []
    
    from app.llm_client import _context
    workspace_path = _context.get("workspace", "")

    for qual_name, meta in filtered_symbols.items():
        symbol_uuid = get_symbol_uuid(collection_name, qual_name)
        active_uuids.add(symbol_uuid)

        # Pre-resolve to absolute paths so we read directly from local SSD cache instead of API
        abs_filepath = os.path.join(workspace_path, meta["file_path"])
        content = get_symbol_signature_or_content(abs_filepath, meta["line_range"], fallback_only=False)
        if not content.strip():
            continue

        code_hash = hashlib.sha256(content.encode("utf-8")).hexdigest()
        
        # Incremental check
        if symbol_uuid in existing_vectors and existing_vectors[symbol_uuid]["hash"] == code_hash and existing_vectors[symbol_uuid].get("symbol") is not None:
            continue

        updates.append({
            "uuid": symbol_uuid,
            "qual_name": qual_name,
            "meta": meta,
            "content": content,
            "hash": code_hash
        })

    # Embed and upsert modified elements
    if updates:
        print(f"[SemanticIndex] Embedding {len(updates)} updated symbols in parallel threads...")
        
        def process_update(u):
            context_text = f"Path: {u['meta']['file_path']}\nSymbol: {u['qual_name']}\nType: {u['meta'].get('type', 'symbol')}\nCode:\n{u['content']}"
            vector = get_ollama_embedding(context_text, task="document")
            if vector:
                return {
                    "id": u["uuid"],
                    "vector": vector,
                    "payload": {
                        "symbol": u["qual_name"],
                        "kind": u["meta"].get("type"),
                        "path": u["meta"]["file_path"],
                        "start_line": u["meta"]["line_range"][0],
                        "end_line": u["meta"]["line_range"][1],
                        "hash": u["hash"]
                    }
                }
            return None

        points_to_upsert = []
        with ThreadPoolExecutor(max_workers=5) as executor:
            results = executor.map(process_update, updates)
            for res in results:
                if res:
                    points_to_upsert.append(res)

        if points_to_upsert:
            qdrant_host = get_config().get("QDRANT_HOST", "http://localhost:6333")
            upsert_url = f"{qdrant_host}/collections/{collection_name}/points"
            batch_size = 500
            total_batches = (len(points_to_upsert) + batch_size - 1) // batch_size
            for i in range(0, len(points_to_upsert), batch_size):
                batch = points_to_upsert[i:i + batch_size]
                try:
                    r = requests.put(upsert_url, json={"points": batch}, timeout=15)
                    if r.status_code == 200:
                        print(f"[SemanticIndex] Successfully upserted batch {i // batch_size + 1}/{total_batches} ({len(batch)} points).")
                    else:
                        print(f"[SemanticIndex] Error: Failed to upsert batch {i // batch_size + 1}/{total_batches}. Status code {r.status_code}: {r.text}")
                except Exception as e:
                    print(f"[SemanticIndex] Failed to upload batch {i // batch_size + 1}/{total_batches}: {e}")

    # Prune ONLY stale symbols belonging to the active files we evaluated
    stale_uuids = [
        uid for uid, info in existing_vectors.items()
        if info.get("path") in files_to_index and uid not in active_uuids
    ]
    if stale_uuids:
        print(f"[SemanticIndex] Pruning {len(stale_uuids)} stale points from Vector DB...")
        qdrant_host = get_config().get("QDRANT_HOST", "http://localhost:6333")
        delete_url = f"{qdrant_host}/collections/{collection_name}/points/delete"
        try:
            requests.post(delete_url, json={"points": stale_uuids}, timeout=10)
        except Exception as e:
            print(f"[SemanticIndex] Stale point deletion failed: {e}")

def search_semantic(query_text: str, collection_name: str, limit: int = 3) -> list:
    """Queries Qdrant for closely matched code blocks using nomic-embed-text."""
    if is_debug_mode():
        print(f"[DEBUG] [SemanticIndex] Querying semantic search for collection: '{collection_name}' (limit: {limit}). Query prefix: '{query_text[:60].strip()}...'")
    query_vector = get_ollama_embedding(query_text, task="query")
    if not query_vector:
        if is_debug_mode():
            print(f"[DEBUG] [SemanticIndex] Query embedding generation failed or returned empty.")
        return []

    config = get_config()
    score_threshold = config.get("SEMANTIC_SCORE_THRESHOLD", 0.30)
    qdrant_host = config.get("QDRANT_HOST", "http://localhost:6333")
    url = f"{qdrant_host}/collections/{collection_name}/points/search"
    payload = {
        "vector": query_vector,
        "limit": limit,
        "with_payload": True
    }
    try:
        r = requests.post(url, json=payload, timeout=10)
        if r.status_code == 200:
            results = r.json().get("result", [])
            if is_debug_mode():
                print(f"[DEBUG] [SemanticIndex] Qdrant search returned {len(results)} matches.")
                for idx, res in enumerate(results):
                    print(f"[DEBUG] [SemanticIndex] Match #{idx}: Symbol='{res.get('payload', {}).get('symbol')}', Score={res.get('score', 0.0):.4f}, Payload={res.get('payload')}")
            filtered_results = [res for res in results if res.get("score", 0.0) >= score_threshold]
            if is_debug_mode():
                print(f"[DEBUG] [SemanticIndex] Filtered down to {len(filtered_results)} matches above threshold {score_threshold}.")
            return filtered_results
        else:
            print(f"[SemanticIndex] Warning: Qdrant search returned status code {r.status_code}: {r.text}")
    except Exception as e:
        if is_debug_mode():
            print(f"[DEBUG] [SemanticIndex] Search request failed: {e}")
    return []