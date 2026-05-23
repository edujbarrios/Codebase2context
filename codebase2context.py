#!/usr/bin/env python3
"""
codebase2context — export any repository into an LLM-ready Markdown context.

Created by Eduardo J. Barrios (GitHub: efujbarrios)

This is a single-file, zero-setup, offline tool (Python 3.10+, standard library only).

Usage:
  python codebase2context.py
  python codebase2context.py /path/to/repo
  python codebase2context.py . --output ARCHITECTURE_CONTEXT.md
  python codebase2context.py --max-files 1000 --max-depth 5
"""

from __future__ import annotations

import argparse
import ast
import hashlib
import os
import re
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import DefaultDict, Iterable, Iterator, Optional


# -----------------------------
# Configurable limits (tokens)
# -----------------------------
MAX_FILE_SUMMARY_CHARS = 1200
MAX_TREE_DEPTH = 4
MAX_FILES_ANALYZED = 500
MAX_FUNCTIONS_PER_FILE = 30
MAX_CLASSES_PER_FILE = 20


# -----------------------------
# Ignore rules
# -----------------------------
IGNORED_DIRECTORIES = {
    ".git",
    "node_modules",
    "dist",
    "build",
    "coverage",
    ".next",
    ".nuxt",
    ".cache",
    "venv",
    ".venv",
    "env",
    "__pycache__",
    "target",
    "bin",
    "obj",
    ".idea",
    ".vscode",
}

IGNORED_FILE_NAMES = {
    # lockfiles / vendor-ish
    "package-lock.json",
    "yarn.lock",
    "pnpm-lock.yaml",
    "npm-shrinkwrap.json",
    "poetry.lock",
    "Pipfile.lock",
    "Cargo.lock",
    "go.sum",
}

IGNORED_FILE_SUFFIXES = {
    ".png",
    ".jpg",
    ".jpeg",
    ".gif",
    ".webp",
    ".ico",
    ".pdf",
    ".zip",
    ".tar",
    ".gz",
    ".7z",
    ".rar",
    ".exe",
    ".dll",
    ".so",
    ".dylib",
    ".class",
    ".jar",
    ".war",
    ".o",
    ".a",
    ".obj",
    ".pyc",
    ".pyo",
    ".pyd",
    ".wasm",
    ".mp4",
    ".mov",
    ".avi",
    ".mp3",
    ".wav",
    ".flac",
}


LANG_BY_SUFFIX: dict[str, str] = {
    ".py": "Python",
    ".js": "JavaScript",
    ".jsx": "JavaScript",
    ".ts": "TypeScript",
    ".tsx": "TypeScript",
    ".go": "Go",
    ".rs": "Rust",
    ".java": "Java",
    ".cs": "C#",
    ".php": "PHP",
}


def _is_probably_binary(path: Path) -> bool:
    try:
        with path.open("rb") as f:
            chunk = f.read(4096)
    except OSError:
        return True
    if b"\x00" in chunk:
        return True
    # Heuristic: very low printable ratio -> likely binary
    text_chars = b"\n\r\t\b" + bytes(range(32, 127))
    if not chunk:
        return False
    printable = sum(1 for b in chunk if b in text_chars)
    return printable / max(1, len(chunk)) < 0.70


def _looks_minified_text(text: str) -> bool:
    # Single very long line + low newline density => likely minified
    if not text:
        return False
    if len(text) < 2000:
        return False
    newlines = text.count("\n")
    if newlines <= 1 and any(len(line) > 5000 for line in text.splitlines()[:3]):
        return True
    if newlines > 0 and (len(text) / max(1, newlines)) > 2000:
        return True
    return False


def _read_text_safely(path: Path, max_bytes: int = 200_000) -> str:
    try:
        with path.open("rb") as f:
            raw = f.read(max_bytes)
    except OSError:
        return ""
    try:
        return raw.decode("utf-8", errors="replace")
    except Exception:
        return ""


