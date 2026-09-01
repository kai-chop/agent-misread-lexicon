#!/usr/bin/env python3
"""Recover misreads from the traces they leave, without anyone having to notice.

mine_misreads.py finds the misreads a human pushed back on. That channel is
blind twice over: it needs the human to have noticed, and it needs somebody to
have said so. A misread that produced plausible-looking work is invisible to it.

This tool reads the other channel. A misread leaves mechanical traces in the
transcript whether or not anybody catches it:

  assert-on-empty   a search returns nothing and the next reply concludes the
                    thing does not exist. The reader's vocabulary missed; the
                    conclusion drawn was about the codebase. (README: M3)

  vocabulary gap    a search returns nothing and a later search - for the same
                    concept, spelled differently - succeeds. The pair names how
                    the two spellings differ: case, dash, space, anchoring.

The second one is the forward-looking half. A gap is not just an incident to
register: its *axis* generalises. Once "en dash vs hyphen" has cost you one
search, `--sweep` lists every other term in your docs exposed on that same axis,
before any of them has cost you anything.

Report tool, not a gate: it never rewrites a file and never blocks. It exits
non-zero only when it inspected nothing, because a checker that scanned zero
items must not report clean.
"""
import argparse
import json
import os
import re
import sys
from pathlib import Path

# Same directory, so this works whether invoked as `tools/trace_misreads.py`
# or from inside tools/. Keeps one definition of "what a human correction
# looks like" and one config lookup across both miners.
from mine_misreads import (  # noqa: E402
    analyze_content,
    classify_candidate,
    load_config,
    strip_envelopes,
    truncate,
)

for _stream in (sys.stdout, sys.stderr):
    # Both: the findings go to stderr, and a console codepage turns a Japanese
    # claim into mojibake - which is M2, in a tool that exists to report M3.
    try:
        _stream.reconfigure(encoding="utf-8")
    except AttributeError:
        pass


SEARCH_TOOLS = ("Grep", "Glob", "Search")
# How far past a zero-hit search a re-spelled query still counts as "the same
# question". Measured on the reference corpus: beyond ~6 searches the next hit
# is usually a different topic entirely.
LOOKAHEAD_SEARCHES = 6
# How many human turns after an absence claim still count as "they caught it".
LOOKAHEAD_HUMAN_MSGS = 3
# An absence claim this early in the reply is the conclusion drawn from the
# empty result; deeper than this it is some other sentence in a long report.
CLAIM_HEAD = 400
MIN_WORD = 3

_EMPTY_RESULT_RE = re.compile(r"no matches found|no files found|found 0 |0 matches|^\s*$", re.I)
_ABSENCE_RE = re.compile(
    r"does\s?n[o']t exist|do(?:es)? not exist|no such |not implemented|there is no |"
    r"isn't (?:any|implemented)|not present in|"
    r"存在しません|存在しない|存在せず|実装されていません|実装されてない|"
    r"見当たりません|定義されていません|該当なし|見つかりませんでした",
    re.I,
)
# "AI癖の除去ではありませんでした" is grammar, not a claim about the codebase.
_GRAMMATICAL_NEG_RE = re.compile(r"では(?:あり|な)")
_WORD_RE = re.compile(r"[A-Za-z0-9_]{%d,}|[ぁ-んァ-ン一-龥]{2,}" % MIN_WORD)
# Only compound terms are swept: a separator is what makes a term a *name*
# (file, config key, function, coined phrase) rather than prose, and a name is
# where a literal search actually misses. Emphasis capitals ("ALWAYS"/"Always")
# have no separator and are dropped here rather than filtered later.
_COMPOUND_RE = re.compile(
    r"[A-Za-z぀-ヿ一-鿿][A-Za-z0-9぀-ヿ一-鿿]*"
    r"(?:[_.‐-―−-][A-Za-z0-9぀-ヿ一-鿿]+)+"
)
_REGEX_META_RE = re.compile(r"[\\^$.|?*+()\[\]{}]")
_DASHES = "‐‑‒–—―−_"
# The sweep is speculative, so it only claims the hazard it can prove: a dash
# you cannot type. hyphen-vs-underscore stays in _DASHES for classifying a pair
# that already cost a search (there it is evidence), but sweeping on it just
# pairs up CLI flags with variable names.
_SWEEP_DASHES = "‐‑‒–—―−"
# Which observed axes are worth sweeping the docs for. Only 'dash' survived
# measurement on the reference corpus: 'case' produced 52 findings that were
# almost entirely heading title-case ("Multi-Agent" vs "Multi-agent"), and
# hyphen-vs-underscore produced 58 pairs that were mostly a CLI flag next to a
# variable of the same name. Both are healthy states, and a checker that fires
# on healthy states is one you learn to ignore. 'dash' measured 2 findings
# across 577 documents, both real: a term spelled with U+2212 that no ASCII
# search will ever reach.
SWEEPABLE_AXES = frozenset({"dash"})


# ---------------------------------------------------------------------------
# Transcript -> ordered event stream
# ---------------------------------------------------------------------------

def _result_text(block):
    content = block.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(b.get("text", "") for b in content if isinstance(b, dict))
    return ""


