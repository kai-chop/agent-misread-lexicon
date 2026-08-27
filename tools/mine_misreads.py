#!/usr/bin/env python3
"""Mine past agent transcripts for evidence of misreads.

This is a report tool, not a gate: it walks a directory of *.jsonl agent
transcripts, finds user messages that read like a correction ("you misread
that", "that's not what I said", etc.), and prints/writes them so a human can
seed the misread lexicon from history instead of only growing it forward.
"""
import argparse
import json
import os
import re
import sys
import tempfile
from collections import deque
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8")
except AttributeError:
    pass


MAX_RECENT_TOOL_USES = 8
TRUNCATE_LEN = 700

_ENVELOPE_TAG_RE = re.compile(
    r"<(system-reminder|command-name|command-message|command-args|command-contents|"
    r"local-command-stdout|local-command-stderr)\b[^>]*>.*?</\1>",
    re.IGNORECASE | re.DOTALL,
)

_TIER1_JA = (
    r"誤読|読み間違|読み違|読み飛ば|勘違い|誤解|取り違|意味が違|"
    r"そういう意味|ちゃんと読|よく読|読み直|解釈が違"
)
_TIER1_EN = (
    r"\bmisread|\bmisunderstood|\bmisinterpret|that's not what .{0,20}(mean|said)|"
    r"read it again|you didn't read|not what i (meant|said)"
)
_TIER1_RE = re.compile(_TIER1_JA + "|" + _TIER1_EN, re.IGNORECASE)

_TIER2_JA = r"そうじゃな|そうではな|違います|書いてあ|と書いた|見落と|読んでない"
_TIER2_EN = r"\bno,? i (meant|said)\b|it says\b|as written\b|you missed\b"
_TIER2_RE = re.compile(_TIER2_JA + "|" + _TIER2_EN, re.IGNORECASE)

_COMPACTION_PREFIX = "This session is being continued"


# ---------------------------------------------------------------------------
# Record / content parsing
# ---------------------------------------------------------------------------

def strip_envelopes(text):
    if not text:
        return text
    return _ENVELOPE_TAG_RE.sub("", text).strip()


def analyze_content(content):
    """Return (joined_text, has_tool_result, tool_uses) for a message.content
    value, which may be a plain string or a list of typed blocks."""
    text_parts = []
    has_tool_result = False
    tool_uses = []

    if isinstance(content, str):
        text_parts.append(content)
    elif isinstance(content, list):
        for block in content:
            if not isinstance(block, dict):
                continue
            btype = block.get("type")
            if btype == "text":
                t = block.get("text")
                if isinstance(t, str):
                    text_parts.append(t)
            elif btype == "tool_use":
                inp = block.get("input") or {}
                if not isinstance(inp, dict):
                    inp = {}
                target = inp.get("file_path") or inp.get("path") or inp.get("pattern")
                tool_uses.append((block.get("name"), target))
            elif btype == "tool_result":
                has_tool_result = True

    return "\n".join(text_parts), has_tool_result, tool_uses


def classify_candidate(text):
    """Return 1, 2, or None depending on which correction-pattern tier text
    matches (tier 1 checked first, since it names a reading error explicitly)."""
    if _TIER1_RE.search(text):
        return 1
    if _TIER2_RE.search(text):
        return 2
    return None


def truncate(text, limit=TRUNCATE_LEN):
    if text is None:
        return ""
    if len(text) <= limit:
        return text
    return text[:limit]


# ---------------------------------------------------------------------------
# Per-file scan
# ---------------------------------------------------------------------------

