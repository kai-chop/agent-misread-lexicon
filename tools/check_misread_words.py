#!/usr/bin/env python3
"""Lint markdown files against a misread lexicon.

The lexicon lives in one or more markdown files (see lexicon/lexicon.*.md).
Each lexicon file has a table located by a `<!-- misread-lexicon:table -->`
marker comment; the table is parsed positionally (column 1 = rule type,
column 2 = misread form(s) separated by "/", column 3 = clearer form,
column 4 = source), so the header row's language and wording do not matter.

This tool only reads files. It never modifies a scanned file.
"""
import argparse
import json
import os
import re
import sys
import tempfile
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8")
except AttributeError:
    # Python < 3.7 (or a stream without reconfigure) - fall back silently.
    pass


TABLE_MARKER = "<!-- misread-lexicon:table -->"

_FENCE_RE = re.compile(r"^\s*(`{3,}|~{3,})")
_INLINE_CODE_RE = re.compile(r"`[^`\n]*`")
_SEPARATOR_ROW_RE = re.compile(r"^[\|\s:-]+$")
_DATE_RE = re.compile(r"\d{4}-\d{2}-\d{2}")


# ---------------------------------------------------------------------------
# Lexicon parsing
# ---------------------------------------------------------------------------

def parse_lexicon_table(path):
    """Parse the marker-delimited table in a lexicon file into rule dicts.

    Each rule dict is {"type": str, "forms": [str, ...], "target": str,
    "source": str}. Returns [] if the file has no marker (or doesn't exist).
    """
    path = Path(path)
    if not path.exists():
        return []
    with open(path, "r", encoding="utf-8") as f:
        lines = f.readlines()

    # The marker must be the line's entire (stripped) content - "EXACTLY this
    # marker line on its own line" - so prose that merely quotes the marker
    # syntax (e.g. in a doc comment) is never mistaken for the real thing.
    marker_idx = None
    for i, line in enumerate(lines):
        if line.strip() == TABLE_MARKER:
            marker_idx = i
            break
    if marker_idx is None:
        return []

    i = marker_idx + 1
    while i < len(lines) and lines[i].strip() == "":
        i += 1

    # Header row (language-independent - skipped unconditionally).
    if i < len(lines) and lines[i].strip().startswith("|"):
        i += 1

    # Separator row, e.g. "|---|---|---|---|".
    if i < len(lines) and _SEPARATOR_ROW_RE.match(lines[i].strip()):
        i += 1

    rules = []
    while i < len(lines):
        stripped = lines[i].strip()
        if not stripped.startswith("|"):
            break
        cols = [c.strip() for c in stripped.strip("|").split("|")]
        if len(cols) >= 4 and cols[0]:
            rule_type, misread, clearer, source = cols[0], cols[1], cols[2], cols[3]
            forms = [f.strip() for f in misread.split("/") if f.strip() != ""]
            rules.append({
                "type": rule_type,
                "forms": forms,
                "target": clearer,
                "source": source,
            })
        i += 1
    return rules


# ---------------------------------------------------------------------------
# Exemptions: fenced code blocks, inline code spans, blockquote lines
# ---------------------------------------------------------------------------

def sanitize_lines(raw_lines):
    """Blank out exempt regions, preserving line count (and, for inline code
    spans, line length) so line numbers in findings stay accurate."""
    sanitized = []
    in_fence = False
    fence_char = ""
    fence_len = 0

    for raw in raw_lines:
        line = raw.rstrip("\n").rstrip("\r")

        m = _FENCE_RE.match(line)
        if m:
            marker = m.group(1)
            char = marker[0]
            length = len(marker)
            if in_fence:
                if char == fence_char and length >= fence_len:
                    in_fence = False
            else:
                in_fence = True
                fence_char = char
                fence_len = length
            sanitized.append(" " * len(line))
            continue

        if in_fence:
            sanitized.append(" " * len(line))
            continue

        if line.lstrip().startswith(">"):
            sanitized.append(" " * len(line))
            continue

        sanitized.append(_INLINE_CODE_RE.sub(lambda mm: " " * len(mm.group(0)), line))

    return sanitized


# ---------------------------------------------------------------------------
# Rule checks
# ---------------------------------------------------------------------------