def events_of(path):
    """Ordered ('search'|'result'|'reply'|'human', id, payload) stream.

    Order is what carries the meaning here - "the reply *after* the empty
    result" - so unlike mine_misreads' per-message scan this keeps the
    tool_use/tool_result pairing intact.
    """
    events = []
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        for raw in f:
            raw = raw.strip()
            if not raw:
                continue
            try:
                record = json.loads(raw)
            except (json.JSONDecodeError, ValueError):
                continue          # malformed lines are skipped, never fatal
            if not isinstance(record, dict) or record.get("type") not in ("user", "assistant"):
                continue
            message = record.get("message")
            if not isinstance(message, dict):
                continue
            content = message.get("content")
            blocks = content if isinstance(content, list) else [{"type": "text", "text": content or ""}]
            _text, has_tool_result, _uses = analyze_content(content)
            rtype = record.get("type")

            for block in blocks:
                if not isinstance(block, dict):
                    continue
                btype = block.get("type")
                if rtype == "assistant" and btype == "tool_use":
                    if block.get("name") in SEARCH_TOOLS:
                        inp = block.get("input") if isinstance(block.get("input"), dict) else {}
                        query = inp.get("pattern") or inp.get("query") or ""
                        events.append(("search", block.get("id"), str(query)))
                elif rtype == "assistant" and btype == "text":
                    events.append(("reply", None, block.get("text") or ""))
                elif btype == "tool_result":
                    events.append(("result", block.get("tool_use_id"), _result_text(block)))
                elif rtype == "user" and btype == "text" and not has_tool_result and not record.get("isMeta"):
                    cleaned = strip_envelopes(block.get("text") or "")
                    if cleaned and not cleaned.startswith("This session is being continued"):
                        events.append(("human", None, cleaned))
    return events


def searches_with_outcome(events):
    """[(query, was_empty), ...] in order, for searches whose result we saw."""
    out, pending = [], {}
    for kind, ident, payload in events:
        if kind == "search":
            pending[ident] = payload
        elif kind == "result":
            query = pending.pop(ident, None)
            if query is not None:
                out.append((query, bool(_EMPTY_RESULT_RE.search(payload.strip()[:400]))))
    return out


# ---------------------------------------------------------------------------
# Signal 1: assert-on-empty
# ---------------------------------------------------------------------------

def find_absence_claim(text):
    """First asserted absence claim in text, or None.

    Skips claims inside 「」/（） (quoted or parenthetical - the speaker is
    reporting a claim, not making one) and grammatical negation.
    """
    for match in _ABSENCE_RE.finditer(text):
        before = text[:match.start()]
        if before.count("「") > before.count("」") or before.count("（") > before.count("）"):
            continue
        if _GRAMMATICAL_NEG_RE.search(text[max(0, match.start() - 6):match.start() + 2]):
            continue
        return match
    return None


def assert_on_empty(events, with_pushback=True):
    """Zero-hit searches whose next reply concluded the thing does not exist.

    with_pushback=False for a session still running: "did a human question it?"
    is decided by looking *after* the claim, and mid-session there is no after.
    The field comes back None rather than False, because "nobody objected yet"
    and "nobody ever objected" are different facts and only one is knowable.
    """
    hits = []
    pending, armed = {}, None
    for index, (kind, ident, payload) in enumerate(events):
        if kind == "search":
            pending[ident] = payload
        elif kind == "result":
            query = pending.pop(ident, None)
            if query is not None and _EMPTY_RESULT_RE.search(payload.strip()[:400]):
                armed = query
        elif kind == "reply" and armed is not None:
            match = find_absence_claim(payload)
            if match is not None:
                start = max(0, match.start() - 80)
                hits.append({
                    "query": armed,
                    "claim": payload[start:match.end() + 80].replace("\n", " "),
                    "tier": 1 if match.start() < CLAIM_HEAD else 2,
                    "caught_by_human": _human_pushed_back(events, index) if with_pushback else None,
                })
                armed = None
            elif len(payload.strip()) > 200:
                armed = None      # the reply moved on to other work
    return hits


def _human_pushed_back(events, from_index):
    """Did a human question this within the next few human turns?"""
    seen = 0
    for kind, _ident, payload in events[from_index + 1:]:
        if kind != "human":
            continue
        if seen >= LOOKAHEAD_HUMAN_MSGS:
            break          # bound before looking, so the window really bounds
        seen += 1
        if classify_candidate(payload) is not None:
            return True
    return False


# ---------------------------------------------------------------------------
# Signal 2: vocabulary gaps, and the axis they generalise along
# ---------------------------------------------------------------------------

def query_words(query):
    """Content words of a search pattern, regex metacharacters dropped."""
    return {w.lower() for w in _WORD_RE.findall(query)}


def _plain(text):
    return _REGEX_META_RE.sub(" ", text).strip()


def _fold_dashes(text):
    for dash in _DASHES:
        text = text.replace(dash, "-")
    return text


