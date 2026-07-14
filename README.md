# Code Review Agent

An automated AI code reviewer for GitHub pull requests. It listens for PR events (via webhook or CLI), pulls the diff, builds a structural understanding of the repository (symbol index + dependency graph), routes changed code to LLM "reviewer" subagents, and posts the findings back as inline PR review comments.

The project is developed across three branches, each representing a different LLM backend strategy:

| Branch | LLM Backend | Best for |
|---|---|---|
| `main` | Google Gemini API | Fast, cheap, no local hardware needed |
| `local-llm` | Local model via Ollama + Qdrant semantic search | Better accuracy, fully private, needs local compute |
| `deepseek` | DeepSeek-R1 (via Ollama) with native "thinking" mode + Qdrant semantic search | Highest accuracy among local options, slowest |

---

## How it works

1. **Trigger** — a GitHub webhook (`pull_request` event) or a manual CLI call starts a review for a given `owner/repo` + PR number.
2. **Fetch** — the PR's metadata, files, and patches are pulled from the GitHub API; the head and base commits are checked out into a local workspace cache.
3. **Triage** — docs-only PRs are skipped, and changed files run through native compilers/linters as a fast sanity check before spending any LLM calls.
4. **Repo intelligence** — an AST-based symbol index and dependency graph are built (and cached in a local SQLite DB) so the reviewer can see how a changed function relates to the rest of the codebase.
5. **Chunking** — diffs are split into review-sized chunks per file/symbol, bundled with relevant dependency context.
6. **Review** — each chunk/symbol is sent to an LLM subagent (Gemini or a local model) which returns structured findings (`CodeComment` objects validated against a Pydantic schema).
7. **Validation & posting** — findings are deduplicated, ranked by severity (P0–P3), and posted back to the PR as a batched inline GitHub review.

A `RateGatekeeper` throttles requests/tokens per minute across all threads, and a `PipelineProfiler` reports timing for every stage at the end of a run.

## Repository structure

```
app/
├── main.py              # CLI entrypoint + webhook server (HTTP listener for GitHub events)
├── config.py             # Secret/config storage (~/.config/code_review_agent/config.json) + CLI for setting keys
├── llm_client.py         # LLM calls (Gemini and/or local backend), retries, token telemetry, get_lines tool
├── diff_parser.py        # Parses unified diffs/patches, file filtering, language detection
├── chunker.py            # Splits file diffs into review-sized chunks, pulls symbol context
├── prompt_builder.py     # Builds system instructions/prompts for reviewer subagents
├── orchestrator.py       # Coordinates chunk/symbol review calls, escalation rounds, shared context cache
├── repo_intelligence.py  # Builds/loads the AST symbol index + dependency graph (SQLite-backed)
├── repo_checks.py        # Fetches repo conventions (linters, style config, etc.)
├── triage.py              # Docs-only skip logic, native compile/lint checks
├── gatekeeper.py          # Central RPM/TPM/RPD rate limiter shared across threads
├── profiler.py            # Pipeline timing/telemetry reporter
├── output_parser.py       # Maps LLM findings to GitHub review comment payloads
├── validator.py           # Deduplicates and severity-filters comments before posting
├── models.py              # Pydantic schemas (CodeComment, CodeReviewResponse, etc.)
└── semantic_index.py       # (local-llm & deepseek branches only) Qdrant embedding sync + semantic search
```

All three branches share the same architecture and file layout; `local-llm` and `deepseek` add `semantic_index.py` and extend `config.py`, `llm_client.py`, `orchestrator.py`, and `prompt_builder.py` to support a local model backend.

---

## Installation

### 1. Clone and install dependencies

```bash
git checkout <main | local-llm | deepseek>
python -m venv venv
source venv/bin/activate        # Windows: venv\Scripts\activate
pip install -r requirements.txt
```

### 2. Configure your GitHub PAT

The agent needs a GitHub Personal Access Token (repo scope) to read PRs and post review comments. It's stored securely (mode `0600`) in `~/.config/code_review_agent/config.json`, and can also be supplied via the `GITHUB_PAT` environment variable (env var takes priority).

```bash
python -m app.config --set-pat
```

You'll be prompted to paste the token (input hidden). To register a token scoped to a specific repo instead of the general one:

```bash
python -m app.config --set-repo-pat owner/repo-name
```

### 3. Configure the LLM backend

**`main` branch (Gemini only):**

```bash
python -m app.config --set-gemini
```

You'll be prompted to paste your `GEMINI_API_KEY` (input hidden). Alternatively set it as an environment variable:

```bash
export GEMINI_API_KEY="your-key-here"     # Windows: set GEMINI_API_KEY=your-key-here
```

**`local-llm` / `deepseek` branches (Ollama + Qdrant):**

These branches can still fall back to Gemini, so you may set `GEMINI_API_KEY` as above. To use a local model instead:

1. Install and run [Ollama](https://ollama.com), then pull a model:
   ```bash
   ollama pull qwen2.5-coder:14b     # local-llm branch default
   ollama pull deepseek-r1:8b        # deepseek branch default
   ollama pull nomic-embed-text      # embedding model used for semantic search on both branches
   ```
2. Run [Qdrant](https://qdrant.tech) locally (used for semantic code search), e.g. via Docker:
   ```bash
   docker run -p 6333:6333 qdrant/qdrant
   ```
3. Switch the backend in your config file (`~/.config/code_review_agent/config.json`) or via environment variable:
   ```bash
   export LLM_BACKEND=ollama
   export LLM_MODEL=qwen2.5-coder:14b   # or deepseek-r1:8b on the deepseek branch
   ```
   `OLLAMA_HOST` (default `http://localhost:11434`) and `QDRANT_HOST` (default `http://localhost:6333`) can also be overridden if you're running either service on a different host/port.

### 4. Run

Build the repo intelligence index for a commit (optional — it's built automatically on first run if missing):

```bash
python -m app.main --build-index --commit <sha>
```

Review a single PR directly:

```bash
python -m app.main --repo owner/repo --pr 123
```

Or run as a webhook server that GitHub can call on PR events:

```bash
python -m app.main --server --port 8000
```

Or review a local `.diff`/`.patch` file offline (no GitHub API calls, no repo intelligence):

```bash
python -m app.main --diff-file path/to/change.diff
```

Useful flags: `--dep-hops` (how many dependency-graph hops of context to include, default 1), `--max-context-calls` (extra escalation rounds a subagent may request, default 1), `--debug` (verbose pipeline trace logging).

---

## Limitations

### `main` branch — Gemini API
- Fastest of the three since it's a single hosted API call per chunk with no local inference bottleneck.
- Accuracy is capped by how much context can be sent per request — GitHub/API rate and token limits (RPM/TPM/RPD, enforced by the `RateGatekeeper`) mean only a limited dependency "hop" radius and a limited number of escalation rounds can be included per chunk. The model often has to review a change without full visibility into every caller/callee, which lowers accuracy on changes with wide blast radius.
- Requires a paid or free-tier Gemini API key and outbound internet access.

### `local-llm` branch — Ollama + Qdrant
- More accurate than the `main` branch on average: in addition to dependency-graph context, it uses semantic search (via Qdrant + `nomic-embed-text` embeddings) to pull in related code by *meaning*, not just direct references — so it can surface relevant context the static dependency graph would miss.
- Noticeably slower than the API branch, especially on CPU-only machines; reviews are also run single-threaded when the backend is `ollama` (vs. up to 4 parallel workers for Gemini) to avoid overloading local inference.
- Quality is bounded by the local model's size/capability. The default `qwen2.5-coder:14b` needs a reasonably capable machine (ideally GPU-enabled) to run at usable speed and context length; on constrained hardware (e.g. 4 vCPU / 8 GB) the context window has to be kept small, which reintroduces some of the same context-limitation problem as the API branch.
- Results improve with a bigger/better local model and a GPU-enabled environment, at the cost of more memory/VRAM and slower iteration.

### `deepseek` branch — DeepSeek-R1 (Ollama) + Qdrant
- Same local-first architecture as `local-llm`, tuned specifically for `deepseek-r1:8b`'s native "thinking" mode, with a larger context ceiling and output budget than the `local-llm` branch defaults.
- The reasoning ("thinking") pass before the final structured answer tends to catch more subtle issues, but adds latency on top of what's already the slowest of the three branches — this is the most accurate but least real-time-friendly option.
- Like `local-llm`, output quality and speed scale with the hardware it's run on; a GPU is strongly recommended, and swapping in a larger DeepSeek checkpoint (if your hardware supports it) generally improves results further.
- Requires the same Ollama + Qdrant setup as `local-llm`, so it shares its dependency footprint and setup overhead.

**Across all three:** review quality also depends on how good the repo intelligence index is (AST parsing coverage per language, dependency graph accuracy) and on the `TRIAGE_SKIP_PATTERNS` / triage checks, which intentionally skip some file types and only run a shallow native compile/lint sanity check rather than full static analysis.