def check_m1(sanitized_lines, token):
    """Return 1-indexed line numbers with a bare (unexpanded) occurrence of
    token, or [] if the token was expanded (TOKEN( or TOKEN（) anywhere."""
    full_text = "\n".join(sanitized_lines)
    if (token + "(") in full_text or (token + "（") in full_text:
        return []
    pattern = re.compile(r"(?<![A-Za-z0-9])" + re.escape(token) + r"(?![A-Za-z0-9])")
    return [i for i, line in enumerate(sanitized_lines, start=1) if pattern.search(line)]


def _is_ascii(s):
    return all(ord(c) < 128 for c in s)


def check_m5(sanitized_lines, form):
    """Return 1-indexed line numbers containing form, skipping lines that
    already carry an absolute date (YYYY-MM-DD)."""
    if _is_ascii(form):
        pattern = re.compile(
            r"(?<![A-Za-z0-9])" + re.escape(form) + r"(?![A-Za-z0-9])", re.IGNORECASE
        )
    else:
        pattern = re.compile(re.escape(form))
    findings = []
    for i, line in enumerate(sanitized_lines, start=1):
        if _DATE_RE.search(line):
            continue
        if pattern.search(line):
            findings.append(i)
    return findings


def lint_text(text, rules, allowed_types):
    """Lint text (already-read file contents) against rules, restricted to
    allowed_types. Returns a list of (lineno, type, form, clearer)."""
    sanitized = sanitize_lines(text.splitlines(keepends=True))
    findings = []
    for rule in rules:
        if rule["type"] not in allowed_types:
            continue
        if rule["type"] == "M1":
            for token in rule["forms"]:
                for ln in check_m1(sanitized, token):
                    findings.append((ln, "M1", token, rule["target"]))
        elif rule["type"] == "M5":
            for form in rule["forms"]:
                for ln in check_m5(sanitized, form):
                    findings.append((ln, "M5", form, rule["target"]))
        else:
            # Unknown rule type: parsed (counted in the alive line) but no
            # checks are performed, for forward compatibility.
            continue
    findings.sort(key=lambda t: t[0])
    return findings


# ---------------------------------------------------------------------------
# Config / scan-target resolution
# ---------------------------------------------------------------------------

def resolve_globs(root, patterns):
    result = set()
    root_path = Path(root)
    for pattern in patterns:
        try:
            for p in root_path.glob(pattern):
                if p.is_file():
                    result.add(p.resolve())
        except OSError:
            continue
    return result


def resolve_roots(config, root_override=None):
    """Return (existing_roots, declared_count).

    Accepts `root` (a single path) or `roots` (a list) so one config can cover
    several agent homes at once -- ~/.claude, ~/.codex, ~/.gemini, a project
    checkout. Nobody has all of them, so a declared root that does not exist is
    skipped silently rather than erroring: the normal state is a partial match.
    `--root` overrides every declared root with exactly one."""
    if root_override:
        declared = [root_override]
    elif "roots" in config:
        declared = config.get("roots") or []
        if isinstance(declared, str):
            declared = [declared]
    else:
        declared = [config.get("root", ".")]
    existing = []
    for raw in declared:
        p = Path(os.path.expanduser(str(raw)))
        if p.is_dir():
            existing.append(p)
    return existing, len(declared)


def build_scan_targets(config, roots):
    """Return {abs_path: set(rule_types)} from the config's scan blocks.

    `roots` is a list; every scan block is resolved against each root and the
    results unioned, so a file matched under two roots keeps both blocks' rules."""
    if isinstance(roots, (str, Path)):
        roots = [roots]
    file_rules_map = {}
    for block in config.get("scan", []):
        include = block.get("include", [])
        exclude = block.get("exclude", [])
        block_rules = set(block.get("rules", []))
        for root in roots:
            included = resolve_globs(root, include)
            excluded = resolve_globs(root, exclude)
            for f in included - excluded:
                file_rules_map.setdefault(f, set()).update(block_rules)
    return file_rules_map


def load_all_rules(config_dir, lexicon_rel_paths):
    all_rules = []
    lexicon_abs_paths = set()
    for rel in lexicon_rel_paths:
        abs_path = (config_dir / rel).resolve()
        lexicon_abs_paths.add(abs_path)
        all_rules.extend(parse_lexicon_table(abs_path))
    return all_rules, lexicon_abs_paths