def classify_axis(missed, found):
    """How the two spellings of one concept differ.

    Returns 'case' | 'dash' | 'space' | 'anchor' | 'other'. The exact axes are
    tried against every candidate pair before 'anchor', which is a containment
    test and would otherwise swallow the others.
    """
    pairs = [(a, b) for a, b in _candidate_pairs(missed, found) if a != b]
    for name, same in (
        ("case", lambda a, b: a.lower() == b.lower()),
        ("dash", lambda a, b: _fold_dashes(a).lower() == _fold_dashes(b).lower()),
        ("space", lambda a, b: re.sub(r"\s+", "", a).lower() == re.sub(r"\s+", "", b).lower()),
    ):
        if any(same(a, b) for a, b in pairs):
            return name
    if any(a.lower().endswith(b.lower()) or b.lower().endswith(a.lower()) for a, b in pairs):
        return "anchor"
    return "other"


def _candidate_pairs(missed, found):
    """Every way these two patterns could be spellings of the same term.

    A search pattern is usually an alternation, so the difference that matters
    is one branch deep: `rtk` vs `rtk|RTK` differs on case only once you look
    at the branches. Whole patterns first, then branches, then differing words.
    """
    yield _plain(missed), _plain(found)
    for a in _branches(missed):
        for b in _branches(found):
            yield a, b
    # A successful query that spells the same term two ways is a reader
    # hedging against a spelling that already burned them - the axis is inside
    # the winning pattern, not between the two patterns.
    hedged = _branches(found)
    for i, a in enumerate(hedged):
        for b in hedged[i + 1:]:
            yield a, b
    only_missed = query_words(missed) - query_words(found)
    only_found = query_words(found) - query_words(missed)
    for word in sorted(only_missed):
        for peer in sorted(only_found):
            # Only compare words that are plausibly the same term re-spelled.
            if _fold_dashes(word)[:MIN_WORD] == _fold_dashes(peer)[:MIN_WORD] or word in peer or peer in word:
                yield word, peer


def _branches(query):
    return [b for b in (_plain(part).strip() for part in query.split("|")) if b]


def vocabulary_gaps(events):
    """Zero-hit query X followed by a hit on Y, where Y re-spells X."""
    sequence = searches_with_outcome(events)
    gaps = []
    for i, (query, was_empty) in enumerate(sequence):
        if not was_empty:
            continue
        for later_query, later_empty in sequence[i + 1:i + 1 + LOOKAHEAD_SEARCHES]:
            if later_empty or later_query == query:
                continue
            # Shared word: otherwise the next search is a different question.
            if query_words(query) & query_words(later_query):
                gaps.append({
                    "missed": query,
                    "found": later_query,
                    "axis": classify_axis(query, later_query),
                })
                break
    return gaps


# ---------------------------------------------------------------------------
# Hook mode: the same signal, in the session that is producing it
# ---------------------------------------------------------------------------

def session_path_from_event(raw_text):
    """The transcript path out of a hook event, or a bare path on a line."""
    text = (raw_text or "").lstrip("﻿").strip()
    if not text:
        return None
    if text.startswith("{"):
        try:
            payload = json.loads(text)
        except ValueError:
            return None
        if not isinstance(payload, dict):
            return None
        for key in ("transcript_path", "transcriptPath", "transcript"):
            value = payload.get(key)
            if isinstance(value, str) and value:
                return value
        return None
    return text if "\n" not in text else None


def run_session_hook(session_path):
    """Report absence-claims in one live session. Always returns 0.

    Two things differ from the audit, and both are deliberate:

    * a session with no searches is a normal session, not a broken instrument,
      so it does not exit non-zero the way the audit does. What the audit's
      zero-fail protects against - reporting clean without having looked - is
      kept by saying which of the two happened in the alive line;
    * nothing is labelled caught or uncaught, because the human has not had
      their turn yet.
    """
    path = Path(session_path) if session_path else None
    if path is None or not path.is_file():
        # Not a clean bill of health: say the instrument could not look.
        print(f"[trace-misreads] hook: no readable transcript at {session_path!r} - "
              "nothing was inspected, which is not the same as nothing being wrong.",
              file=sys.stderr)
        return [], 0

    events = events_of(path)
    outcomes = searches_with_outcome(events)
    claims = assert_on_empty(events, with_pushback=False)
    print(f"[trace-misreads] hook: session={path.name[:16]} searches={len(outcomes)} "
          f"zero_hit={sum(1 for _q, empty in outcomes if empty)} asserted_absence={len(claims)}")
    for claim in claims:
        print(f'[trace-misreads] searched {claim["query"][:48]!r}, found nothing, '
              f'then concluded: {truncate(claim["claim"], 200)}', file=sys.stderr)
    return claims, 0


# ---------------------------------------------------------------------------
# Gaps -> probes: hand the measured incident to probe_misreads.py
# ---------------------------------------------------------------------------

PROBE_TEMPLATE = """---
id: {id}
type: M3
raw: {raw}
hardened: {hardened}
runs: 3
filler: 40
---

## task

Search the notes/ directory. Does this project have anything about {missed_term}?
Answer with the exact term the notes use for it, or with `no`.

## fixture: notes/service.md

# Service notes

The {{{{term}}}} governs how the batch is admitted. The runbook covers the rest.

## correct

{found_term}

## misread

/^\\s*no\\b/
/not (?:present|mentioned|documented|covered)/
/there is no/
/存在しません|見当たりません/

<!--
Generated by trace_misreads.py --emit-probes, from a search that really failed:

  looked for   {missed}
  found under  {found}
  axis         {axis}
  transcript   {file}

The pair is measured; the question is a template. Rewrite the task in your own
words - the incident this came from asked something specific, and a probe is
only worth its number when it asks what you actually asked.
-->
"""