def scan_transcript_file(path):
    """Scan one *.jsonl transcript file. Returns a dict with 'candidates'
    (list of dicts) and per-file stats. Malformed JSON lines never crash
    the scan; they are simply counted."""
    path = Path(path)
    stats = {
        "lines_total": 0,
        "malformed": 0,
        "user_seen": 0,
        "tier1": 0,
        "tier2": 0,
        "dropped_compaction": 0,
    }
    candidates = []

    last_assistant_text = ""
    recent_tool_uses = deque(maxlen=MAX_RECENT_TOOL_USES)

    with open(path, "r", encoding="utf-8", errors="replace") as f:
        for lineno, raw_line in enumerate(f, start=1):
            line = raw_line.strip()
            if not line:
                continue
            stats["lines_total"] += 1
            try:
                record = json.loads(line)
            except (json.JSONDecodeError, ValueError):
                stats["malformed"] += 1
                continue
            if not isinstance(record, dict):
                stats["malformed"] += 1
                continue

            rtype = record.get("type")
            if rtype not in ("user", "assistant"):
                continue

            message = record.get("message")
            if not isinstance(message, dict):
                continue
            content = message.get("content")
            text, has_tool_result, tool_uses = analyze_content(content)

            if rtype == "assistant":
                if text.strip():
                    last_assistant_text = text
                for name, target in tool_uses:
                    if target is not None:
                        recent_tool_uses.append((name, target))
                continue

            # rtype == "user"
            if record.get("isMeta"):
                continue
            if has_tool_result:
                continue  # a tool result fed back as a "user" turn is not human speech

            cleaned = strip_envelopes(text)
            if not cleaned:
                continue
            stats["user_seen"] += 1

            if cleaned.startswith(_COMPACTION_PREFIX):
                stats["dropped_compaction"] += 1
                continue

            tier = classify_candidate(cleaned)
            if tier is None:
                continue
            if tier == 1:
                stats["tier1"] += 1
            else:
                stats["tier2"] += 1

            candidates.append({
                "tier": tier,
                "file": path.name,
                "timestamp": record.get("timestamp"),
                "lineno": lineno,
                "user_text": truncate(cleaned),
                "assistant_tail": truncate(last_assistant_text),
                "recent_targets": list(recent_tool_uses),
            })

    return {"candidates": candidates, "stats": stats}


def merge_stats(a, b):
    for k in a:
        a[k] += b.get(k, 0)
    return a


# ---------------------------------------------------------------------------
# Config resolution (mirrors check_misread_words.py's default lookup)
# ---------------------------------------------------------------------------

def resolve_default_config_path():
    cwd_config = Path.cwd() / "misread-lexicon.json"
    if cwd_config.exists():
        return cwd_config.resolve()
    repo_root = Path(__file__).resolve().parent.parent
    example_config = repo_root / "misread-lexicon.example.json"
    if example_config.exists():
        return example_config.resolve()
    return None