def _stable_hash(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8", errors="ignore")).hexdigest()[:12]


def _relpath(path: Path, root: Path) -> str:
    try:
        return str(path.relative_to(root)).replace(os.sep, "/")
    except ValueError:
        return str(path).replace(os.sep, "/")


@dataclass(frozen=True)
class Limits:
    max_file_summary_chars: int = MAX_FILE_SUMMARY_CHARS
    max_tree_depth: int = MAX_TREE_DEPTH
    max_files_analyzed: int = MAX_FILES_ANALYZED
    max_functions_per_file: int = MAX_FUNCTIONS_PER_FILE
    max_classes_per_file: int = MAX_CLASSES_PER_FILE


@dataclass
class FileFacts:
    path: str
    language: str
    size_bytes: int
    is_test: bool = False
    is_config: bool = False
    is_entrypoint: bool = False
    exports: list[str] = field(default_factory=list)
    imports: list[str] = field(default_factory=list)
    functions: list[str] = field(default_factory=list)
    classes: list[str] = field(default_factory=list)
    routes: list[str] = field(default_factory=list)
    models: list[str] = field(default_factory=list)
    todo_markers: int = 0
    summary: str = ""
    responsibilities: list[str] = field(default_factory=list)
    score: float = 0.0


@dataclass
class RepoFacts:
    root: Path
    files: list[FileFacts]
    skipped_files: int
    languages: Counter[str]
    frameworks: list[str]
    package_managers: list[str]
    configs: dict[str, str]
    dependencies: dict[str, str]
    entrypoints: list[FileFacts]
    api_surface: list[str]
    data_models: list[str]
    test_facts: list[str]
    build_run_instructions: list[str]
    dev_notes: list[str]
    architecture_notes: list[str]


def _should_ignore_dir(dirname: str) -> bool:
    return dirname in IGNORED_DIRECTORIES


def _should_ignore_file(path: Path) -> bool:
    name = path.name
    if name in IGNORED_FILE_NAMES:
        return True
    suf = path.suffix.lower()
    if suf in IGNORED_FILE_SUFFIXES:
        return True
    # Ignore obvious generated/minified bundles by name.
    lowered = name.lower()
    if lowered.endswith(".min.js") or lowered.endswith(".min.css"):
        return True
    if lowered.endswith(".bundle.js") or lowered.endswith(".bundle.css"):
        return True
    return False


def _iter_repo_files(root: Path) -> Iterator[Path]:
    # Deterministic walk (sorted) while skipping ignored dirs.
    stack: list[Path] = [root]
    while stack:
        current = stack.pop()
        try:
            entries = list(current.iterdir())
        except OSError:
            continue
        dirs: list[Path] = []
        files: list[Path] = []
        for e in entries:
            if e.is_dir():
                if _should_ignore_dir(e.name):
                    continue
                dirs.append(e)
            elif e.is_file():
                files.append(e)
        for f in sorted(files, key=lambda p: p.name):
            yield f
        for d in sorted(dirs, key=lambda p: p.name, reverse=True):
            stack.append(d)


def _detect_language(path: Path) -> str:
    suf = path.suffix.lower()
    if suf in LANG_BY_SUFFIX:
        return LANG_BY_SUFFIX[suf]
    return "Other"


def _is_test_path(rel: str) -> bool:
    parts = rel.lower().split("/")
    if any(p in {"test", "tests", "__tests__"} for p in parts):
        return True
    base = parts[-1]
    return base.startswith("test_") or base.endswith("_test.py") or base.endswith(".test.ts") or base.endswith(".spec.ts")


def _is_entrypoint_name(name: str) -> bool:
    lowered = name.lower()
    return lowered in {
        "main.py",
        "app.py",
        "wsgi.py",
        "asgi.py",
        "manage.py",
        "server.js",
        "server.ts",
        "index.js",
        "index.ts",
        "cli.py",
        "__main__.py",
        "dockerfile",
        "docker-compose.yml",
        "docker-compose.yaml",
    }


def _is_config_name(name: str) -> bool:
    lowered = name.lower()
    return lowered in {
        "pyproject.toml",
        "requirements.txt",
        "setup.cfg",
        "setup.py",
        "tox.ini",
        "package.json",
        "tsconfig.json",
        "webpack.config.js",
        "vite.config.ts",
        "vite.config.js",
        "go.mod",
        "cargo.toml",
        "makefile",
        ".env.example",
        ".env",
        ".github/workflows",
        "dockerfile",
        "docker-compose.yml",
        "docker-compose.yaml",
    }


def scan_repository(root: Path, limits: Limits) -> tuple[list[Path], int]:
    candidates: list[Path] = []
    skipped = 0
    for path in _iter_repo_files(root):
        rel = _relpath(path, root)
        if _should_ignore_file(path):
            skipped += 1
            continue
        if _is_probably_binary(path):
            skipped += 1
            continue
        lang = _detect_language(path)
        if lang == "Other" and path.name not in {"README.md", "LICENSE"} and not _is_config_name(path.name):
            # Keep "Other" only for important config/docs.
            skipped += 1
            continue
        # Skip very large files early (still count as skipped).
        try:
            size = path.stat().st_size
        except OSError:
            skipped += 1
            continue
        if size > 2_000_000:
            skipped += 1
            continue
        candidates.append(path)
    candidates = sorted(candidates, key=lambda p: _relpath(p, root))
    if len(candidates) > limits.max_files_analyzed:
        candidates = candidates[: limits.max_files_analyzed]
    return candidates, skipped


def _tree_lines(root: Path, files: Iterable[Path], max_depth: int) -> list[str]:
    # Build a compact tree containing only relevant directories.
    included_dirs: set[Path] = set()
    included_files: set[Path] = set()
    for p in files:
        rel = p.relative_to(root)
        included_files.add(rel)
        parent = rel.parent
        while str(parent) != ".":
            included_dirs.add(parent)
            parent = parent.parent

    def iter_children(dir_rel: Path) -> tuple[list[Path], list[Path]]:
        dirs: set[Path] = set()
        files_: set[Path] = set()
        for d in included_dirs:
            if d.parent == dir_rel:
                dirs.add(d)
        for f in included_files:
            if f.parent == dir_rel:
                files_.add(f)
        return sorted(dirs, key=lambda p: str(p)), sorted(files_, key=lambda p: str(p))

    lines: list[str] = []

    def walk(dir_rel: Path, depth: int) -> None:
        if depth > max_depth:
            return
        dirs, files_ = iter_children(dir_rel)
        indent = "  " * depth
        for d in dirs:
            name = f"{d.name}/"
            lines.append(f"{indent}{name}")
            walk(d, depth + 1)
        for f in files_:
            lines.append(f"{indent}{f.name}")

    walk(Path("."), 0)
    return lines[:500]


def _truncate(s: str, max_chars: int) -> str:
    if len(s) <= max_chars:
        return s
    return s[: max_chars - 1].rstrip() + "…"


def _count_todo_markers(text: str) -> int:
    return len(re.findall(r"\b(TODO|FIXME|HACK)\b", text))


def analyze_file(root: Path, path: Path, limits: Limits) -> FileFacts:
    rel = _relpath(path, root)
    lang = _detect_language(path)
    try:
        size = path.stat().st_size
    except OSError:
        size = 0

    text = _read_text_safely(path)
    if lang in {"JavaScript", "TypeScript"} and _looks_minified_text(text):
        # Treat minified assets as ignored.
        return FileFacts(path=rel, language=lang, size_bytes=size, summary="(minified asset skipped)", score=-1.0)

    facts = FileFacts(
        path=rel,
        language=lang,
        size_bytes=size,
        is_test=_is_test_path(rel),
        is_config=_is_config_name(path.name),
        is_entrypoint=_is_entrypoint_name(path.name),
        todo_markers=_count_todo_markers(text),
    )

    if lang == "Python":
        _analyze_python(text, facts, limits)
    elif lang in {"JavaScript", "TypeScript"}:
        _analyze_js_ts(text, facts, limits)
    elif lang == "Go":
        _analyze_go(text, facts, limits)
    elif lang == "Rust":
        _analyze_rust(text, facts, limits)
    elif lang == "Java":
        _analyze_java(text, facts, limits)
    elif lang == "C#":
        _analyze_csharp(text, facts, limits)
    elif lang == "PHP":
        _analyze_php(text, facts, limits)
    else:
        _analyze_other(text, facts, limits)

    facts.summary = _truncate(facts.summary.strip() or _default_file_summary(facts), limits.max_file_summary_chars)
    facts.responsibilities = _infer_responsibilities(facts)
    facts.score = score_file(facts)
    return facts


def _default_file_summary(f: FileFacts) -> str:
    parts: list[str] = []
    if f.is_entrypoint:
        parts.append("entrypoint")
    if f.is_config:
        parts.append("configuration")
    if f.is_test:
        parts.append("tests")
    if f.routes:
        parts.append("API routes")
    if f.models:
        parts.append("data models")
    if f.classes:
        parts.append(f"{len(f.classes)} classes")
    if f.functions:
        parts.append(f"{len(f.functions)} functions")
    if not parts:
        return "source file"
    return ", ".join(parts)


def _infer_responsibilities(f: FileFacts) -> list[str]:
    rel = f.path.lower()
    resp: list[str] = []
    if f.is_entrypoint:
        resp.append("startup / orchestration")
    if f.is_config:
        resp.append("configuration / tooling")
    if any(k in rel for k in ("/route", "/router", "/routes", "/controller", "/handlers")) or f.routes:
        resp.append("request routing / endpoints")
    if any(k in rel for k in ("/service", "/usecase", "/interactor")):
        resp.append("business logic / services")
    if any(k in rel for k in ("/model", "/models", "/schema", "/entity", "/entities")) or f.models:
        resp.append("data models / schemas")
    if any(k in rel for k in ("/db", "/database", "/repo", "/repository", "/dao", "/storage", "/migrat")):
        resp.append("persistence / data access")
    if any(k in rel for k in ("/infra", "/deploy", "/docker", "/k8", "/terraform", "/ci", ".github/")):
        resp.append("infrastructure / automation")
    if f.is_test:
        resp.append("testing")
    # Deduplicate while preserving order
    seen: set[str] = set()
    out: list[str] = []
    for r in resp:
        if r not in seen:
            seen.add(r)
            out.append(r)
    return out[:6]


def score_file(f: FileFacts) -> float:
    score = 0.0
    p = f.path.lower()
    if f.is_entrypoint:
        score += 8.0
    if f.is_config:
        score += 5.0
    if f.routes:
        score += 6.0
    if f.models:
        score += 4.0
    if any(k in p for k in ("/src/", "/app/", "/apps/", "/cmd/", "/server", "/api/")):
        score += 2.0
    if any(k in p for k in ("/service", "/core", "/domain", "/lib", "/pkg")):
        score += 3.0
    if f.is_test:
        score -= 2.0
    score += min(2.0, f.todo_markers * 0.2)
    # Prefer moderate sized files (huge files tend to be noisy)
    if f.size_bytes > 250_000:
        score -= 2.0
    return score


def _safe_split_lines(text: str, max_lines: int = 4000) -> list[str]:
    lines = text.splitlines()
    if len(lines) <= max_lines:
        return lines
    return lines[:max_lines]


def _analyze_python(text: str, facts: FileFacts, limits: Limits) -> None:
    try:
        tree = ast.parse(text)
    except SyntaxError:
        facts.summary = "Python file (syntax error prevented AST parsing)"
        return

    imports: list[str] = []
    functions: list[str] = []
    classes: list[str] = []
    routes: list[str] = []
    models: list[str] = []

    for node in tree.body:
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    imports.append(alias.name)
            else:
                mod = node.module or ""
                for alias in node.names:
                    name = f"{mod}.{alias.name}".strip(".")
                    imports.append(name)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            functions.append(_py_function_sig(node))
            routes.extend(_py_routes_from_decorators(node))
        elif isinstance(node, ast.ClassDef):
            classes.append(_py_class_sig(node))
            models.extend(_py_models_from_class(node))
            # Also capture routes declared as methods with decorators.
            for item in node.body:
                if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    routes.extend(_py_routes_from_decorators(item))

    facts.imports = sorted(set(imports))[:200]
    facts.functions = functions[: limits.max_functions_per_file]
    facts.classes = classes[: limits.max_classes_per_file]
    facts.routes = sorted(set(routes))[:60]
    facts.models = sorted(set(models))[:60]

    # Exports: heuristically treat non-underscore top-level defs as public.
    exports: list[str] = []
    for sig in facts.functions:
        name = sig.split("(")[0].replace("async def ", "").replace("def ", "")
        if not name.startswith("_"):
            exports.append(name)
    for sig in facts.classes:
        name = sig.replace("class ", "").split("(")[0].strip(": ")
        if not name.startswith("_"):
            exports.append(name)
    facts.exports = sorted(set(exports))[:80]

    summary_bits: list[str] = []
    if facts.routes:
        summary_bits.append(f"{len(facts.routes)} route handlers")
    if facts.models:
        summary_bits.append(f"{len(facts.models)} model-like classes")
    if facts.classes:
        summary_bits.append(f"{len(facts.classes)} classes")
    if facts.functions:
        summary_bits.append(f"{len(facts.functions)} functions")
    if not summary_bits:
        summary_bits.append("Python module")
    facts.summary = ", ".join(summary_bits)


def _py_function_sig(node: ast.AST) -> str:
    if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
        return "def <unknown>(...)"
    args = []
    for a in node.args.posonlyargs:
        args.append(a.arg)
    if node.args.posonlyargs:
        args.append("/")
    for a in node.args.args:
        args.append(a.arg)
    if node.args.vararg:
        args.append(f"*{node.args.vararg.arg}")
    elif node.args.kwonlyargs:
        args.append("*")
    for a in node.args.kwonlyargs:
        args.append(a.arg)
    if node.args.kwarg:
        args.append(f"**{node.args.kwarg.arg}")
    prefix = "async def" if isinstance(node, ast.AsyncFunctionDef) else "def"
    return f"{prefix} {node.name}({', '.join(args)})"


def _py_class_sig(node: ast.ClassDef) -> str:
    bases: list[str] = []
    for b in node.bases[:3]:
        bases.append(_ast_name(b))
    base_s = f"({', '.join(bases)})" if bases else ""
    return f"class {node.name}{base_s}"


def _ast_name(node: ast.AST) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return f"{_ast_name(node.value)}.{node.attr}"
    if isinstance(node, ast.Subscript):
        return _ast_name(node.value)
    if isinstance(node, ast.Call):
        return _ast_name(node.func)
    return type(node).__name__


def _py_routes_from_decorators(fn: ast.AST) -> list[str]:
    if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
        return []
    routes: list[str] = []
    for d in fn.decorator_list:
        # FastAPI/Starlette: @app.get("/x"), @router.post("/y")
        if isinstance(d, ast.Call) and isinstance(d.func, ast.Attribute):
            method = d.func.attr.lower()
            if method in {"get", "post", "put", "patch", "delete", "options", "head"}:
                if d.args and isinstance(d.args[0], ast.Constant) and isinstance(d.args[0].value, str):
                    routes.append(f"{method.upper()} {d.args[0].value} -> {fn.name}")
        # Flask: @app.route("/x", methods=["GET"])
        if isinstance(d, ast.Call) and isinstance(d.func, ast.Attribute) and d.func.attr == "route":
            if d.args and isinstance(d.args[0], ast.Constant) and isinstance(d.args[0].value, str):
                path = d.args[0].value
                methods = None
                for kw in d.keywords:
                    if kw.arg == "methods" and isinstance(kw.value, (ast.List, ast.Tuple)):
                        vals: list[str] = []
                        for el in kw.value.elts:
                            if isinstance(el, ast.Constant) and isinstance(el.value, str):
                                vals.append(el.value.upper())
                        if vals:
                            methods = ",".join(vals[:4])
                if methods:
                    routes.append(f"{methods} {path} -> {fn.name}")
                else:
                    routes.append(f"ROUTE {path} -> {fn.name}")
    return routes


def _py_models_from_class(cls: ast.ClassDef) -> list[str]:
    bases = {_ast_name(b) for b in cls.bases}
    if any(b.endswith("BaseModel") or b.endswith("Model") for b in bases):
        return [cls.name]
    # SQLAlchemy declarative patterns
    for node in cls.body:
        if isinstance(node, ast.Assign):
            for t in node.targets:
                if isinstance(t, ast.Name) and t.id == "__tablename__":
                    return [cls.name]
    return []


_RE_JS_IMPORT = re.compile(r'^\s*import\s+.*?\s+from\s+[\'"]([^\'"]+)[\'"]', re.M)
_RE_JS_REQUIRE = re.compile(r'require\(\s*[\'"]([^\'"]+)[\'"]\s*\)')
_RE_JS_EXPORT_FN = re.compile(r'^\s*export\s+(async\s+)?function\s+([A-Za-z0-9_]+)\s*\(', re.M)
_RE_JS_EXPORT_CLASS = re.compile(r'^\s*export\s+class\s+([A-Za-z0-9_]+)\b', re.M)
_RE_EXPRESS_ROUTE = re.compile(
    r'\b(app|router)\.(get|post|put|patch|delete|options|head)\(\s*[\'"`]([^\'"`]+)[\'"`]',
    re.I,
)


def _analyze_js_ts(text: str, facts: FileFacts, limits: Limits) -> None:
    imports = set(_RE_JS_IMPORT.findall(text)) | set(_RE_JS_REQUIRE.findall(text))
    exports: list[str] = []
    exports.extend([m[1] for m in _RE_JS_EXPORT_FN.findall(text)])
    exports.extend(_RE_JS_EXPORT_CLASS.findall(text))
    routes = [f"{m[1].upper()} {m[2]}" for m in _RE_EXPRESS_ROUTE.findall(text)]
    routes = routes[:60]

    facts.imports = sorted(imports)[:200]
    facts.exports = sorted(set(exports))[:80]
    facts.routes = sorted(set(routes))[:60]
    facts.summary = ", ".join(
        [p for p in [f"{len(facts.exports)} exports" if facts.exports else "", f"{len(facts.routes)} routes" if facts.routes else ""] if p]
    ) or "JavaScript/TypeScript module"

    # Functions/classes (best-effort signatures)
    fns = re.findall(r"^\s*(async\s+)?function\s+([A-Za-z0-9_]+)\s*\(", text, flags=re.M)
    facts.functions = [f"function {name}()" for _, name in fns][: limits.max_functions_per_file]
    cls = re.findall(r"^\s*class\s+([A-Za-z0-9_]+)\b", text, flags=re.M)
    facts.classes = [f"class {name}" for name in cls][: limits.max_classes_per_file]


def _analyze_go(text: str, facts: FileFacts, limits: Limits) -> None:
    imports = set(re.findall(r'^\s*import\s+"([^"]+)"', text, flags=re.M))
    imports |= set(re.findall(r'^\s*"([^"]+)"\s*$', text, flags=re.M))
    funcs = re.findall(r"^\s*func\s+([A-Za-z0-9_]+)\s*\(", text, flags=re.M)
    types = re.findall(r"^\s*type\s+([A-Za-z0-9_]+)\s+struct\b", text, flags=re.M)
    routes: list[str] = []
    for m in re.findall(r'http\.HandleFunc\(\s*"([^"]+)"', text):
        routes.append(f"HTTP {m}")
    facts.imports = sorted(imports)[:200]
    facts.functions = [f"func {n}(...)" for n in funcs][: limits.max_functions_per_file]
    facts.classes = [f"type {n} struct" for n in types][: limits.max_classes_per_file]
    facts.routes = sorted(set(routes))[:60]
    facts.summary = "Go source file"


def _analyze_rust(text: str, facts: FileFacts, limits: Limits) -> None:
    fns = re.findall(r"^\s*(pub\s+)?fn\s+([A-Za-z0-9_]+)\s*\(", text, flags=re.M)
    structs = re.findall(r"^\s*(pub\s+)?struct\s+([A-Za-z0-9_]+)\b", text, flags=re.M)
    enums = re.findall(r"^\s*(pub\s+)?enum\s+([A-Za-z0-9_]+)\b", text, flags=re.M)
    facts.functions = [f"fn {name}(...)" for _, name in fns][: limits.max_functions_per_file]
    facts.classes = [f"struct {name}" for _, name in structs][: limits.max_classes_per_file]
    facts.models = [name for _, name in structs[:60]]
    facts.summary = "Rust source file"
    exports = [name for pub, name in fns if pub] + [name for pub, name in structs if pub] + [name for pub, name in enums if pub]
    facts.exports = sorted(set(exports))[:80]


def _analyze_java(text: str, facts: FileFacts, limits: Limits) -> None:
    classes = re.findall(r"^\s*(public\s+)?(class|interface|enum)\s+([A-Za-z0-9_]+)\b", text, flags=re.M)
    facts.classes = [f"{kind} {name}" for _, kind, name in classes][: limits.max_classes_per_file]
    if re.search(r"\bpublic\s+static\s+void\s+main\s*\(", text):
        facts.is_entrypoint = True
    facts.routes = _spring_routes(text)[:60]
    facts.summary = "Java source file"


def _spring_routes(text: str) -> list[str]:
    routes: list[str] = []
    for ann, path in re.findall(r"@(GetMapping|PostMapping|PutMapping|PatchMapping|DeleteMapping)\(\s*\"([^\"]+)\"", text):
        method = ann.replace("Mapping", "").upper()
        routes.append(f"{method} {path}")
    for path in re.findall(r"@RequestMapping\\(\\s*\"([^\"]+)\"", text):
        routes.append(f"ROUTE {path}")
    return routes


def _analyze_csharp(text: str, facts: FileFacts, limits: Limits) -> None:
    classes = re.findall(r"^\s*(public\s+)?class\s+([A-Za-z0-9_]+)\b", text, flags=re.M)
    facts.classes = [f"class {name}" for _, name in classes][: limits.max_classes_per_file]
    if re.search(r"\bstatic\s+void\s+Main\s*\(", text):
        facts.is_entrypoint = True
    routes: list[str] = []
    for ann, path in re.findall(r"\[(HttpGet|HttpPost|HttpPut|HttpPatch|HttpDelete)\\(\\s*\"([^\"]+)\"", text):
        method = ann.replace("Http", "").upper()
        routes.append(f"{method} {path}")
    facts.routes = routes[:60]
    facts.summary = "C# source file"


def _analyze_php(text: str, facts: FileFacts, limits: Limits) -> None:
    classes = re.findall(r"^\s*(abstract\s+|final\s+)?class\s+([A-Za-z0-9_]+)\b", text, flags=re.M)
    funcs = re.findall(r"^\s*function\s+([A-Za-z0-9_]+)\s*\\(", text, flags=re.M)
    facts.classes = [f"class {name}" for _, name in classes][: limits.max_classes_per_file]
    facts.functions = [f"function {n}()" for n in funcs][: limits.max_functions_per_file]
    facts.summary = "PHP source file"


def _analyze_other(text: str, facts: FileFacts, limits: Limits) -> None:
    lines = _safe_split_lines(text, 200)
    head = "\n".join(lines[:40])
    facts.summary = _truncate(head.strip().splitlines()[0] if head.strip() else "file", limits.max_file_summary_chars)


def _load_json(path: Path) -> dict:
    import json

    try:
        return json.loads(_read_text_safely(path, 300_000) or "{}")
    except Exception:
        return {}


def _detect_configs_and_deps(root: Path) -> tuple[dict[str, str], dict[str, str], list[str], list[str]]:
    configs: dict[str, str] = {}
    deps: dict[str, str] = {}
    frameworks: set[str] = set()
    pkg_mgrs: set[str] = set()

    pkg_json = root / "package.json"
    if pkg_json.exists():
        pkg_mgrs.add("npm")
        data = _load_json(pkg_json)
        scripts = data.get("scripts") or {}
        if isinstance(scripts, dict) and scripts:
            sample = ", ".join(sorted(scripts.keys())[:10])
            configs["package.json"] = f"Scripts: {sample}"
        all_deps: dict[str, str] = {}
        for k in ("dependencies", "devDependencies", "peerDependencies"):
            v = data.get(k)
            if isinstance(v, dict):
                all_deps.update({str(name): str(ver) for name, ver in v.items()})
        for name in sorted(all_deps.keys()):
            deps[name] = _dep_purpose(name)
        frameworks |= _frameworks_from_dep_names(set(all_deps.keys()))
        if (root / "yarn.lock").exists():
            pkg_mgrs.add("yarn")
        if (root / "pnpm-lock.yaml").exists():
            pkg_mgrs.add("pnpm")

    req = root / "requirements.txt"
    if req.exists():
        pkg_mgrs.add("pip")
        lines = [ln.strip() for ln in _read_text_safely(req).splitlines()]
        pkgs = []
        for ln in lines:
            if not ln or ln.startswith("#"):
                continue
            name = re.split(r"[<=>\\[]", ln, 1)[0].strip()
            if name:
                pkgs.append(name)
        for name in sorted(set(pkgs)):
            deps[name] = _dep_purpose(name)
        frameworks |= _frameworks_from_dep_names(set(pkgs))
        configs["requirements.txt"] = f"{len(set(pkgs))} dependencies"

    pyproject = root / "pyproject.toml"
    if pyproject.exists():
        pkg_mgrs.add("pip")
        configs["pyproject.toml"] = "Python project configuration"
        text = _read_text_safely(pyproject)
        deps_from_pyproject = _extract_pyproject_deps(text)
        for name in sorted(deps_from_pyproject):
            deps.setdefault(name, _dep_purpose(name))
        frameworks |= _frameworks_from_dep_names(set(deps_from_pyproject))

    gomod = root / "go.mod"
    if gomod.exists():
        pkg_mgrs.add("go")
        configs["go.mod"] = "Go module"
        text = _read_text_safely(gomod)
        for dep in sorted(set(re.findall(r"^\\s*require\\s+([^\\s]+)", text, flags=re.M))):
            deps.setdefault(dep, "Go module dependency")

    cargo = root / "Cargo.toml"
    if cargo.exists():
        pkg_mgrs.add("cargo")
        configs["Cargo.toml"] = "Rust crate"
        text = _read_text_safely(cargo)
        for dep in sorted(_extract_cargo_deps(text)):
            deps.setdefault(dep, _dep_purpose(dep))

    dockerfile = root / "Dockerfile"
    if dockerfile.exists():
        configs["Dockerfile"] = "Container build definition"
        frameworks.add("Docker")
    compose = None
    for name in ("docker-compose.yml", "docker-compose.yaml"):
        p = root / name
        if p.exists():
            compose = p
            break
    if compose:
        configs[compose.name] = "Local multi-service environment"
        frameworks.add("Docker Compose")

    makefile = root / "Makefile"
    if makefile.exists():
        configs["Makefile"] = "Build/run automation"

    # Basic CI detection
    gha = root / ".github" / "workflows"
    if gha.exists() and gha.is_dir():
        workflows = sorted([p.name for p in gha.glob("*.yml")]) + sorted([p.name for p in gha.glob("*.yaml")])
        if workflows:
            configs[".github/workflows/"] = f"GitHub Actions workflows: {', '.join(workflows[:8])}"

    return configs, deps, sorted(frameworks), sorted(pkg_mgrs)


def _extract_pyproject_deps(text: str) -> list[str]:
    # Heuristic: look for common dependency blocks (poetry/PEP 621) without full TOML parsing.
    deps: set[str] = set()
    # PEP 621: dependencies = ["x", "y>=1"]
    for block in re.findall(r"(?ms)^\\s*dependencies\\s*=\\s*\\[(.*?)\\]", text):
        for item in re.findall(r"\"([^\"]+)\"|'([^']+)'", block):
            raw = item[0] or item[1]
            name = re.split(r"[<=>\\[]", raw, 1)[0].strip()
            if name:
                deps.add(name)
    # Poetry: [tool.poetry.dependencies] section with key = "version"
    m = re.search(r"(?ms)^\\[tool\\.poetry\\.dependencies\\]\\s*(.*?)(^\\[|\\Z)", text)
    if m:
        section = m.group(1)
        for name in re.findall(r"^\\s*([A-Za-z0-9_.-]+)\\s*=", section, flags=re.M):
            if name.lower() != "python":
                deps.add(name)
    return sorted(deps)


def _extract_cargo_deps(text: str) -> list[str]:
    # Minimal parsing of [dependencies] and [dev-dependencies].
    deps: set[str] = set()
    for table in ("dependencies", "dev-dependencies", "build-dependencies"):
        m = re.search(rf"(?ms)^\\[{re.escape(table)}\\]\\s*(.*?)(^\\[|\\Z)", text)
        if not m:
            continue
        section = m.group(1)
        for name in re.findall(r"^\\s*([A-Za-z0-9_-]+)\\s*=", section, flags=re.M):
            deps.add(name)
    return sorted(deps)


def _frameworks_from_dep_names(dep_names: set[str]) -> set[str]:
    lowered = {d.lower() for d in dep_names}
    frameworks: set[str] = set()
    if {"fastapi", "starlette"} & lowered:
        frameworks.add("FastAPI/Starlette")
    if {"flask"} & lowered:
        frameworks.add("Flask")
    if {"django"} & lowered:
        frameworks.add("Django")
    if {"pytest"} & lowered:
        frameworks.add("Pytest")
    if {"requests"} & lowered:
        frameworks.add("Requests")
    if {"sqlalchemy", "alembic"} & lowered:
        frameworks.add("SQLAlchemy")
    if {"pydantic"} & lowered:
        frameworks.add("Pydantic")
    if {"celery"} & lowered:
        frameworks.add("Celery")
    if {"redis"} & lowered:
        frameworks.add("Redis")
    if {"express"} & lowered:
        frameworks.add("Express")
    if {"react"} & lowered:
        frameworks.add("React")
    if {"next"} & lowered:
        frameworks.add("Next.js")
    if {"nestjs"} & lowered:
        frameworks.add("NestJS")
    if {"jest"} & lowered:
        frameworks.add("Jest")
    if {"vitest"} & lowered:
        frameworks.add("Vitest")
    if {"mocha"} & lowered:
        frameworks.add("Mocha")
    if {"typeorm"} & lowered:
        frameworks.add("TypeORM")
    if {"prisma"} & lowered:
        frameworks.add("Prisma")
    if {"gin-gonic/gin"} & lowered:
        frameworks.add("Gin (Go)")
    if {"actix-web"} & lowered:
        frameworks.add("actix-web (Rust)")
    return frameworks


def _dep_purpose(name: str) -> str:
    n = name.lower()
    mapping = {
        "fastapi": "API framework",
        "starlette": "ASGI framework",
        "flask": "Web framework",
        "django": "Web framework",
        "pytest": "Testing framework",
        "requests": "HTTP client",
        "sqlalchemy": "ORM / database toolkit",
        "alembic": "Database migrations",
        "pydantic": "Data validation / schemas",
        "uvicorn": "ASGI server",
        "gunicorn": "WSGI server",
        "celery": "Background jobs / task queue",
        "redis": "Caching / broker",
        "boto3": "AWS SDK",
        "google-cloud-storage": "GCP SDK",
        "azure-storage-blob": "Azure SDK",
        "express": "Node.js web framework",
        "react": "UI framework",
        "next": "React framework",
        "nestjs": "Node.js backend framework",
        "typeorm": "ORM",
        "prisma": "ORM / schema",
        "jest": "Testing framework",
        "vitest": "Testing framework",
        "webpack": "Bundler",
        "vite": "Bundler/dev server",
    }
    return mapping.get(n, "Dependency")


def infer_application_type(languages: Counter[str], frameworks: list[str], entrypoints: list[FileFacts]) -> str:
    langs = {k for k, _ in languages.most_common()}
    fw = {f.lower() for f in frameworks}
    ep = {Path(e.path).name.lower() for e in entrypoints}
    if {"React", "Next.js"} & set(frameworks):
        return "frontend app"
    if any("fastapi" in f.lower() or "flask" in f.lower() or "django" in f.lower() for f in frameworks):
        return "backend service (web API)"
    if any(n in ep for n in ("cli.py", "__main__.py")):
        return "CLI tool"
    if len(langs - {"Other"}) >= 3:
        return "monorepo / polyglot"
    if "Python" in langs and "JavaScript" in langs:
        return "full-stack app"
    if "Go" in langs:
        return "service (Go)"
    if "Rust" in langs:
        return "service/library (Rust)"
    if "Java" in langs:
        return "service/library (Java)"
    if "C#" in langs:
        return "service/library (C#)"
    if "PHP" in langs:
        return "service/library (PHP)"
    return "repository"


def _infer_project_purpose(root: Path, files: list[FileFacts], frameworks: list[str]) -> str:
    # Prefer README title/first lines if present.
    readme = None
    for name in ("README.md", "README.rst", "README.txt"):
        p = root / name
        if p.exists():
            readme = p
            break
    if readme:
        text = _read_text_safely(readme, 60_000)
        for line in text.splitlines():
            line = line.strip()
            if not line:
                continue
            if line.startswith("#"):
                return line.lstrip("#").strip()
            return _truncate(line, 120)
    if frameworks:
        return f"Project using {', '.join(frameworks[:3])}"
    return f"Repository at {root.name}"


def _infer_architecture(files: list[FileFacts]) -> list[str]:
    rels = [f.path.lower() for f in files]
    notes: list[str] = []
    if any(r.startswith("src/") for r in rels):
        notes.append("Uses `src/` source layout")
    if any("/apps/" in r or r.startswith("apps/") for r in rels):
        notes.append("Multi-app structure (`apps/`)")
    if any("/packages/" in r or r.startswith("packages/") for r in rels):
        notes.append("Packages/modules split (`packages/`)")
    if any("/api/" in r for r in rels):
        notes.append("Has API layer (`api/`)")
    if any("/service" in r for r in rels):
        notes.append("Service layer present (`service/`)")
    if any("/model" in r or "/schema" in r for r in rels):
        notes.append("Model/schema layer present")
    if any(r.startswith(".github/workflows/") for r in rels):
        notes.append("Has CI workflows (GitHub Actions)")
    return notes[:8] or ["Architecture inferred from file layout and entrypoints"]


def _build_run_instructions(configs: dict[str, str], frameworks: list[str], deps: dict[str, str]) -> list[str]:
    instructions: list[str] = []
    if "package.json" in configs:
        instructions.extend(["npm install", "npm run dev (or npm start)", "npm test"])
    if "pyproject.toml" in configs or "requirements.txt" in configs:
        instructions.extend(["python -m venv .venv && source .venv/bin/activate", "pip install -r requirements.txt", "pytest"])
    if "Dockerfile" in configs:
        instructions.append("docker build -t app .")
    if any("Docker Compose" in f for f in frameworks) or any(k.startswith("docker-compose") for k in configs):
        instructions.append("docker compose up")
    if "go.mod" in configs:
        instructions.extend(["go test ./...", "go run ."])
    if "Cargo.toml" in configs:
        instructions.extend(["cargo test", "cargo run"])
    # Deduplicate
    out: list[str] = []
    seen: set[str] = set()
    for x in instructions:
        if x not in seen:
            seen.add(x)
            out.append(x)
    return out[:10]


def _derive_api_surface(files: list[FileFacts]) -> list[str]:
    routes: set[str] = set()
    for f in files:
        for r in f.routes:
            routes.add(r)
    return sorted(routes)[:120]


def _derive_data_models(files: list[FileFacts]) -> list[str]:
    models: set[str] = set()
    for f in files:
        for m in f.models:
            models.add(m)
        # Also include obvious model folder classes.
        if "/models" in f.path.lower() or "/model" in f.path.lower():
            for c in f.classes:
                models.add(c.replace("class ", "").split("(")[0].strip())
    return sorted(models)[:120]


def _dev_notes(files: list[FileFacts]) -> list[str]:
    # Emit a compact marker summary plus a few hotspots.
    total = sum(f.todo_markers for f in files)
    hotspots = sorted([f for f in files if f.todo_markers > 0], key=lambda x: (-x.todo_markers, x.path))[:10]
    notes = [f"TODO/FIXME/HACK markers found: {total}"]
    for f in hotspots:
        notes.append(f"{f.path}: {f.todo_markers}")
    return notes


def _testing_facts(root: Path, files: list[FileFacts], frameworks: list[str], deps: dict[str, str]) -> list[str]:
    test_files = [f for f in files if f.is_test]
    facts: list[str] = []
    if test_files:
        facts.append(f"Test files detected: {len(test_files)}")
    if any("Pytest" in f for f in frameworks) or "pytest" in (k.lower() for k in deps.keys()):
        facts.append("Likely uses Pytest")
    if any("Jest" in f for f in frameworks) or "jest" in (k.lower() for k in deps.keys()):
        facts.append("Likely uses Jest")
    if (root / "tests").exists() or (root / "__tests__").exists():
        facts.append("Tests organized under a dedicated test directory")
    if not facts:
        facts.append("No obvious test framework detected (heuristic)")
    return facts[:8]


def _internal_relationships(files: list[FileFacts]) -> list[str]:
    # Simple heuristic: detect layering by imports across path categories.
    layer_of: dict[str, str] = {}
    for f in files:
        p = f.path.lower()
        if "/controller" in p or "/handlers" in p or "/routes" in p:
            layer_of[f.path] = "controller/router"
        elif "/service" in p or "/usecase" in p:
            layer_of[f.path] = "service"
        elif "/repo" in p or "/repository" in p or "/dao" in p or "/db" in p:
            layer_of[f.path] = "data-access"
        elif "/model" in p or "/schema" in p:
            layer_of[f.path] = "model"

    notes: list[str] = []
    # Mention common pattern if multiple layers present.
    layers = set(layer_of.values())
    if {"controller/router", "service", "data-access"} <= layers:
        notes.append("Likely layering: controller/router → service → data-access")
    if {"service", "model"} <= layers:
        notes.append("Services appear to depend on model/schema types")
    if not notes:
        notes.append("Internal relationships inferred primarily via imports and file layout (heuristic)")
    return notes[:8]


def build_repo_facts(root: Path, limits: Limits) -> RepoFacts:
    paths, skipped = scan_repository(root, limits)
    analyzed: list[FileFacts] = [analyze_file(root, p, limits) for p in paths]
    # Remove "minified skipped" pseudo-items from downstream views
    analyzed = [f for f in analyzed if f.score >= -0.5]

    langs = Counter(f.language for f in analyzed)
    configs, deps, frameworks, pkg_mgrs = _detect_configs_and_deps(root)

    entrypoints = sorted([f for f in analyzed if f.is_entrypoint], key=lambda x: x.path)
    api_surface = _derive_api_surface(analyzed)
    data_models = _derive_data_models(analyzed)
    test_facts = _testing_facts(root, analyzed, frameworks, deps)
    build_run = _build_run_instructions(configs, frameworks, deps)
    dev_notes = _dev_notes(analyzed)
    arch_notes = _infer_architecture(analyzed)

    return RepoFacts(
        root=root,
        files=analyzed,
        skipped_files=skipped,
        languages=langs,
        frameworks=frameworks,
        package_managers=pkg_mgrs,
        configs=configs,
        dependencies=deps,
        entrypoints=entrypoints,
        api_surface=api_surface,
        data_models=data_models,
        test_facts=test_facts,
        build_run_instructions=build_run,
        dev_notes=dev_notes,
        architecture_notes=arch_notes,
    )


def _top_important_files(files: list[FileFacts], limit: int = 25) -> list[FileFacts]:
    # Prefer high-score non-test files; include a few tests if they score high.
    ranked = sorted(files, key=lambda f: (-f.score, f.is_test, f.path))
    return ranked[:limit]


def generate_markdown(repo: RepoFacts, limits: Limits) -> str:
    now = datetime.utcnow().strftime("%Y-%m-%d")
    purpose = _infer_project_purpose(repo.root, repo.files, repo.frameworks)
    app_type = infer_application_type(repo.languages, repo.frameworks, repo.entrypoints)
    total_analyzed = len(repo.files)
    language_list = ", ".join([f"{k} ({v})" for k, v in repo.languages.most_common()])
    frameworks_list = ", ".join(repo.frameworks) if repo.frameworks else "None detected (heuristic)"
    pkg_mgrs = ", ".join(repo.package_managers) if repo.package_managers else "None detected"

    files_paths = [repo.root / f.path for f in repo.files if not f.path.startswith("/")]
    tree = _tree_lines(repo.root, [repo.root / f.path for f in repo.files], limits.max_tree_depth)

    entry_lines: list[str] = []
    for e in repo.entrypoints[:15]:
        desc = e.summary or "entrypoint"
        entry_lines.append(f"- `{e.path}` — {desc}")
    if not entry_lines:
        entry_lines.append("- (No obvious entrypoints detected; heuristics look for main/app/server/index files)")

    important = _top_important_files(repo.files, limit=30)

    md: list[str] = []
    md.append("Given this context:")
    md.append("")

    md.append("1. Project Overview")
    md.append(f"- Purpose (inferred): {purpose}")
    md.append(f"- Application type (inferred): {app_type}")
    md.append(f"- Architecture (inferred): {', '.join(repo.architecture_notes)}")
    md.append(f"- Detected frameworks: {frameworks_list}")
    md.append(f"- Detected languages: {language_list or 'None detected'}")
    md.append(f"- Repository scale: {total_analyzed} files analyzed, {repo.skipped_files} skipped (filters/limits)")
    md.append("")

    md.append("2. Tech Stack")
    md.append(f"- Languages: {', '.join([k for k, _ in repo.languages.most_common()]) or 'Unknown'}")
    md.append(f"- Frameworks: {frameworks_list}")
    md.append(f"- Package managers: {pkg_mgrs}")
    # Infra/DB/queues (heuristic from deps)
    infra = []
    lowered_deps = {k.lower() for k in repo.dependencies.keys()}
    if "docker" in {f.lower() for f in repo.frameworks} or "dockerfile" in repo.configs:
        infra.append("Docker")
    if any(k.startswith("docker-compose") for k in repo.configs):
        infra.append("Docker Compose")
    if {"boto3"} & lowered_deps:
        infra.append("AWS (boto3)")
    if any(d.startswith("google-cloud-") for d in lowered_deps):
        infra.append("GCP SDKs")
    if any(d.startswith("azure-") for d in lowered_deps):
        infra.append("Azure SDKs")
    dbq = []
    if {"sqlalchemy", "alembic"} & lowered_deps:
        dbq.append("SQL database (via SQLAlchemy/Alembic)")
    if {"redis"} & lowered_deps:
        dbq.append("Redis")
    if {"celery"} & lowered_deps:
        dbq.append("Celery (queue)")
    if infra:
        md.append(f"- Infrastructure: {', '.join(sorted(set(infra)))}")
    if dbq:
        md.append(f"- Datastores/queues: {', '.join(sorted(set(dbq)))}")
    md.append("")

    md.append("3. Repository Structure")
    md.append("- Tree (filtered, max depth limited):")
    for line in tree:
        md.append(f"  {line}")
    md.append("")

    md.append("4. Entry Points")
    md.extend(entry_lines)
    md.append("")

    md.append("5. Important Files")
    for f in important:
        md.append(f"- `{f.path}`")
        md.append(f"  - Summary: {f.summary}")
        if f.responsibilities:
            md.append(f"  - Responsibilities: {', '.join(f.responsibilities)}")
        if f.exports:
            md.append(f"  - Exports: {', '.join(f.exports[:12])}{'…' if len(f.exports) > 12 else ''}")
        if f.classes:
            md.append(f"  - Classes: {', '.join(f.classes[:8])}{'…' if len(f.classes) > 8 else ''}")
        if f.functions:
            md.append(f"  - Functions: {', '.join(f.functions[:8])}{'…' if len(f.functions) > 8 else ''}")
        if f.routes:
            md.append(f"  - Routes: {', '.join(f.routes[:6])}{'…' if len(f.routes) > 6 else ''}")
        if f.models:
            md.append(f"  - Models: {', '.join(f.models[:8])}{'…' if len(f.models) > 8 else ''}")
    md.append("")

    md.append("6. Configuration")
    if repo.configs:
        for k in sorted(repo.configs.keys(), key=str.lower):
            md.append(f"- `{k}` — {repo.configs[k]}")
    else:
        md.append("- No common configuration files detected (heuristic)")
    md.append("")

    md.append("7. Dependency Summary")
    if repo.dependencies:
        # Prioritize known purposes and common deps.
        important_deps = sorted(repo.dependencies.items(), key=lambda kv: (kv[1] == "Dependency", kv[0].lower()))
        for name, purpose_ in important_deps[:40]:
            md.append(f"- {name} → {purpose_}")
    else:
        md.append("- No dependencies detected from config files (heuristic)")
    md.append("")

    md.append("8. API Surface")
    if repo.api_surface:
        for r in repo.api_surface[:80]:
            md.append(f"- {r}")
    else:
        md.append("- No obvious API surface detected (heuristic)")
    md.append("")

    md.append("9. Data Models")
    if repo.data_models:
        for m in repo.data_models[:80]:
            md.append(f"- {m}")
    else:
        md.append("- No obvious data models detected (heuristic)")
    md.append("")

    md.append("10. Internal Relationships")
    for n in _internal_relationships(repo.files):
        md.append(f"- {n}")
    md.append("")

    md.append("11. Testing")
    for t in repo.test_facts:
        md.append(f"- {t}")
    md.append("")

    md.append("12. Build / Run Instructions")
    if repo.build_run_instructions:
        for c in repo.build_run_instructions:
            md.append(f"- `{c}`")
    else:
        md.append("- No build/run commands inferred (heuristic)")
    md.append("")

    md.append("13. Development Notes")
    for n in repo.dev_notes[:12]:
        md.append(f"- {n}")
    md.append("")

    md.append("14. Suggested Questions")
    for q in suggested_questions(repo):
        md.append(f"- {q}")
    md.append("")

    md.append("15. Optimized Agent Context")
    for line in optimized_agent_context(repo, limits):
        md.append(f"- {line}")
    md.append("")

    md.append("I have the following question:")
    return "\n".join(md)


def suggested_questions(repo: RepoFacts) -> list[str]:
    # Deterministic prompts driven by detected surface/entrypoints.
    qs: list[str] = []
    if repo.entrypoints:
        qs.append(f"Where does execution start (see: {repo.entrypoints[0].path})?")
    if repo.api_surface:
        qs.append("How are requests routed from entrypoints to handlers/services?")
        qs.append("Which endpoints are public vs internal, and where is auth enforced?")
    if repo.data_models:
        qs.append("Which data models are core to the domain, and where are they validated?")
    if repo.dependencies:
        qs.append("Which dependencies are critical at runtime vs dev-only, and why?")
    qs.extend(
        [
            "Where is the safest place to implement a new feature without breaking architecture layering?",
            "Which modules are the most central (imported widely) and should be changed carefully?",
            "What are the riskiest/most complex files based on size, routing, and TODO markers?",
        ]
    )
    # Keep it compact and stable
    out: list[str] = []
    seen: set[str] = set()
    for q in qs:
        if q not in seen:
            seen.add(q)
            out.append(q)
    return out[:10]


def optimized_agent_context(repo: RepoFacts, limits: Limits) -> list[str]:
    # Compact, token-optimized summary for LLM ingestion.
    top_files = _top_important_files(repo.files, limit=12)
    lang = ", ".join([k for k, _ in repo.languages.most_common()])
    fw = ", ".join(repo.frameworks[:6]) if repo.frameworks else "none"
    lines: list[str] = []
    lines.append(f"App type: {infer_application_type(repo.languages, repo.frameworks, repo.entrypoints)}; langs={lang or 'unknown'}; frameworks={fw}.")
    if repo.entrypoints:
        lines.append(f"Entrypoints: {', '.join(e.path for e in repo.entrypoints[:5])}.")
    if repo.api_surface:
        lines.append(f"API surface: {len(repo.api_surface)} routes detected; see 'API Surface' for list.")
    if repo.data_models:
        lines.append(f"Models: {len(repo.data_models)} model-like types detected; see 'Data Models'.")
    if repo.configs:
        lines.append(f"Config: {', '.join(sorted(repo.configs.keys(), key=str.lower)[:6])}.")
    if repo.build_run_instructions:
        lines.append(f"Common commands: {', '.join(repo.build_run_instructions[:6])}.")
    if repo.architecture_notes:
        lines.append(f"Layout: {', '.join(repo.architecture_notes[:4])}.")
    if top_files:
        lines.append("Key files (ranked): " + ", ".join(f.path for f in top_files) + ".")
    # Keep stable length and avoid verbosity
    return [_truncate(l, 260) for l in lines][:10]


def write_output(root: Path, output_arg: str, content: str) -> Path:
    out_path = Path(output_arg)
    if not out_path.is_absolute():
        out_path = root / out_path
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(content, encoding="utf-8", newline="\n")
    return out_path


def parse_args(argv: Optional[list[str]] = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        prog="codebase2context",
        description="Generate an LLM-optimized Markdown context file for a repository (offline, stdlib-only).",
    )
    p.add_argument(
        "path",
        nargs="?",
        default=".",
        help="Repository path to analyze (default: current directory).",
    )
    p.add_argument(
        "--output",
        default="CODEBASE_CONTEXT.md",
        help="Output Markdown filename (written inside the target repository unless absolute).",
    )
    p.add_argument("--max-files", type=int, default=MAX_FILES_ANALYZED, help="Maximum files to analyze.")
    p.add_argument("--max-depth", type=int, default=MAX_TREE_DEPTH, help="Maximum repository tree depth.")
    p.add_argument(
        "--max-summary-chars",
        type=int,
        default=MAX_FILE_SUMMARY_CHARS,
        help="Max characters for any per-file summary.",
    )
    p.add_argument("--max-functions", type=int, default=MAX_FUNCTIONS_PER_FILE, help="Max functions extracted per file.")
    p.add_argument("--max-classes", type=int, default=MAX_CLASSES_PER_FILE, help="Max classes extracted per file.")
    return p.parse_args(argv)


def main(argv: Optional[list[str]] = None) -> int:
    args = parse_args(argv)
    root = Path(args.path).expanduser().resolve()
    if not root.exists() or not root.is_dir():
        print(f"error: path is not a directory: {root}", file=sys.stderr)
        return 2

    limits = Limits(
        max_file_summary_chars=max(200, int(args.max_summary_chars)),
        max_tree_depth=max(1, int(args.max_depth)),
        max_files_analyzed=max(10, int(args.max_files)),
        max_functions_per_file=max(1, int(args.max_functions)),
        max_classes_per_file=max(1, int(args.max_classes)),
    )

    repo = build_repo_facts(root, limits)
    md = generate_markdown(repo, limits)
    out = write_output(root, args.output, md)
    print(f"Wrote {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