def perform_scan(config, config_dir, root_override=None, paths_override=None, rules_override=None):
    """Run the full lint pipeline. Returns (all_rules, findings, targets_count,
    lexicon_abs_paths). findings is a list of (path, lineno, type, form, clearer)."""
    config_dir = Path(config_dir)
    lexicon_rel = config.get("lexicons", [])
    all_rules, lexicon_abs_paths = load_all_rules(config_dir, lexicon_rel)

    if paths_override is not None:
        file_rules_map = {}
        if rules_override:
            allowed = set(t.strip() for t in rules_override.split(",") if t.strip())
        else:
            allowed = set(r["type"] for r in all_rules)
        for p in paths_override:
            file_rules_map[Path(p).resolve()] = allowed
    else:
        roots, _declared = resolve_roots(config, root_override)
        file_rules_map = build_scan_targets(config, roots)

    targets_count = len(file_rules_map)
    findings = []
    for abs_path in sorted(file_rules_map, key=lambda p: str(p)):
        allowed_types = file_rules_map[abs_path]
        if abs_path in lexicon_abs_paths:
            continue  # a configured lexicon file is exempt from its own rules
        if not abs_path.exists() or not abs_path.is_file():
            continue
        try:
            text = abs_path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError) as exc:
            print(f"[misread-lint] warning: skipping unreadable file {abs_path}: {exc}", file=sys.stderr)
            continue
        for (ln, rtype, form, clearer) in lint_text(text, all_rules, allowed_types):
            findings.append((abs_path, ln, rtype, form, clearer))

    return all_rules, findings, targets_count, lexicon_abs_paths


def resolve_default_config_path():
    """--config default: misread-lexicon.json in cwd, else
    misread-lexicon.example.json next to this tool's repo root."""
    cwd_config = Path.cwd() / "misread-lexicon.json"
    if cwd_config.exists():
        return cwd_config.resolve()
    repo_root = Path(__file__).resolve().parent.parent
    example_config = repo_root / "misread-lexicon.example.json"
    if example_config.exists():
        return example_config.resolve()
    return None


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def build_arg_parser():
    p = argparse.ArgumentParser(
        prog="check_misread_words.py",
        description="Lint files for tokens/phrases known to be misread, per a lexicon.",
    )
    p.add_argument("--config", default=None, help="Path to a misread-lexicon.json config file.")
    p.add_argument("--root", default=None, help="Override the config's 'root' path (~ expanded).")
    p.add_argument("--paths", nargs="+", default=None, help="Lint these files instead of the config scan.")
    p.add_argument(
        "--rules",
        default=None,
        help="Comma-separated rule types to apply with --paths (default: all types present in the lexicons).",
    )
    p.add_argument("--self-test", action="store_true", help="Run embedded self-tests and exit.")
    return p


