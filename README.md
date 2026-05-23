# codebase2context

Export any repository into an LLM-ready Markdown context file — offline, deterministic, and single-file.

[![Python](https://img.shields.io/badge/python-3.10%2B-blue)](https://www.python.org/)
[![Dependencies](https://img.shields.io/badge/deps-stdlib--only-success)](#requirements)
[![Offline](https://img.shields.io/badge/offline-yes-success)](#privacy--safety)

`codebase2context` scans a repository and generates a structured Markdown file you can paste into an LLM (Codex, GPT, Claude, Gemini, etc.) to bootstrap architecture understanding without dumping the whole codebase.

It’s designed to feel like: “Export this repo into an LLM-ready architecture + API surface context.”

## Quickstart

1. Copy `codebase2context.py` into the repo you want to analyze.
2. Run:

```bash
python codebase2context.py
```

This writes `CODEBASE_CONTEXT.md` into that repository.

### Analyze a different repository

```bash
python /path/to/codebase2context.py /path/to/other/repo
```

### Customize the output file

```bash
python codebase2context.py . --output ARCHITECTURE_CONTEXT.md
```

## Requirements

- Python 3.10+
- Standard library only (no dependencies)
- Works fully offline (no network calls)

## What it generates

By default the tool writes `CODEBASE_CONTEXT.md`. The output is intentionally deterministic and has strict markers so it can be reused as a “universal context wrapper”:

- First line is exactly: `Given this context:`
- Last line is exactly: `I have the following question:`

### Using the generated context with an LLM

1. Open `CODEBASE_CONTEXT.md`.
2. Paste its contents into your LLM chat.
3. After the final line (`I have the following question:`), type your actual question.

### Output preview

```md
Given this context:

1. Project Overview
- Purpose (inferred): ...
- Application type (inferred): ...

5. Important Files
- `src/...` ...

15. Optimized Agent Context
- App type: ...

I have the following question:
```

## How it works (high level)

- Deterministically walks the repository (stable ordering)
- Skips common junk, binaries, generated/minified assets, and very large files
- Detects languages/frameworks via extensions + dependency/config heuristics
- Extracts a compact “architecture surface” (entrypoints, routes, models, exports, signatures)
- Ranks “important” files with simple architecture signals (entrypoints, routing, configs, central imports)

## CLI

```bash
python codebase2context.py --help
```

Common knobs:

- `--max-files` — cap how many files are analyzed (default is conservative)
- `--max-depth` — cap repository tree depth in the output
- `--max-summary-chars` — cap per-file summary size
- `--max-functions` / `--max-classes` — cap extracted signatures per file

## Detection (heuristics)

- Languages: Python, JavaScript, TypeScript, Go, Rust, Java, C#, PHP
- Frameworks: inferred from dependencies/imports (e.g. FastAPI, Flask, Django, Express, React, Next.js, etc.)
- Entrypoints: `main.py`, `app.py`, `server.js`, `index.ts`, Docker entrypoints, etc.
- Token optimization: prefers summaries + signatures over full-file dumps

## Ignore rules

The tool intentionally ignores common noise such as:

- `.git`, `node_modules`, `dist`, `build`, `coverage`, `.next`, `.nuxt`, `.cache`, `venv`, `.venv`, `__pycache__`, `target`, `.idea`, `.vscode`
- Lockfiles (e.g. `package-lock.json`, `poetry.lock`, `Cargo.lock`, `go.sum`, etc.)
- Minified bundles (e.g. `*.min.js`, `*.min.css`, `*.bundle.js`)
- Many binary / archive formats (`.png`, `.pdf`, `.zip`, `.exe`, etc.)
- Secrets by default: `.env` and `.env.*` (except `.env.example`)

It also skips very large files (currently > ~2MB) to keep output compact and avoid wasting tokens.

## Privacy & safety

- The script makes no network requests and does not call any APIs.
- Treat the generated context file as sensitive: it may include filenames, summaries, and extracted signatures from your codebase.

## Contributing

Issues and PRs are welcome. If you report a bug, include:

- OS + Python version
- A minimal repo layout that reproduces the behavior (or anonymized file names)
- The command you ran (including flags)

## Author

Created by Eduardo J. Barrios (GitHub: `@edujbarrios`).