_TERM_MIN, _TERM_MAX = 4, 40


def _pick_term(pattern, avoid=None):
    """The longest branch of a search pattern that reads like a term."""
    best = ""
    for branch in _branches(pattern):
        candidate = branch.strip().strip("#*/\\")
        if not (_TERM_MIN <= len(candidate) <= _TERM_MAX):
            continue
        if avoid and (avoid in candidate.lower() or candidate.lower() in avoid):
            continue
        if len(candidate) > len(best):
            best = candidate
    return best


def probe_from_gap(gap):
    """Turn one measured gap into a runnable probe. None if it cannot be one."""
    found_term = _pick_term(gap["found"])
    if not found_term:
        return None
    missed_term = _pick_term(gap["missed"], avoid=found_term.lower())
    if not missed_term:
        return None
    slug = re.sub(r"[^A-Za-z0-9]+", "-", found_term).strip("-").lower()[:24]
    if not slug:                     # a non-ASCII term still needs a filename
        slug = f"gap-{abs(hash(found_term)) % 10000:04d}"
    return f"gap-{slug}", PROBE_TEMPLATE.format(
        id=f"gap-{slug}", raw=found_term, hardened=f"{found_term} ({missed_term})",
        found_term=found_term, missed_term=missed_term,
        missed=gap["missed"][:70], found=gap["found"][:70],
        axis=gap["axis"], file=gap.get("file", "?"))


def convertible(gaps):
    """[(id, text, gap)] for the gaps that can become a probe at all."""
    out = []
    for gap in gaps:
        built = probe_from_gap(gap)
        if built is not None:
            out.append((built[0], built[1], gap))
    return out


def emit_probes(gaps, target_dir, only_id=None):
    """Write the chosen probe. Never overwrites. Returns (written, skipped).

    Emitting every convertible gap was measured on the reference corpus and is
    not worth shipping: 12 files, of which 1 was a real pair of names for one
    thing. The pairing sees "two searches near each other"; whether two words
    mean the same thing is not a question string comparison can answer. So the
    tool lists candidates and you name the one you recognise.
    """
    target = Path(target_dir)
    target.mkdir(parents=True, exist_ok=True)
    written, skipped = [], 0
    for name, text, _gap in convertible(gaps):
        if only_id and name != only_id:
            skipped += 1
            continue
        path = target / f"{name}.md"
        if path.exists():            # your edits win over a re-run of the miner
            skipped += 1
            continue
        path.write_text(text, encoding="utf-8")
        written.append(path.name)
    return written, skipped


# ---------------------------------------------------------------------------
# The forward-looking sweep: same axis, terms that have not burned you yet
# ---------------------------------------------------------------------------

def sweep_docs(roots, axes):
    """Terms in the docs exposed on an axis that has already cost a search.

    Every check here needs *two spellings present in your own corpus*, so
    nothing is guessed from a dictionary: if a term is written one way
    everywhere, it is not reported, however unusual it looks.
    """
    files = [p for root in roots for p in Path(root).rglob("*.md") if p.is_file()]
    spellings = {}          # normalised form -> {actual spelling: [files]}
    for path in files:
        try:
            text = path.read_text(encoding="utf-8-sig", errors="replace")
        except OSError:
            continue
        for word in set(_COMPOUND_RE.findall(text)):
            for axis in axes:
                key = _normalise_for(word, axis)
                if key is None:
                    continue
                bucket = spellings.setdefault((axis, key), {})
                bucket.setdefault(word, []).append(path.name)

    findings = []
    for (axis, _key), variants in sorted(spellings.items()):
        if len(variants) < 2:
            continue            # one spelling only - nothing to collide with
        findings.append({
            "axis": axis,
            "spellings": sorted(variants),
            "files": sorted({f for spots in variants.values() for f in spots})[:4],
        })
    return len(files), findings


def _normalise_for(word, axis):
    """Only 'dash' generalises to unburned terms; see SWEEPABLE_AXES."""
    if axis != "dash":
        return None
    folded = word
    for dash in _SWEEP_DASHES:
        folded = folded.replace(dash, "-")
    return folded if folded != word or "-" in word else None


# ---------------------------------------------------------------------------
# Run
# ---------------------------------------------------------------------------

