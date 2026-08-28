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

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8")
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
    # utf-8-sig: decodes BOM-less files identically, and strips the BOM editors
    # like VS Code and PowerShell's Out-File prepend. Without it, a BOM in front
    # of a first-line marker makes this return [] -- a guard that goes silent.
    with open(path, "r", encoding="utf-8-sig") as f:
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
            # resolve() so a root compares equal to the resolve()d file paths it
            # is matched against. Without it, root-anchored scope matching goes
            # silently empty wherever the OS spells the same directory two ways:
            # /var vs /private/var on macOS, 8.3 short names vs long on Windows.
            existing.append(p.resolve())
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


def _glob_to_regex(pattern):
    """Translate a glob (with ** spanning directories) into an anchored regex.

    Used only to test one already-known path against the config's scan blocks
    (the --paths/--hook route). The ordinary sweep still resolves globs with
    Path.glob, so the two routes agree on `**` meaning zero-or-more directories."""
    out = []
    i = 0
    while i < len(pattern):
        if pattern.startswith("**/", i):
            out.append("(?:.*/)?")
            i += 3
        elif pattern.startswith("**", i):
            out.append(".*")
            i += 2
        elif pattern[i] == "*":
            out.append("[^/]*")
            i += 1
        elif pattern[i] == "?":
            out.append("[^/]")
            i += 1
        else:
            out.append(re.escape(pattern[i]))
            i += 1
    flags = re.IGNORECASE if os.name == "nt" else 0
    return re.compile("^" + "".join(out) + "$", flags)


def _pattern_matches(pattern, abs_path, roots):
    """True if pattern selects abs_path.

    A pattern starting with `**/` is location-independent and is matched against
    the whole path, so `**/HANDOFF.md` also selects a HANDOFF.md living outside
    every declared root -- a project checkout you write to but never sweep. Any
    other pattern is root-anchored and only selects files under a declared root,
    matched relative to it, which is what stops `rules/**/*.md` from firing on an
    unrelated repository that happens to have a rules/ directory."""
    rx = _glob_to_regex(pattern)
    if pattern.startswith("**/"):
        return bool(rx.match(abs_path.as_posix()))
    for root in roots:
        try:
            rel = abs_path.relative_to(root)
        except ValueError:
            continue
        if rx.match(rel.as_posix()):
            return True
    return False


def rules_for_path(config, abs_path, roots):
    """Rule types the config's scan blocks assign to one path (empty = out of scope)."""
    allowed = set()
    for block in config.get("scan", []):
        if not any(_pattern_matches(p, abs_path, roots) for p in block.get("include", [])):
            continue
        if any(_pattern_matches(p, abs_path, roots) for p in block.get("exclude", [])):
            continue
        allowed.update(block.get("rules", []))
    return allowed


def load_all_rules(config_dir, lexicon_rel_paths):
    all_rules = []
    lexicon_abs_paths = set()
    for rel in lexicon_rel_paths:
        abs_path = (config_dir / rel).resolve()
        lexicon_abs_paths.add(abs_path)
        all_rules.extend(parse_lexicon_table(abs_path))
    return all_rules, lexicon_abs_paths


def perform_scan(config, config_dir, root_override=None, paths_override=None, rules_override=None,
                 respect_scope=False):
    """Run the full lint pipeline. Returns (all_rules, findings, targets_count,
    lexicon_abs_paths). findings is a list of (path, lineno, type, form, clearer)."""
    config_dir = Path(config_dir)
    lexicon_rel = config.get("lexicons", [])
    all_rules, lexicon_abs_paths = load_all_rules(config_dir, lexicon_rel)

    if paths_override is not None:
        file_rules_map = {}
        if rules_override:
            default_allowed = set(t.strip() for t in rules_override.split(",") if t.strip())
        else:
            default_allowed = set(r["type"] for r in all_rules)
        scope_roots = resolve_roots(config, root_override)[0] if respect_scope else []
        for p in paths_override:
            abs_p = Path(p).resolve()
            if respect_scope:
                allowed = rules_for_path(config, abs_p, scope_roots) & default_allowed
                if not allowed:
                    continue  # out of the configured scope: silently not our file
            else:
                allowed = default_allowed
            file_rules_map[abs_p] = allowed
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
            text = abs_path.read_text(encoding="utf-8-sig")
        except (UnicodeDecodeError, OSError) as exc:
            print(f"[misread-lint] warning: skipping unreadable file {abs_path}: {exc}", file=sys.stderr)
            continue
        for (ln, rtype, form, clearer) in lint_text(text, all_rules, allowed_types):
            findings.append((abs_path, ln, rtype, form, clearer))

    return all_rules, findings, targets_count, lexicon_abs_paths


