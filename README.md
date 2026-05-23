# codebase2context

Created by Eduardo J. Barrios (GitHub: efujbarrios)

`codebase2context` is a production-quality, offline, single-file Python developer tool that scans a repository and generates a highly optimized structured Markdown context file for LLM agents (Claude, Codex, GPT, Gemini, etc.).

It is designed to feel like:

“Export this repository into an LLM-ready architecture context.”

## What it generates

Running the tool produces a deterministic Markdown file:

- `CODEBASE_CONTEXT.md` (default output name)

The generated Markdown is structured as a ready-to-use universal prompt context for AI agents, and it:

- Begins with exactly: `Given this context:`
- Ends with exactly: `I have the following question:`

## Requirements

- Python 3.10+
- No external dependencies (standard library only)
- Works fully offline (no APIs)

## Using codebase2context in Any Repository

The script is intentionally portable: you can copy the single `codebase2context.py` file into any repository and run it immediately.

### Option 1 — Drag & Drop

1. Copy or drag `codebase2context.py` into any repository folder.
2. Open a terminal in that folder.
3. Run:

   ```bash
   python codebase2context.py
   ```

4. The tool automatically generates:

   - `CODEBASE_CONTEXT.md`

### Option 2 — Analyze Another Repository

You can analyze a different repository by passing its path:

```bash
python codebase2context.py /path/to/project
```

Example:

```bash
python codebase2context.py ~/projects/my-app
```

The tool will:

- Scan the target repository
- Generate the Markdown file inside that repository
- Infer architecture automatically
- Work regardless of framework/language (best-effort heuristics)

You can also customize the output filename:

```bash
python codebase2context.py . --output ARCHITECTURE_CONTEXT.md
```

This allows engineers to:

- Onboard new AI agents quickly
- Reuse architecture context
- Share codebase understanding
- Standardize LLM context engineering
- Avoid pasting entire repositories into chats
- Reduce token costs dramatically

## CLI options

```bash
python codebase2context.py --help
```

Common knobs:

- `--max-files 1000` — analyze more files
- `--max-depth 5` — show a deeper repository tree
- `--max-summary-chars 1200` — cap per-file summaries
- `--max-functions 30` / `--max-classes 20` — cap extracted signatures per file

## How it works (high level)

- Recursively scans the repository with a deterministic walk order
- Ignores common junk directories and binary/generated/minified files
- Detects languages/frameworks via file extensions, dependency files, and import heuristics
- Extracts a compact “API/architecture surface” (entrypoints, routes, models, exports)
- Ranks important files and emits structured Markdown optimized for LLM ingestion

## Example output (excerpt)

```md
Given this context:

1. Project Overview
- Purpose (inferred): ...
- Application type (inferred): ...
...

15. Optimized Agent Context
- App type: ...
...

I have the following question:
```

## Included example codebase

This repository includes an example “toy repo” at `exmaple_codebase/` plus a pre-generated sample output:

- `exmaple_codebase/CODEBASE_CONTEXT.md`

To regenerate it (demonstrating the “copy the file into a repo and run it” workflow):

```bash
cd exmaple_codebase
python codebase2context.py
```

## What it detects (heuristics)

- Languages: Python, JavaScript, TypeScript, Go, Rust, Java, C#, PHP
- Frameworks: inferred from dependencies and imports (e.g. FastAPI, Flask, Django, Express, React, Next.js, etc.)
- Entry points: `main.py`, `app.py`, `server.js`, `index.ts`, Docker entrypoints, etc.
- Important files: ranked by architecture signals (entrypoints, routes, models, central imports, config)
- Token optimization: summarizes and extracts signatures instead of dumping full files

## Ignored junk by default

The tool automatically ignores common noise:

- `.git`, `node_modules`, `dist`, `build`, `coverage`, `.next`, `.nuxt`, `.cache`, `venv`, `.venv`, `__pycache__`, `target`, `.idea`, `.vscode`, etc.
- Binary files and large generated assets
- Minified bundles (e.g. `*.min.js`)
- Lockfiles where appropriate
- `.env` / `.env.*` (to avoid leaking secrets). Prefer `.env.example`.