def run_trace(transcripts_dir, roots, out_path=None, do_sweep=False, emit_probes_dir=None,
              gap_id=None):
    """Returns (summary_dict, exit_code). Never writes outside out_path."""
    transcripts_dir = Path(transcripts_dir)
    files = sorted(p for p in transcripts_dir.rglob("*.jsonl") if p.is_file()) \
        if transcripts_dir.exists() else []

    searches = zero_hits = 0
    claims, gaps = [], []
    for path in files:
        events = events_of(path)
        outcomes = searches_with_outcome(events)
        searches += len(outcomes)
        zero_hits += sum(1 for _q, empty in outcomes if empty)
        for hit in assert_on_empty(events):
            hit["file"] = path.name
            claims.append(hit)
        for gap in vocabulary_gaps(events):
            gap["file"] = path.name
            gaps.append(gap)

    uncaught = sum(1 for c in claims if not c["caught_by_human"])
    axes = sorted({g["axis"] for g in gaps} & SWEEPABLE_AXES)
    swept_files, exposures = sweep_docs(roots, axes) if (do_sweep and axes) else (0, [])

    summary = {
        "files": len(files),
        "searches": searches,
        "zero_hits": zero_hits,
        "claims": claims,
        "gaps": gaps,
        "uncaught": uncaught,
        "swept_files": swept_files,
        "exposures": exposures,
    }

    print(f"[trace-misreads] alive: transcripts={len(files)} searches={searches} "
          f"zero_hit={zero_hits}")

    if not files or not searches:
        # Zero inspected items is not a clean bill of health. Say so, loudly.
        print("[trace-misreads] inspected nothing - check --transcripts. "
              "Reporting no findings here would be a false all-clear.", file=sys.stderr)
        return summary, 2

    rate = (len(claims) / zero_hits * 100) if zero_hits else 0.0
    print(f"[trace-misreads] assert-on-empty: {len(claims)}/{zero_hits} ({rate:.1f}%) "
          f"never questioned by a human: {uncaught}")
    for claim in claims:
        print(f'  {claim["file"]}: [tier{claim["tier"]}]'
              f'{"" if claim["caught_by_human"] else " [uncaught]"} '
              f'searched {claim["query"][:48]!r} -> {truncate(claim["claim"], 160)}')

    by_axis = {}
    for gap in gaps:
        by_axis[gap["axis"]] = by_axis.get(gap["axis"], 0) + 1
    print(f"[trace-misreads] vocabulary-gap: {len(gaps)} pair(s) "
          f"axes={{{', '.join(f'{k}:{v}' for k, v in sorted(by_axis.items()))}}}")
    for gap in gaps:
        print(f'  {gap["file"]}: [{gap["axis"]}] looked for {gap["missed"][:48]!r} '
              f'-> found under {gap["found"][:48]!r}')

    if do_sweep:
        print(f"[trace-misreads] sweep: axes={axes or ['-']} docs={swept_files} "
              f"exposed={len(exposures)}")
        for exposure in exposures:
            print(f'  [{exposure["axis"]}] {" / ".join(exposure["spellings"][:4])} '
                  f'({", ".join(exposure["files"])})')

    if emit_probes_dir:
        if gap_id:
            written, skipped = emit_probes(gaps, emit_probes_dir, only_id=gap_id)
            print(f"[trace-misreads] probes: wrote {len(written)} to {emit_probes_dir} "
                  f"(skipped {skipped})")
            if not written:
                print(f"[trace-misreads] no gap called {gap_id!r} - run without --gap to list them",
                      file=sys.stderr)
        else:
            candidates = convertible(gaps)
            print(f"[trace-misreads] convertible gaps: {len(candidates)} of {len(gaps)}. "
                  f"Pick the pair that is two names for one thing, then re-run with --gap ID.")
            for name, _text, gap in candidates:
                print(f'  {name:<34} doc says {_pick_term(gap["found"])!r} / '
                      f'reader looked for {_pick_term(gap["missed"], _pick_term(gap["found"]).lower())!r}')

    if out_path:
        with open(out_path, "w", encoding="utf-8") as f:
            for claim in claims:
                f.write(json.dumps({"kind": "assert-on-empty", **claim}, ensure_ascii=False) + "\n")
            for gap in gaps:
                f.write(json.dumps({"kind": "vocabulary-gap", **gap}, ensure_ascii=False) + "\n")
            for exposure in exposures:
                f.write(json.dumps({"kind": "exposure", **exposure}, ensure_ascii=False) + "\n")

    return summary, 0


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def build_arg_parser():
    p = argparse.ArgumentParser(
        prog="trace_misreads.py",
        description="Recover misreads from their mechanical traces, with no self-report involved.",
    )
    p.add_argument("--transcripts", default=None, help="Directory of *.jsonl transcripts (default: from config).")
    p.add_argument("--root", default=None, help="Override the config's roots for --sweep.")
    p.add_argument("--hook", action="store_true",
                   help="Session mode: read a hook event (or a bare transcript path) on stdin "
                        "and report absence-claims from that one session. Always exits 0.")
    p.add_argument("--session", default=None, metavar="PATH",
                   help="With --hook: read this transcript instead of stdin.")
    p.add_argument("--sweep", action="store_true",
                   help="Also list doc terms exposed on an axis a gap already proved costly.")
    p.add_argument("--emit-probes", default=None, metavar="DIR",
                   help="Where to write a probe built from a measured gap. Without --gap this "
                        "only lists the candidates; whether two words name one thing is your "
                        "call, not a string comparison's.")
    p.add_argument("--gap", default=None, metavar="ID",
                   help="With --emit-probes: the candidate to write. Never overwrites.")
    p.add_argument("--out", default=None, help="Write findings as JSONL here.")
    p.add_argument("--config", default=None, help="Path to a misread-lexicon.json config file.")
    p.add_argument("--self-test", action="store_true", help="Run embedded self-tests and exit.")
    return p