HOOK_ADVICE = (
    "Fix the source, not the checker: expand the token at first use (M1), or write an "
    "absolute date (M5).\n"
    "If the flagged text is a quotation or a conceptual use, do NOT reword it -- the scope is "
    "wrong, not the sentence. Narrow include/exclude in the config and add the exact line as a "
    "negative control in the tests."
)

HOOK_ADVICE_REPEAT = (
    "This file was already reported and the count has not gone down. Stop rewording it.\n"
    "Either the finding is real and still unfixed, or the rule is right and the SCOPE is wrong: "
    "narrow include/exclude in the config and add the exact line as a negative control in the "
    "tests. This is the last time this file is reported in this session."
)

# Where a tool event keeps the written path. Agents disagree about this, so try
# the known shapes rather than hardcoding one vendor's.
EVENT_PATH_KEYS = (
    ("tool_input", "file_path"),
    ("tool_input", "path"),
    ("tool_input", "notebook_path"),
    ("tool_response", "filePath"),
    ("params", "file_path"),
    ("arguments", "file_path"),
    ("file_path",),
    ("path",),
)

STATE_FILENAME = "misread-lexicon-hook-state.json"
MAX_STRIKES = 2      # consecutive reports without progress before going quiet
MAX_SESSIONS = 8     # the state is an accessory to the check, not an asset


def _dig(payload, keys):
    cur = payload
    for key in keys:
        if not isinstance(cur, dict):
            return None
        cur = cur.get(key)
    return cur if isinstance(cur, str) and cur else None


def parse_event(raw_text):
    """Return (target_path, session_key) from a tool event.

    Accepts a JSON event in any of the shapes in EVENT_PATH_KEYS, or a bare path
    on its own line -- which is what an editor hook or a CI step will send. Returns
    (None, ...) when nothing path-shaped is present, so the caller stays silent."""
    text = (raw_text or "").lstrip(chr(0xFEFF)).strip()
    if not text:
        return None, "-"
    if text.startswith("{"):
        try:
            payload = json.loads(text)
        except ValueError:
            return None, "-"
        if not isinstance(payload, dict):
            return None, "-"
        session = payload.get("session_id") or payload.get("sessionId") or "-"
        for keys in EVENT_PATH_KEYS:
            found = _dig(payload, keys)
            if found:
                return found, str(session)
        return None, str(session)
    if "\n" in text:
        return None, "-"
    return text, "-"


def _read_stdin_text():
    """Read stdin as bytes and decode UTF-8 explicitly.

    Tool events are UTF-8, but sys.stdin decodes with the console codepage (cp932 on
    a Japanese Windows), which mangles a non-ASCII path into one that simply does not
    exist -- a silent zero-finding pass, the worst failure mode a checker has."""
    try:
        return sys.stdin.buffer.read().decode("utf-8", errors="replace")
    except (AttributeError, OSError):
        try:
            return sys.stdin.read() or ""
        except OSError:
            return ""


def _default_state_path():
    return Path(tempfile.gettempdir()) / STATE_FILENAME


def _record_strike(state_file, session, target, count):
    """Remember how often this path was reported without the count going down.

    Returns the strike number for this report (0 = first, or progress since the last
    one). State is disposable: if it cannot be read or written the count stays 0 and
    the check keeps reporting, which is the safe direction to fail in."""
    state = {}
    try:
        state = json.loads(Path(state_file).read_text(encoding="utf-8"))
        if not isinstance(state, dict):
            state = {}
    except (OSError, ValueError):
        state = {}

    seen = state.setdefault(session, {})
    if not isinstance(seen, dict):
        seen = {}
        state[session] = seen
    prev = seen.get(target) if isinstance(seen.get(target), dict) else None

    if count == 0:
        if prev is None:
            return 0  # nothing to clear, nothing to write
        seen.pop(target, None)
        strikes = 0
    elif prev is None or count != prev.get("count", 0):
        # Any change in the count -- down OR up -- means the writer changed the
        # text, so the situation is new: report fresh rather than creep toward
        # silence. Only an identical count is a stall.
        strikes = 0
        seen[target] = {"count": count, "strikes": 0}
    else:
        strikes = int(prev.get("strikes", 0)) + 1
        seen[target] = {"count": count, "strikes": strikes}

    if len(state) > MAX_SESSIONS:
        state = dict(list(state.items())[-MAX_SESSIONS:])
    try:
        Path(state_file).write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
    except OSError:
        pass
    return strikes