def main(argv=None):
    args = build_arg_parser().parse_args(argv)

    if args.self_test:
        return run_self_tests()

    if args.config:
        config_path = Path(args.config)
        if not config_path.exists():
            print(f"[misread-lint] config not found: {config_path}", file=sys.stderr)
            return 2
        config_path = config_path.resolve()
    else:
        config_path = resolve_default_config_path()
        if config_path is None:
            print(
                "[misread-lint] no config found. Looked for 'misread-lexicon.json' in the "
                "current directory and 'misread-lexicon.example.json' next to this tool's "
                "repo root. Copy misread-lexicon.example.json to misread-lexicon.json and "
                "edit it, or pass --config PATH.",
                file=sys.stderr,
            )
            return 2

    try:
        config = json.loads(config_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        print(f"[misread-lint] invalid JSON in config {config_path}: {exc}", file=sys.stderr)
        return 2

    config_dir = config_path.parent

    all_rules, findings, targets_count, _lexicon_abs_paths = perform_scan(
        config,
        config_dir,
        root_override=args.root,
        paths_override=args.paths,
        rules_override=args.rules,
    )

    if args.paths is not None:
        roots_str = "-/-"
    else:
        existing_roots, declared_count = resolve_roots(config, args.root)
        roots_str = f"{len(existing_roots)}/{declared_count}"

    print(f"[misread-lint] alive: rules={len(all_rules)} roots={roots_str} targets={targets_count}")
    for (path, lineno, rtype, form, clearer) in findings:
        print(f'{path}:{lineno}: [{rtype}] "{form}" -> {clearer}')
    print(f"[misread-lint] summary: {len(findings)} finding(s) across {targets_count} target(s)")

    return 1 if findings else 0


# ---------------------------------------------------------------------------
# Self-test
# ---------------------------------------------------------------------------

def run_self_tests():
    results = []

    def record(label, passed):
        print(("PASS: " if passed else "FAIL: ") + label)
        results.append(bool(passed))

    def safe_check(label, fn):
        try:
            record(label, fn())
        except Exception as exc:  # noqa: BLE001 - self-test must not crash the run
            record(label + f" (raised {exc!r})", False)

    m1_rules = [{"type": "M1", "forms": ["UE"], "target": "UE (Unreal Engine)", "source": "example"}]
    m5_ja_rules = [{
        "type": "M5",
        "forms": ["昨日", "今日", "明日", "先週", "来週", "再来週"],
        "target": "an absolute date",
        "source": "example",
    }]
    m5_en_rules = [{
        "type": "M5",
        "forms": ["yesterday", "today", "tomorrow", "last week", "next week"],
        "target": "an absolute date",
        "source": "example",
    }]

    safe_check(
        "M1 positive: bare token, no expansion -> flagged",
        lambda: len(lint_text("The UE engine is powerful.\n", m1_rules, {"M1"})) == 1,
    )
    safe_check(
        "M1 negative: expanded at first use, bare later -> not flagged",
        lambda: lint_text(
            "We use UE(Unreal Engine) here.\nLater we mention UE again.\n", m1_rules, {"M1"}
        ) == [],
    )
    safe_check(
        "M1 negative: token only inside a fence -> not flagged",
        lambda: lint_text("```\nUE\n```\n", m1_rules, {"M1"}) == [],
    )
    safe_check(
        "M1 negative: token only inside an inline code span -> not flagged",
        lambda: lint_text("Use `UE` in code.\n", m1_rules, {"M1"}) == [],
    )
    safe_check(
        "M5 positive (JA form): relative word, no absolute date -> flagged",
        lambda: len(lint_text("来週リリースします。\n", m5_ja_rules, {"M5"})) >= 1,
    )
    safe_check(
        "M5 positive (EN form, capitalized): 'Tomorrow we ship.' -> flagged",
        lambda: len(lint_text("Tomorrow we ship.\n", m5_en_rules, {"M5"})) == 1,
    )
    safe_check(
        "M5 negative: same line carries an absolute date -> not flagged",
        lambda: lint_text("Tomorrow we ship on 2026-08-29.\n", m5_en_rules, {"M5"}) == [],
    )
    safe_check(
        "M5 negative: blockquote line -> not flagged",
        lambda: lint_text("> Tomorrow we ship.\n", m5_en_rules, {"M5"}) == [],
    )

    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)

        # 9: lexicon file itself is exempt.
        def test_lexicon_exempt():
            repo = tmp_path / "t9"
            (repo / "lexicon").mkdir(parents=True)
            lex_file = repo / "lexicon" / "lex.md"
            lex_file.write_text(
                "# lex\n"
                "<!-- misread-lexicon:table -->\n"
                "| Type | Misread form | Clearer form | Source |\n"
                "|---|---|---|---|\n"
                "| M1 | UE | UE (Unreal Engine) | example |\n",
                encoding="utf-8",
            )
            config = {
                "root": str(repo),
                "lexicons": ["lexicon/lex.md"],
                "scan": [{"rules": ["M1"], "include": ["lexicon/**/*.md"], "exclude": []}],
            }
            all_rules, findings, targets_count, lexicon_abs_paths = perform_scan(config, repo)
            return len(all_rules) == 1 and findings == [] and lex_file.resolve() in lexicon_abs_paths

        safe_check("lexicon file itself is exempt from its own scan", test_lexicon_exempt)

        # 10: marker-based parsing works with a non-English header row.
        def test_marker_language_independent():
            lex = tmp_path / "t10"
            lex.mkdir(parents=True, exist_ok=True)
            custom = lex / "custom.md"
            custom.write_text(
                "# Some header\n"
                "<!-- misread-lexicon:table -->\n"
                "| 型 | 誤読される形 | 伝わる形 | 根拠 |\n"
                "|---|---|---|---|\n"
                "| M1 | UE | UE (Unreal Engine) | 例 |\n",
                encoding="utf-8",
            )
            rules = parse_lexicon_table(custom)
            return (
                len(rules) == 1
                and rules[0]["type"] == "M1"
                and rules[0]["forms"] == ["UE"]
                and rules[0]["target"] == "UE (Unreal Engine)"
            )

        safe_check(
            "marker-based parsing works with a non-English header row", test_marker_language_independent
        )

        # 11: unknown rule type M9 does not crash and produces no findings.
        def test_unknown_rule_type():
            rules = [{"type": "M9", "forms": ["something"], "target": "clearer", "source": "example"}]
            findings = lint_text("something appears here.\n", rules, {"M9"})
            return findings == []

        safe_check("unknown rule type M9 does not crash and produces no findings", test_unknown_rule_type)

        # 12: a config with a 'roots' list scans every existing root and unions findings.
        def test_roots_list_scans_every_existing_root():
            base = tmp_path / "t12"
            lex_dir = base / "lexicon"
            lex_dir.mkdir(parents=True)
            (lex_dir / "lex.md").write_text(
                "# lex\n"
                "<!-- misread-lexicon:table -->\n"
                "| Type | Misread form | Clearer form | Source |\n"
                "|---|---|---|---|\n"
                "| M1 | UE | UE (Unreal Engine) | example |\n",
                encoding="utf-8",
            )
            root_a = base / "root_a"
            root_b = base / "root_b"
            (root_a / "rules").mkdir(parents=True)
            (root_b / "rules").mkdir(parents=True)
            (root_a / "rules" / "a.md").write_text("The UE engine.\n", encoding="utf-8")
            (root_b / "rules" / "a.md").write_text("The UE engine too.\n", encoding="utf-8")
            config = {
                "roots": [str(root_a), str(root_b)],
                "lexicons": ["lexicon/lex.md"],
                "scan": [{"rules": ["M1"], "include": ["rules/*.md"], "exclude": []}],
            }
            _all_rules, findings, targets_count, _lex = perform_scan(config, base)
            return targets_count == 2 and len(findings) >= 2

        safe_check(
            "config: roots list scans every existing root", test_roots_list_scans_every_existing_root
        )

        # 13: a declared root that does not exist is skipped, not fatal.
        def test_missing_root_skipped():
            base = tmp_path / "t13"
            lex_dir = base / "lexicon"
            lex_dir.mkdir(parents=True)
            (lex_dir / "lex.md").write_text(
                "# lex\n"
                "<!-- misread-lexicon:table -->\n"
                "| Type | Misread form | Clearer form | Source |\n"
                "|---|---|---|---|\n"
                "| M1 | UE | UE (Unreal Engine) | example |\n",
                encoding="utf-8",
            )
            real_root = base / "root_real"
            (real_root / "rules").mkdir(parents=True)
            (real_root / "rules" / "a.md").write_text("The UE engine.\n", encoding="utf-8")
            missing_root = str(base / "definitely-absent-xyz")
            config = {
                "roots": [str(real_root), missing_root],
                "lexicons": ["lexicon/lex.md"],
                "scan": [{"rules": ["M1"], "include": ["rules/*.md"], "exclude": []}],
            }
            _all_rules, _findings, targets_count, _lex = perform_scan(config, base)
            existing, declared = resolve_roots(config)
            return targets_count == 1 and len(existing) == 1 and declared == 2

        safe_check(
            "config: a declared root that does not exist is skipped, not fatal", test_missing_root_skipped
        )

        # 14: legacy single 'root' key (no 'roots') still works.
        def test_legacy_root_key():
            base = tmp_path / "t14"
            lex_dir = base / "lexicon"
            lex_dir.mkdir(parents=True)
            (lex_dir / "lex.md").write_text(
                "# lex\n"
                "<!-- misread-lexicon:table -->\n"
                "| Type | Misread form | Clearer form | Source |\n"
                "|---|---|---|---|\n"
                "| M1 | UE | UE (Unreal Engine) | example |\n",
                encoding="utf-8",
            )
            root_dir = base / "root_legacy"
            (root_dir / "rules").mkdir(parents=True)
            (root_dir / "rules" / "a.md").write_text("The UE engine.\n", encoding="utf-8")
            config = {
                "root": str(root_dir),
                "lexicons": ["lexicon/lex.md"],
                "scan": [{"rules": ["M1"], "include": ["rules/*.md"], "exclude": []}],
            }
            _all_rules, findings, targets_count, _lex = perform_scan(config, base)
            return targets_count == 1 and len(findings) >= 1

        safe_check("config: legacy single 'root' key still works", test_legacy_root_key)

    all_passed = all(results)
    return 0 if all_passed else 1


if __name__ == "__main__":
    sys.exit(main())