def main(argv=None):
    args = build_arg_parser().parse_args(argv)
    if args.self_test:
        return run_self_tests()

    if args.hook:
        session = args.session
        if session is None:
            # Same stdin decoding as the linter's hook: cp932 would mangle a
            # non-ASCII path into one that does not exist - a silent clean pass.
            from check_misread_words import _read_stdin_text
            session = session_path_from_event(_read_stdin_text())
        _claims, code = run_session_hook(session)
        return code

    config, err = load_config(args.config)
    if args.transcripts:
        transcripts_dir = os.path.expanduser(args.transcripts)
    elif config is not None and config.get("transcripts"):
        transcripts_dir = os.path.expanduser(config["transcripts"])
    else:
        print(f"[trace-misreads] {err or 'no transcripts directory configured.'}", file=sys.stderr)
        return 2

    if args.root:
        roots = [os.path.expanduser(args.root)]
    else:
        roots = [os.path.expanduser(r) for r in (config or {}).get("roots", [])]
    roots = [r for r in roots if Path(r).exists()]

    _summary, code = run_trace(transcripts_dir, roots, out_path=args.out, do_sweep=args.sweep,
                               emit_probes_dir=args.emit_probes, gap_id=args.gap)
    return code


# ---------------------------------------------------------------------------
# Self-test
# ---------------------------------------------------------------------------

def _transcript(records):
    return "\n".join(json.dumps(r, ensure_ascii=False) for r in records) + "\n"


def _search(ident, query):
    return {"type": "assistant", "message": {"content": [
        {"type": "tool_use", "id": ident, "name": "Grep", "input": {"pattern": query}}]}}


def _result(ident, text):
    return {"type": "user", "message": {"content": [
        {"type": "tool_result", "tool_use_id": ident, "content": text}]}}


def _reply(text):
    return {"type": "assistant", "message": {"content": [{"type": "text", "text": text}]}}


def _human(text):
    return {"type": "user", "message": {"content": [{"type": "text", "text": text}]}}