def run_hook(config, config_dir, root_override=None, emit="text", raw_event=None, state_path=None):
    """Post-write hook: lint the one file a tool just wrote.

    Reports only, and only for paths the config's scan blocks actually cover -- the
    write has already happened, so there is nothing to block, and a checker that
    refuses writes is one you eventually satisfy by rewording quotations. Always
    exits 0; a non-zero exit here reads as a hook failure rather than a finding.

    emit="text"   -> findings on stderr (any editor, any CI step)
    emit="claude" -> {"decision": "block", "reason": ...} on stdout, the shape a
                     Claude Code PostToolUse hook feeds back to the agent. Named
                     for its consumer: the JSON contract is Claude Code's, and
                     pretending it is generic would be the kind of quiet
                     vendor-assumption this repo exists to flag."""
    if raw_event is None:
        raw_event = _read_stdin_text()
    target, session = parse_event(raw_event)
    if not target:
        return 0

    _rules, findings, _targets, _lex = perform_scan(
        config, config_dir, root_override=root_override, paths_override=[target],
        respect_scope=True,
    )

    state_file = Path(state_path) if state_path else _default_state_path()
    strikes = _record_strike(state_file, session, target, len(findings))
    if not findings or strikes >= MAX_STRIKES:
        return 0

    name = Path(target).name
    lines = ['{}:{}: [{}] "{}" -> {}'.format(name, ln, rtype, form, clearer)
             for (_p, ln, rtype, form, clearer) in findings]
    body = "[misread-lint] {}: {} known-misread form(s) in a file later sessions read.\n{}\n\n{}".format(
        name, len(findings), "\n".join(lines), HOOK_ADVICE if strikes == 0 else HOOK_ADVICE_REPEAT
    )
    if emit == "claude":
        print(json.dumps({"decision": "block", "reason": body}, ensure_ascii=False))
    else:
        print(body, file=sys.stderr)
    return 0


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
    p.add_argument(
        "--respect-scope",
        action="store_true",
        help="With --paths: apply only the rules the config's scan blocks assign to each path, "
             "and skip paths no block covers (default: apply every rule to every given path).",
    )
    p.add_argument(
        "--hook",
        action="store_true",
        help="Post-write mode: read a tool event (or a bare path) on stdin and lint the file "
             "that was written, scope-aware. Reports, never blocks; always exits 0.",
    )
    p.add_argument(
        "--emit",
        choices=["text", "claude"],
        default="text",
        help="With --hook: 'text' writes findings to stderr (any editor or CI step); 'claude' "
             "writes the {\"decision\": \"block\", \"reason\": ...} JSON a Claude Code "
             "PostToolUse hook feeds back to the agent. Default: text.",
    )
    p.add_argument(
        "--state",
        default=None,
        help="With --hook: where to keep the per-session repeat-suppression state "
             "(default: a fixed name in the system temp directory).",
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

    if args.hook:
        return run_hook(config, config_dir, root_override=args.root,
                        emit=args.emit, state_path=args.state)

    all_rules, findings, targets_count, _lexicon_abs_paths = perform_scan(
        config,
        config_dir,
        root_override=args.root,
        paths_override=args.paths,
        rules_override=args.rules,
        respect_scope=args.respect_scope,
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

        # ------------------------------------------------------------------
        # Scope-aware single-path linting (--respect-scope / --hook).
        # ------------------------------------------------------------------

        def _scope_fixture(tag):
            """A config shaped like the documented one: M1 root-anchored on
            instruction docs, M5 location-independent on HANDOFF.md."""
            base = tmp_path / tag
            lex_dir = base / "lexicon"
            lex_dir.mkdir(parents=True)
            (lex_dir / "lex.md").write_text(
                "# lex\n"
                "<!-- misread-lexicon:table -->\n"
                "| Type | Misread form | Clearer form | Source |\n"
                "|---|---|---|---|\n"
                "| M1 | UE | UE (Unreal Engine) | example |\n"
                "| M5 | tomorrow | an absolute date | example |\n",
                encoding="utf-8",
            )
            root = base / "home"
            (root / "skills" / "a").mkdir(parents=True)
            (root / "ledgers").mkdir(parents=True)
            config = {
                "roots": [str(root)],
                "lexicons": ["lexicon/lex.md"],
                "scan": [
                    {"rules": ["M1"], "include": ["skills/**/*.md"], "exclude": ["ledgers/**"]},
                    {"rules": ["M5"], "include": ["**/HANDOFF.md"], "exclude": ["ledgers/**"]},
                ],
            }
            return base, root, config

        # 15: a `**/`-anchored pattern reaches a file outside every declared root.
        def test_location_independent_pattern_reaches_outside_roots():
            base, _root, config = _scope_fixture("t15")
            outside = base / "some_project"
            outside.mkdir(parents=True)
            handoff = outside / "HANDOFF.md"
            handoff.write_text("We ship tomorrow.\n", encoding="utf-8")
            _r, findings, targets, _lex = perform_scan(
                config, base, paths_override=[str(handoff)], respect_scope=True
            )
            return targets == 1 and len(findings) == 1 and findings[0][2] == "M5"

        safe_check(
            "scope: `**/HANDOFF.md` reaches a file outside every declared root",
            test_location_independent_pattern_reaches_outside_roots,
        )

        # 16: a root-anchored pattern does NOT reach a lookalike outside the roots.
        def test_root_anchored_pattern_stays_inside_roots():
            base, _root, config = _scope_fixture("t16")
            outside = base / "other_repo" / "skills" / "a"
            outside.mkdir(parents=True)
            stray = outside / "SKILL.md"
            stray.write_text("The UE engine.\n", encoding="utf-8")
            _r, findings, targets, _lex = perform_scan(
                config, base, paths_override=[str(stray)], respect_scope=True
            )
            return targets == 0 and findings == []

        safe_check(
            "scope: a root-anchored pattern does not reach a lookalike outside the roots",
            test_root_anchored_pattern_stays_inside_roots,
        )

        # 17: scope decides which rules apply -- M5 must not leak onto an M1-only file.
        def test_rules_do_not_leak_across_blocks():
            base, root, config = _scope_fixture("t17")
            skill = root / "skills" / "a" / "SKILL.md"
            skill.write_text("We ship tomorrow.\nThe UE engine.\n", encoding="utf-8")
            _r, findings, _t, _lex = perform_scan(
                config, base, paths_override=[str(skill)], respect_scope=True
            )
            return len(findings) == 1 and findings[0][2] == "M1"

        safe_check(
            "scope: an M1-scoped file is not also checked for M5", test_rules_do_not_leak_across_blocks
        )

        # 18: exclude beats include, even when include matched.
        def test_exclude_beats_include():
            base, root, config = _scope_fixture("t18")
            excluded = root / "ledgers" / "HANDOFF.md"
            excluded.write_text("We ship tomorrow.\n", encoding="utf-8")
            _r, findings, targets, _lex = perform_scan(
                config, base, paths_override=[str(excluded)], respect_scope=True
            )
            return targets == 0 and findings == []

        safe_check("scope: exclude beats include", test_exclude_beats_include)

        # 19: without --respect-scope, --paths keeps applying every rule (unchanged default).
        def test_paths_default_still_applies_all_rules():
            base, _root, config = _scope_fixture("t19")
            loose = base / "anywhere.md"
            loose.write_text("We ship tomorrow.\nThe UE engine.\n", encoding="utf-8")
            _r, findings, _t, _lex = perform_scan(config, base, paths_override=[str(loose)])
            return len(findings) == 2

        safe_check(
            "scope: plain --paths still applies every rule (default unchanged)",
            test_paths_default_still_applies_all_rules,
        )

        # ------------------------------------------------------------------
        # Post-write hook mode (--hook).
        # ------------------------------------------------------------------

        # 20: event parsing accepts the known shapes and rejects non-events.
        def test_parse_event_shapes():
            claude = json.dumps({"session_id": "s1", "tool_input": {"file_path": "/a/b.md"}})
            p1, s1 = parse_event(claude)
            p2, s2 = parse_event("/a/bare path/c.md")
            p3, _ = parse_event(chr(0xFEFF) + claude)          # BOM in front of the JSON
            p4, _ = parse_event("line one\nline two")        # multiline garbage
            p5, _ = parse_event(json.dumps({"session_id": "s2"}))  # JSON, no path
            return (p1 == "/a/b.md" and s1 == "s1"
                    and p2 == "/a/bare path/c.md" and s2 == "-"
                    and p3 == "/a/b.md"
                    and p4 is None and p5 is None)

        safe_check("hook: event parsing (Claude shape, bare path, BOM, garbage)", test_parse_event_shapes)

        # 21: an unchanged finding count escalates to silence; any change resets.
        def test_strike_escalation_and_reset():
            sf = tmp_path / "t21-state.json"
            a = _record_strike(sf, "s", "/f.md", 2)   # first report
            b = _record_strike(sf, "s", "/f.md", 2)   # stalled
            c = _record_strike(sf, "s", "/f.md", 2)   # stalled again -> silent zone
            d = _record_strike(sf, "s", "/f.md", 3)   # count CHANGED (up) -> fresh
            e = _record_strike(sf, "s", "/f.md", 0)   # fixed -> cleared
            f = _record_strike(sf, "s", "/f.md", 1)   # regression -> fresh again
            return (a, b, c, d, e, f) == (0, 1, 2, 0, 0, 0)

        safe_check("hook: strike escalation, reset on change, clear on zero", test_strike_escalation_and_reset)

        # 22: end-to-end -- report, repeat-warning, then silence; emit=claude JSON shape.
        def test_hook_end_to_end():
            import contextlib
            import io as _io
            base, root, config = _scope_fixture("t22")
            skill = root / "skills" / "a" / "SKILL.md"
            skill.write_text("The UE engine.\n", encoding="utf-8")
            sf = tmp_path / "t22-state.json"
            event = json.dumps({"session_id": "s", "tool_input": {"file_path": str(skill)}})

            outs = []
            for _ in range(3):
                buf = _io.StringIO()
                with contextlib.redirect_stdout(buf):
                    rc = run_hook(config, base, emit="claude", raw_event=event, state_path=str(sf))
                if rc != 0:
                    return False
                outs.append(buf.getvalue().strip())

            first = json.loads(outs[0])
            second = json.loads(outs[1])
            return (first.get("decision") == "block" and "[M1]" in first.get("reason", "")
                    and "last time" in second.get("reason", "")
                    and outs[2] == "")

        safe_check("hook: report -> repeat-warning -> silence; emit=claude JSON", test_hook_end_to_end)

        # 23: emit=text goes to stderr, stdout stays empty (safe for non-Claude hosts).
        def test_hook_emit_text_stderr():
            import contextlib
            import io as _io
            base, root, config = _scope_fixture("t23")
            skill = root / "skills" / "a" / "SKILL.md"
            skill.write_text("The UE engine.\n", encoding="utf-8")
            sf = tmp_path / "t23-state.json"
            event = str(skill)  # bare-path form
            out_buf, err_buf = _io.StringIO(), _io.StringIO()
            with contextlib.redirect_stdout(out_buf), contextlib.redirect_stderr(err_buf):
                rc = run_hook(config, base, emit="text", raw_event=event, state_path=str(sf))
            return rc == 0 and out_buf.getvalue() == "" and "[M1]" in err_buf.getvalue()

        safe_check("hook: emit=text writes stderr only, accepts a bare path", test_hook_emit_text_stderr)

        # 24: a UTF-8 BOM silences neither the lexicon nor a scanned file.
        def test_bom_does_not_silence():
            lex_dir = tmp_path / "t24"
            lex_dir.mkdir(parents=True)
            lex = lex_dir / "lex.md"
            # BOM directly in front of a first-line marker -- the nastiest placement.
            lex.write_bytes(
                b"\xef\xbb\xbf<!-- misread-lexicon:table -->\n"
                b"| Type | Misread form | Clearer form | Source |\n"
                b"|---|---|---|---|\n"
                b"| M1 | UE | UE (Unreal Engine) | example |\n"
            )
            rules = parse_lexicon_table(lex)
            target = lex_dir / "doc.md"
            target.write_bytes(b"\xef\xbb\xbfUE engine notes.\n")
            text = target.read_text(encoding="utf-8-sig")
            findings = lint_text(text, rules, {"M1"})
            return len(rules) == 1 and len(findings) == 1

        safe_check("BOM: neither the lexicon nor a scanned file goes silent", test_bom_does_not_silence)

    all_passed = all(results)
    return 0 if all_passed else 1


if __name__ == "__main__":
    sys.exit(main())