def load_config(explicit_path):
    if explicit_path:
        p = Path(explicit_path)
        if not p.exists():
            return None, f"config not found: {p}"
    else:
        p = resolve_default_config_path()
        if p is None:
            return None, (
                "no config found. Looked for 'misread-lexicon.json' in the current "
                "directory and 'misread-lexicon.example.json' next to this tool's repo "
                "root. Copy misread-lexicon.example.json to misread-lexicon.json and "
                "edit it, or pass --config PATH."
            )
    try:
        config = json.loads(p.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        return None, f"invalid JSON in config {p}: {exc}"
    return config, None


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def build_arg_parser():
    p = argparse.ArgumentParser(
        prog="mine_misreads.py",
        description="Mine past agent transcripts for evidence of misreads.",
    )
    p.add_argument("--transcripts", default=None, help="Directory of *.jsonl transcripts (default: from config).")
    p.add_argument("--out", default=None, help="Write a JSONL report here (default: print a summary only).")
    p.add_argument("--config", default=None, help="Path to a misread-lexicon.json config file.")
    p.add_argument("--self-test", action="store_true", help="Run embedded self-tests and exit.")
    return p


def run_mine(transcripts_dir, out_path):
    transcripts_dir = Path(transcripts_dir)
    total_stats = {
        "lines_total": 0,
        "malformed": 0,
        "user_seen": 0,
        "tier1": 0,
        "tier2": 0,
        "dropped_compaction": 0,
    }
    all_candidates = []
    files_scanned = 0

    if transcripts_dir.exists():
        for jsonl_path in sorted(transcripts_dir.rglob("*.jsonl")):
            if not jsonl_path.is_file():
                continue
            files_scanned += 1
            result = scan_transcript_file(jsonl_path)
            all_candidates.extend(result["candidates"])
            merge_stats(total_stats, result["stats"])

    if out_path:
        with open(out_path, "w", encoding="utf-8") as f:
            for candidate in all_candidates:
                f.write(json.dumps(candidate, ensure_ascii=False) + "\n")

    print(
        "[mine-misreads] files={files} malformed_lines={malformed} "
        "user_messages={user_seen} tier1={tier1} tier2={tier2} "
        "dropped_compaction={dropped_compaction}".format(files=files_scanned, **total_stats)
    )

    return all_candidates, total_stats


def main(argv=None):
    args = build_arg_parser().parse_args(argv)

    if args.self_test:
        return run_self_tests()

    config, err = load_config(args.config)
    if args.transcripts:
        transcripts_dir = os.path.expanduser(args.transcripts)
    elif config is not None and config.get("transcripts"):
        transcripts_dir = os.path.expanduser(config["transcripts"])
    else:
        if err:
            print(f"[mine-misreads] {err}", file=sys.stderr)
        else:
            print("[mine-misreads] no --transcripts given and config has no 'transcripts' key.", file=sys.stderr)
        return 2

    run_mine(transcripts_dir, args.out)
    return 0  # report tool, not a gate: always exits 0


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

    def user_record(text, timestamp="2026-08-01T00:00:00Z", is_meta=False):
        rec = {
            "type": "user",
            "message": {"role": "user", "content": text},
            "timestamp": timestamp,
        }
        if is_meta:
            rec["isMeta"] = True
        return json.dumps(rec, ensure_ascii=False)

    def tool_result_user_record(text):
        return json.dumps(
            {
                "type": "user",
                "message": {"role": "user", "content": [{"type": "tool_result", "content": text}]},
                "timestamp": "2026-08-01T00:00:00Z",
            },
            ensure_ascii=False,
        )

    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)

        # 1: Japanese tier-1 message is captured.
        def test_ja_tier1():
            f = tmp_path / "ja_tier1.jsonl"
            f.write_text(user_record("それは誤読です、読み直してください。") + "\n", encoding="utf-8")
            result = scan_transcript_file(f)
            return result["stats"]["tier1"] == 1 and len(result["candidates"]) == 1

        safe_check("a Japanese tier-1 message is captured", test_ja_tier1)

        # 2: English tier-1 message is captured.
        def test_en_tier1():
            f = tmp_path / "en_tier1.jsonl"
            f.write_text(user_record("You misread that.") + "\n", encoding="utf-8")
            result = scan_transcript_file(f)
            return result["stats"]["tier1"] == 1 and len(result["candidates"]) == 1

        safe_check("an English tier-1 message ('You misread that.') is captured", test_en_tier1)

        # 3: compaction record is dropped and counted.
        def test_compaction_dropped():
            f = tmp_path / "compaction.jsonl"
            f.write_text(
                user_record("This session is being continued from a previous conversation.") + "\n",
                encoding="utf-8",
            )
            result = scan_transcript_file(f)
            return (
                result["stats"]["dropped_compaction"] == 1
                and result["stats"]["tier1"] == 0
                and result["stats"]["tier2"] == 0
                and len(result["candidates"]) == 0
            )

        safe_check(
            "a 'This session is being continued...' record is dropped and counted",
            test_compaction_dropped,
        )

        # 4: tool_result message is not treated as user speech.
        def test_tool_result_excluded():
            f = tmp_path / "tool_result.jsonl"
            f.write_text(tool_result_user_record("誤読 evidence embedded in tool output") + "\n", encoding="utf-8")
            result = scan_transcript_file(f)
            return result["stats"]["user_seen"] == 0 and len(result["candidates"]) == 0

        safe_check("a tool_result message is not treated as user speech", test_tool_result_excluded)

        # 5: malformed JSON line is counted, not fatal.
        def test_malformed_line():
            f = tmp_path / "malformed.jsonl"
            f.write_text(
                "{this is not valid json\n" + user_record("You misread that.") + "\n",
                encoding="utf-8",
            )
            result = scan_transcript_file(f)
            return result["stats"]["malformed"] == 1 and result["stats"]["tier1"] == 1

        safe_check("a malformed JSON line is counted, not fatal", test_malformed_line)

        # 6: isMeta record is skipped.
        def test_is_meta_skipped():
            f = tmp_path / "is_meta.jsonl"
            f.write_text(user_record("You misread that.", is_meta=True) + "\n", encoding="utf-8")
            result = scan_transcript_file(f)
            return (
                result["stats"]["user_seen"] == 0
                and result["stats"]["tier1"] == 0
                and len(result["candidates"]) == 0
            )

        safe_check("a record with isMeta: true is skipped", test_is_meta_skipped)

    all_passed = all(results)
    return 0 if all_passed else 1


if __name__ == "__main__":
    sys.exit(main())