def run_self_tests():
    import tempfile

    results = []

    def record(label, passed):
        print(("PASS: " if passed else "FAIL: ") + label)
        results.append(bool(passed))

    def safe_check(label, fn):
        try:
            record(label, fn())
        except Exception as exc:  # noqa: BLE001 - a self-test must not crash the run
            record(label + f" (raised {exc!r})", False)

    def trace(records, **kw):
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "t.jsonl").write_text(_transcript(records), encoding="utf-8")
            import io
            import contextlib
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
                summary, code = run_trace(tmp, [], **kw)
            return summary, code

    # --- the detector actually detects (a green run must be earned) ---
    def positive_claim():
        summary, code = trace([
            _search("a", "read_observer"),
            _result("a", "No matches found"),
            _reply("そのフックは存在しません。別の方法を提案します。"),
        ])
        return code == 0 and len(summary["claims"]) == 1 and summary["claims"][0]["tier"] == 1

    def positive_claim_en():
        summary, _ = trace([
            _search("a", "retryPolicy"),
            _result("a", "No matches found"),
            _reply("There is no retry policy in this codebase."),
        ])
        return len(summary["claims"]) == 1

    # --- and stays quiet on the healthy shapes ---
    def negative_kept_looking():
        summary, _ = trace([
            _search("a", "read_observer"),
            _result("a", "No matches found"),
            _reply("見つからないので別の綴りで探します。"),
            _search("b", "read_observations"),
            _result("b", "hooks/read_observer.py:12: ..."),
        ])
        return summary["claims"] == []

    def negative_quoted_claim():
        summary, _ = trace([
            _search("a", "rtk"),
            _result("a", "No matches found"),
            _reply("前回私は「存在しない」と書きましたが、確かめ直します。"),
        ])
        return summary["claims"] == []

    def negative_grammatical():
        summary, _ = trace([
            _search("a", "prose_gate"),
            _result("a", "No matches found"),
            _reply("加筆が主で、AI癖の除去ではありませんでした。"),
        ])
        return summary["claims"] == []

    def negative_result_not_empty():
        summary, _ = trace([
            _search("a", "hook"),
            _result("a", "settings.json:4: hooks"),
            _reply("そのフックは存在しません。"),
        ])
        return summary["claims"] == []

    # --- caught vs uncaught, which is the number the whole tool exists for ---
    def human_pushback_is_seen():
        summary, _ = trace([
            _search("a", "PropRide"),
            _result("a", "No matches found"),
            _reply("その仕様は存在しません。"),
            _human("いや、それは誤読。PropRideManager.cs にあるはず"),
        ])
        return len(summary["claims"]) == 1 and summary["claims"][0]["caught_by_human"] \
            and summary["uncaught"] == 0

    def silence_counts_as_uncaught():
        summary, _ = trace([
            _search("a", "PropRide"),
            _result("a", "No matches found"),
            _reply("その仕様は存在しません。"),
            _human("ありがとう、次いこう"),
        ])
        return summary["uncaught"] == 1

    def pushback_outside_the_window_is_not_a_catch():
        # Pushback four human turns later is about something else by then; if
        # this counted, "uncaught" would quietly absorb every late correction.
        summary, _ = trace([
            _search("a", "PropRide"),
            _result("a", "No matches found"),
            _reply("その仕様は存在しません。"),
            _human("ok"), _human("next"), _human("thanks"),
            _human("さっきのは誤読だったね"),
        ])
        return summary["uncaught"] == 1

    # --- vocabulary gaps and their axes ---
    def gap_pairs_and_axis():
        summary, _ = trace([
            _search("a", "Claude–Codex能力差"),
            _result("a", "No matches found"),
            _search("b", "Claude-Codex能力差"),
            _result("b", "ledger.md:3: ..."),
        ])
        return len(summary["gaps"]) == 1 and summary["gaps"][0]["axis"] == "dash"

    def hedged_query_names_the_axis():
        summary, _ = trace([
            _search("a", "能力差|隔週"),
            _result("a", "No matches found"),
            _search("b", "Claude–Codex能力差|Claude-Codex能力差"),
            _result("b", "ledger.md:3: ..."),
        ])
        return len(summary["gaps"]) == 1 and summary["gaps"][0]["axis"] == "dash"

    def gap_needs_a_shared_word():
        summary, _ = trace([
            _search("a", "retryPolicy"),
            _result("a", "No matches found"),
            _search("b", "unrelated_thing"),
            _result("b", "x.md:1: ..."),
        ])
        return summary["gaps"] == []

    def axis_classification():
        cases = [
            ("rtk", "rtk|RTK", "case"),
            ("Claude–Codex", "Claude-Codex", "dash"),
            ('mode="a"', 'mode = "a"', "space"),
            ("tests/test_run.py", "src/tests/test_run.py", "anchor"),
        ]
        return all(classify_axis(a, b) == want for a, b, want in cases)

    # --- a checker that inspected nothing must not report clean ---
    def zero_fail_on_empty_corpus():
        with tempfile.TemporaryDirectory() as tmp:
            import io
            import contextlib
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
                _summary, code = run_trace(tmp, [])
            return code == 2

    def zero_fail_when_no_searches():
        summary, code = trace([_reply("hello"), _human("hi")])
        return code == 2 and summary["searches"] == 0

    # --- hook mode: same signal, live session, different contract ---
    def hook_reports_a_claim_without_labelling_it_caught():
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "s.jsonl"
            path.write_text(_transcript([
                _search("a", "read_observer"),
                _result("a", "No matches found"),
                _reply("そのフックは存在しません。"),
            ]), encoding="utf-8")
            import io
            import contextlib
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
                claims, code = run_session_hook(path)
            return (code == 0 and len(claims) == 1
                    and claims[0]["caught_by_human"] is None)

    def a_quiet_session_is_not_a_failure():
        # The audit exits 2 on zero searches; a session that simply did not
        # search is normal, and a Stop hook that fails on it gets turned off.
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "s.jsonl"
            path.write_text(_transcript([_reply("done"), _human("thanks")]), encoding="utf-8")
            import io
            import contextlib
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
                claims, code = run_session_hook(path)
            return code == 0 and claims == [] and "searches=0" in buf.getvalue()

    def an_unreadable_transcript_says_so():
        # The zero-fail idea survives here as a statement, not an exit code:
        # "inspected nothing" must never print as "nothing wrong".
        import io
        import contextlib
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
            claims, code = run_session_hook("no/such/transcript.jsonl")
        return code == 0 and claims == [] and "nothing was inspected" in buf.getvalue()

    def event_parsing_finds_the_transcript():
        as_json = json.dumps({"session_id": "x", "transcript_path": "/tmp/a.jsonl"})
        return (session_path_from_event(as_json) == "/tmp/a.jsonl"
                and session_path_from_event("  /tmp/b.jsonl  ") == "/tmp/b.jsonl"
                and session_path_from_event("{not json") is None)

    def the_audit_still_labels_caught_and_uncaught():
        # Regression guard: hook mode must not have changed the audit's meaning.
        summary, _ = trace([
            _search("a", "PropRide"),
            _result("a", "No matches found"),
            _reply("その仕様は存在しません。"),
            _human("いや、それは誤読"),
        ])
        return summary["claims"][0]["caught_by_human"] is True

    # --- gaps convert into probes the other tool can actually run ---
    def emitted_probe_loads_in_the_harness():
        from probe_misreads import parse_probe          # local: no import cycle
        built = probe_from_gap({"missed": "rate.limit|throttling",
                                "found": "perimeter-throttle|admission",
                                "axis": "other", "file": "t.jsonl"})
        if built is None:
            return False
        name, text = built
        probe = parse_probe(text, name)
        return (probe["hardened"].startswith(probe["raw"]) and probe["filler"] == 40
                and probe["correct"] and probe["misread"]
                and "<!--" not in " ".join(probe["misread"]))

    def a_gap_with_no_usable_term_is_skipped():
        return probe_from_gap({"missed": "a|b", "found": "c|d", "axis": "other"}) is None

    def emitting_twice_never_overwrites():
        # Your edits to a generated probe survive the next mining run.
        gap = {"missed": "rate.limit", "found": "perimeter-throttle",
               "axis": "other", "file": "t.jsonl"}
        name = probe_from_gap(gap)[0]
        with tempfile.TemporaryDirectory() as tmp:
            first, _ = emit_probes([gap], tmp, only_id=name)
            marker = Path(tmp) / first[0]
            marker.write_text("edited by hand", encoding="utf-8")
            second, skipped = emit_probes([gap], tmp, only_id=name)
            return (len(first) == 1 and second == [] and skipped == 1
                    and marker.read_text(encoding="utf-8") == "edited by hand")

    def emitting_writes_nothing_without_a_choice():
        # Measured 1 real pair in 12 convertible ones, so writing them all is
        # the noise this repo argues against. Nothing lands unless you pick.
        gaps = [{"missed": "rate.limit", "found": "perimeter-throttle",
                 "axis": "other", "file": "t.jsonl"},
                {"missed": "queue.depth", "found": "backlog-meter",
                 "axis": "other", "file": "t.jsonl"}]
        with tempfile.TemporaryDirectory() as tmp:
            written, _skipped = emit_probes(gaps, tmp, only_id="gap-backlog-meter")
            return written == ["gap-backlog-meter.md"] and len(convertible(gaps)) == 2

    # --- the sweep only reports a collision it can see both halves of ---
    def sweep_needs_two_spellings():
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "a.md").write_text("Claude–Codex能力差 の記録", encoding="utf-8")
            (root / "b.md").write_text("Claude-Codex能力差 の参照", encoding="utf-8")
            (root / "c.md").write_text("only-one-spelling here", encoding="utf-8")
            _n, exposures = sweep_docs([root], ["dash"])
            axes_found = [e for e in exposures if e["axis"] == "dash"]
            return len(axes_found) == 1 and len(axes_found[0]["spellings"]) == 2

    def sweep_reports_only_the_untypeable_dash():
        # The whole sweep rests on this line: a term you cannot type is a
        # search that cannot succeed. hyphen-vs-underscore is not that.
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "a.md").write_text("the A−C threshold", encoding="utf-8")
            (root / "b.md").write_text("the A-C threshold", encoding="utf-8")
            (root / "c.md").write_text("agent-name and agent_name", encoding="utf-8")
            _n, exposures = sweep_docs([root], ["dash"])
            return len(exposures) == 1 and "A−C" in exposures[0]["spellings"]

    def case_axis_is_not_sweepable():
        # Measured at 52 findings, nearly all heading title-case. Pinned here
        # so re-enabling it has to be a deliberate edit, not a drift.
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "a.md").write_text("Multi-Agent design", encoding="utf-8")
            (root / "b.md").write_text("Multi-agent design", encoding="utf-8")
            _n, exposures = sweep_docs([root], ["case"])
            return exposures == [] and "case" not in SWEEPABLE_AXES

    def sweep_is_silent_without_an_observed_axis():
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "a.md").write_text("Claude–Codex / Claude-Codex", encoding="utf-8")
            _n, exposures = sweep_docs([Path(tmp)], [])
            return exposures == []

    safe_check("detects an absence claim after an empty search (ja)", positive_claim)
    safe_check("detects an absence claim after an empty search (en)", positive_claim_en)
    safe_check("silent when the reply kept looking", negative_kept_looking)
    safe_check("silent on a quoted absence claim", negative_quoted_claim)
    safe_check("silent on grammatical negation", negative_grammatical)
    safe_check("silent when the search was not empty", negative_result_not_empty)
    safe_check("human pushback marks the claim caught", human_pushback_is_seen)
    safe_check("silence marks the claim uncaught", silence_counts_as_uncaught)
    safe_check("pushback outside the window is not a catch", pushback_outside_the_window_is_not_a_catch)
    safe_check("pairs a re-spelled query and names the axis", gap_pairs_and_axis)
    safe_check("a hedged winning query names the axis", hedged_query_names_the_axis)
    safe_check("no gap without a shared word", gap_needs_a_shared_word)
    safe_check("axis classification: case/dash/space/anchor", axis_classification)
    safe_check("exit 2 on an empty corpus", zero_fail_on_empty_corpus)
    safe_check("exit 2 when no search was inspected", zero_fail_when_no_searches)
    safe_check("sweep reports only a two-spelling collision", sweep_needs_two_spellings)
    safe_check("hook reports a claim without labelling it caught", hook_reports_a_claim_without_labelling_it_caught)
    safe_check("a quiet session is not a hook failure", a_quiet_session_is_not_a_failure)
    safe_check("an unreadable transcript says so", an_unreadable_transcript_says_so)
    safe_check("the hook event yields the transcript path", event_parsing_finds_the_transcript)
    safe_check("the audit still labels caught and uncaught", the_audit_still_labels_caught_and_uncaught)
    safe_check("an emitted probe loads in the harness", emitted_probe_loads_in_the_harness)
    safe_check("a gap with no usable term is skipped", a_gap_with_no_usable_term_is_skipped)
    safe_check("emitting twice never overwrites your edits", emitting_twice_never_overwrites)
    safe_check("nothing is emitted until you choose", emitting_writes_nothing_without_a_choice)
    safe_check("sweep reports only the untypeable dash", sweep_reports_only_the_untypeable_dash)
    safe_check("case axis stays out of the sweep", case_axis_is_not_sweepable)
    safe_check("sweep is silent with no observed axis", sweep_is_silent_without_an_observed_axis)

    print(f"[trace-misreads] self-test: {sum(results)}/{len(results)} passed")
    return 0 if all(results) else 1


if __name__ == "__main__":
    sys.exit(main())
