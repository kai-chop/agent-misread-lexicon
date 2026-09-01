#!/usr/bin/env python3
"""Measure the misread rate by running probes, not by asking anyone.

trace_misreads.py recovers misreads that already happened. This one asks a
different question: does rewriting the word actually help, and by how much?

A probe is one task with one word in it, written twice - `raw:` is the spelling
that gets misread, `hardened:` is the spelling from your lexicon's third
column. Everything else about the two runs is identical, so the model's mood,
the day, and the phase of the moon land on both arms equally and cancel:

    misread rate (raw arm)  -  misread rate (hardened arm)  =  what the row bought you

Nothing here is self-reported. Grading looks only at the artifact the agent
produced - which file it named, which id it answered - so an agent that stops
admitting its mistakes changes nothing about the number. Disclosure is measured
too, but as a *separate* number, conditioned on the trials the artifact already
proved wrong. That is what tells the two failure modes apart:

    misread down, disclosure steady  ->  it genuinely reads better
    misread steady, disclosure down  ->  it just went quiet

Bring your own agent: `--runner 'claude -p {prompt}'`. The harness writes the
fixture into a scratch directory, runs that command there, and grades stdout.
"""
import argparse
import json
import os
import re
import shlex
import subprocess
import sys
import tempfile
from datetime import date, timedelta
from pathlib import Path

# Same directory; keeps one lexicon parser across the linter and the harness.
from check_misread_words import parse_lexicon_table  # noqa: E402

try:
    sys.stdout.reconfigure(encoding="utf-8")
except AttributeError:
    pass


TERM_PLACEHOLDER = "{{term}}"
ARMS = ("raw", "hardened")
DEFAULT_RUNS = 2
DEFAULT_TIMEOUT = 180

_FRONT_MATTER_RE = re.compile(r"\A---\r?\n(.*?)\r?\n---\r?\n", re.S)
_SECTION_RE = re.compile(r"^##[ \t]+(.+?)[ \t]*$", re.M)
_REGEX_LINE_RE = re.compile(r"\A/(.*)/\Z")
_HTML_COMMENT_RE = re.compile(r"<!--.*?-->", re.S)
# Said out loud by the agent on a trial the artifact already shows is wrong.
_DISCLOSURE_RE = re.compile(
    r"not (?:certain|sure)|unsure|i (?:may|might) (?:have )?(?:be wrong|misread)|"
    r"ambiguous|unclear which|assuming (?:you|that)|if i understood|could refer to|"
    r"誤読|読み違|どちらか判断|曖昧|確信が(?:持て|ありま)|かもしれません|判別できません",
    re.I,
)


# ---------------------------------------------------------------------------
# Probe files
# ---------------------------------------------------------------------------

def parse_probe(text, name="<probe>"):
    """Parse one probe file. Raises ValueError with a usable message.

    Shape: front matter (id/type/raw/hardened/runs), then `## task`,
    any number of `## fixture: PATH`, `## correct`, `## misread`.
    """
    match = _FRONT_MATTER_RE.match(text)
    if not match:
        raise ValueError(f"{name}: no --- front matter at the top")
    meta = {}
    for line in match.group(1).splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        key, sep, value = line.partition(":")
        if not sep:
            raise ValueError(f"{name}: front matter line is not 'key: value': {line!r}")
        meta[key.strip()] = value.strip()

    body = text[match.end():]
    sections, bounds = {}, [(m.group(1), m.start(), m.end()) for m in _SECTION_RE.finditer(body)]
    for i, (title, _start, end) in enumerate(bounds):
        stop = bounds[i + 1][1] if i + 1 < len(bounds) else len(body)
        sections[title] = body[end:stop].strip("\r\n")

    probe = {
        "id": meta.get("id") or Path(name).stem,
        "type": meta.get("type", ""),
        "raw": meta.get("raw", ""),
        "hardened": meta.get("hardened", ""),
        "runs": int(meta.get("runs", DEFAULT_RUNS)),
        "filler": int(meta.get("filler", 0)),
        "source": name,
        "task": (sections.get("task") or "").strip(),
        "fixtures": {t.split(":", 1)[1].strip(): b for t, b in sections.items()
                     if t.lower().startswith("fixture:")},
        "correct": _patterns(sections.get("correct", "")),
        "misread": _patterns(sections.get("misread", "")),
    }
    if not probe["task"]:
        raise ValueError(f"{name}: no '## task' section")
    if not probe["raw"] or not probe["hardened"]:
        raise ValueError(f"{name}: front matter needs both 'raw:' and 'hardened:'")
    if not probe["correct"]:
        # Without a positive discriminator the probe cannot tell a right answer
        # from a crash, and would score both the same. Refuse it.
        raise ValueError(f"{name}: no '## correct' patterns - the probe cannot discriminate")
    return probe


def _patterns(block):
    # A trailing <!-- note --> belongs to the reader, not to the grader. Without
    # this every line of the comment loads as a match pattern.
    block = _HTML_COMMENT_RE.sub("", block)
    return [line.strip() for line in block.splitlines() if line.strip()]


def load_probes(probes_dir):
    """Returns (probes, errors). A malformed probe is an error, never a skip."""
    probes, errors = [], []
    for path in sorted(Path(probes_dir).glob("*.md")):
        try:
            probes.append(parse_probe(path.read_text(encoding="utf-8-sig"), path.name))
        except (ValueError, OSError) as exc:
            errors.append(str(exc))
    return probes, errors


# ---------------------------------------------------------------------------
# Probes derived from the lexicon, so a fresh registry still measures something
# ---------------------------------------------------------------------------

def auto_probes(rules, today=None):
    """Build probes straight from lexicon rows. Returns (probes, skipped)."""
    today = today or date.today()
    probes, skipped = [], []
    for rule in rules:
        for form in rule["forms"]:
            if rule["type"] == "M1":
                probe = _auto_m1(form, rule["target"])
            elif rule["type"] == "M5":
                probe = _auto_m5(form, today)
            else:
                probe = None
            if probe is None:
                skipped.append(f'{rule["type"]}:{form}')
            else:
                probes.append(probe)
    return probes, skipped


def _clearer_form(target):
    """The replacement spelling out of a third column that also carries advice:
    'UE (Unreal Engine) - expand at first use' -> 'UE (Unreal Engine)'."""
    for separator in ("—", " - ", " – "):
        target = target.split(separator)[0]
    return target.strip()


def _auto_m1(form, target):
    """Expansion recall: does the reader resolve the token to the registered
    referent? One-sided by construction - we know the right answer, but not
    which wrong thing a given reader will invent - so it has no `misread`
    patterns and a non-match is scored as a miss. A hand-written probe with a
    decoy is strictly better; this is what a bare registry can still measure."""
    clearer = _clearer_form(target)
    inner = re.search(r"[（(]([^）)]+)[）)]", clearer)
    if not inner:
        return None            # no expansion to check against
    return {
        "id": f"auto-m1-{form}",
        "type": "M1", "raw": form, "hardened": clearer, "runs": DEFAULT_RUNS,
        "source": "<auto from lexicon>",
        "task": f"Read notes.md. What does {TERM_PLACEHOLDER} refer to there? "
                "Answer with the name only, no sentence.",
        "fixtures": {"notes.md": f"Finish the {TERM_PLACEHOLDER} setup steps before the run."},
        "correct": [inner.group(1).strip()],
        "misread": [],
        "one_sided": True,
    }


def _auto_m5(form, today):
    """Which entry does a relative reference point at - the one that is relative
    to the document's own date, or to the day the reader happens to run?"""
    if not re.search(r"yesterday|昨日", form, re.I):
        return None            # only the one-day-back forms have a clean answer
    doc_date = today - timedelta(days=30)
    return {
        "id": f"auto-m5-{form}",
        "type": "M5", "raw": form, "hardened": (doc_date - timedelta(days=1)).isoformat(),
        "runs": DEFAULT_RUNS,
        "source": "<auto from lexicon>",
        "task": "Read note.md and decisions.md. Which decision does the note refer to? "
                "Answer with the id only (D-A or D-B).",
        "fixtures": {
            "note.md": f"Written {doc_date.isoformat()}.\n\n"
                       f"The {TERM_PLACEHOLDER} decision stands.",
            "decisions.md": f"D-A  {(doc_date - timedelta(days=1)).isoformat()}  raise the timeout\n"
                            f"D-B  {(today - timedelta(days=1)).isoformat()}  drop the cache",
        },
        "correct": ["D-A"],
        "misread": ["D-B"],
    }


# ---------------------------------------------------------------------------
# Running one trial
# ---------------------------------------------------------------------------

def split_runner(template):
    """Split a runner template into argv without a shell.

    posix=False so a Windows path's backslashes survive; quotes are stripped
    afterwards, which is what a quoted "C:\\Program Files\\..." needs.
    """
    tokens = shlex.split(template, posix=False)
    out = []
    for token in tokens:
        if len(token) >= 2 and token[0] == token[-1] and token[0] in "\"'":
            token = token[1:-1]
        out.append(token)
    return out


_FILLER_TOPICS = ("cache warmup", "log rotation", "deploy window", "retry budget",
                  "schema drift", "index rebuild", "token refresh", "queue drain",
                  "shard rebalance", "cert renewal", "backfill job", "alert routing")
_FILLER_BODIES = ("Owned by the platform team. Reviewed each quarter.",
                  "Runbook sits next to the service. No open questions.",
                  "Superseded by the entry above; kept for the history.",
                  "Draft - the numbers are placeholders until the next review.")


def filler_files(count, avoid):
    """Plausible notes that surround the real one.

    A misread happens because searching is cheaper than reading everything. A
    fixture of one small file removes that pressure - the agent just reads it
    all and the mechanism never fires. Filler restores the pressure, so the
    probe tests what the real session tested. Nothing in it contains either
    spelling, or the probe would answer itself.
    """
    out, index, ceiling = {}, 0, count * 4 + 100
    while len(out) < count and index < ceiling:
        topic = _FILLER_TOPICS[index % len(_FILLER_TOPICS)]
        body = _FILLER_BODIES[(index // len(_FILLER_TOPICS)) % len(_FILLER_BODIES)]
        text = f"# {topic} {index:02d}\n\n{body}\n"
        index += 1
        if any(term and term.lower() in text.lower() for term in avoid):
            continue
        out[f"notes/note-{len(out):02d}.md"] = text
    return out


def materialise(probe, arm, target_dir):
    """Write the probe's fixture for one arm. Returns the task prompt."""
    spelling = probe[arm]
    target_dir = Path(target_dir)
    files = dict(filler_files(probe.get("filler", 0), (probe["raw"], probe["hardened"])))
    files.update(probe["fixtures"])
    for rel, body in files.items():
        path = target_dir / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body.replace(TERM_PLACEHOLDER, spelling) + "\n", encoding="utf-8")
    return probe["task"].replace(TERM_PLACEHOLDER, spelling)


def isolated_env(work_dir):
    """An environment whose home is the scratch directory.

    Without this the agent under test loads *your* instruction files - the same
    ones carrying the lexicon being measured - and the raw arm gets to look the
    word up. Observed on the first real run: an agent resolved a bare token and
    said so, citing the tester's own registry.
    """
    env = dict(os.environ)
    home = Path(work_dir) / ".home"
    home.mkdir(parents=True, exist_ok=True)
    env.update({"HOME": str(home), "USERPROFILE": str(home),
                "CLAUDE_CONFIG_DIR": str(home / ".claude")})
    return env


def run_trial(runner, prompt, work_dir, timeout=DEFAULT_TIMEOUT, isolate_home=False):
    """Run the agent once in work_dir. Returns (output, error)."""
    argv = [tok.replace("{prompt}", prompt).replace("{dir}", str(work_dir))
            for tok in split_runner(runner)]
    try:
        done = subprocess.run(argv, cwd=str(work_dir), capture_output=True,
                              timeout=timeout, encoding="utf-8", errors="replace",
                              env=isolated_env(work_dir) if isolate_home else None)
    except (OSError, subprocess.SubprocessError) as exc:
        return "", f"{type(exc).__name__}: {exc}"
    return (done.stdout or "") + (done.stderr or ""), None


def matches(patterns, text):
    for pattern in patterns:
        regex = _REGEX_LINE_RE.match(pattern)
        if regex:
            # re.M so /^D-A$/ anchors to a line of the answer, not to the whole
            # artifact - agent output is always multi-line.
            if re.search(regex.group(1), text, re.I | re.M):
                return True
        elif pattern.lower() in text.lower():
            return True
    return False


def answer_line(artifact):
    """The agent's answer: its first non-empty line, undressed of markdown.

    Grading the whole artifact scores the *reasoning* too, and an agent that
    explains itself well - "reading it against today would wrongly give D-B" -
    then trips the misread pattern with the very sentence that proves it read
    correctly. Measured: that turned a correct trial into an ambiguous one on
    the first real run.
    """
    for line in artifact.splitlines():
        stripped = line.strip().strip("`*_ ")
        if stripped:
            return stripped
    return ""


def grade(probe, artifact):
    """'correct' | 'misread' | 'ambiguous'. Ambiguous is reported, not dropped:
    a probe that cannot separate the two readings is a broken instrument."""
    head = answer_line(artifact)
    hit_correct, hit_misread = matches(probe["correct"], head), matches(probe["misread"], head)
    if hit_correct != hit_misread:
        return "correct" if hit_correct else "misread"
    if hit_correct and hit_misread:
        return "ambiguous"     # the answer line itself names both readings
    return _grade_whole(probe, artifact)   # nothing on the answer line; use it all


def _grade_whole(probe, artifact):
    hit_correct = matches(probe["correct"], artifact)
    hit_misread = matches(probe["misread"], artifact)
    if hit_correct and not hit_misread:
        return "correct"
    if hit_misread and not hit_correct:
        return "misread"
    if not hit_correct and not hit_misread and probe.get("one_sided"):
        return "misread"       # one-sided probe: the referent was not named
    return "ambiguous"


def wilson(hits, total):
    """95% Wilson interval. Small n is the normal case here; say so honestly."""
    if not total:
        return (0.0, 0.0)
    z = 1.96
    p = hits / total
    denominator = 1 + z * z / total
    centre = (p + z * z / (2 * total)) / denominator
    spread = z * ((p * (1 - p) / total + z * z / (4 * total * total)) ** 0.5) / denominator
    return (max(0.0, centre - spread), min(1.0, centre + spread))


# ---------------------------------------------------------------------------
# Run
# ---------------------------------------------------------------------------

def run_probes(probes, runner, runs_override=None, timeout=DEFAULT_TIMEOUT, out_path=None,
               isolate_home=False):
    """Returns (summary, exit_code). Runs every probe on both arms."""
    tally = {arm: {"correct": 0, "misread": 0, "ambiguous": 0} for arm in ARMS}
    disclosed = {arm: 0 for arm in ARMS}
    trials, failures = [], []

    for probe in probes:
        runs = runs_override or probe["runs"]
        for arm in ARMS:
            for _ in range(runs):
                with tempfile.TemporaryDirectory() as work:
                    prompt = materialise(probe, arm, work)
                    artifact, error = run_trial(runner, prompt, work, timeout, isolate_home)
                if error:
                    failures.append(f'{probe["id"]}/{arm}: {error}')
                    continue
                outcome = grade(probe, artifact)
                tally[arm][outcome] += 1
                if outcome == "misread" and _DISCLOSURE_RE.search(artifact):
                    disclosed[arm] += 1
                trials.append({"probe": probe["id"], "arm": arm, "outcome": outcome,
                               "disclosed": bool(_DISCLOSURE_RE.search(artifact)),
                               "artifact": artifact[:400]})

    graded = sum(sum(c.values()) for c in tally.values())
    print(f"[probe-misreads] alive: probes={len(probes)} arms={len(ARMS)} "
          f"trials={graded} failed={len(failures)}")
    for failure in failures[:5]:
        print(f"  runner failed: {failure}", file=sys.stderr)

    if not probes or not graded:
        print("[probe-misreads] graded nothing - no probes, or the runner never "
              "produced output. A rate over zero trials is not a result.", file=sys.stderr)
        return {"tally": tally, "trials": trials, "failures": failures}, 2

    misread_total = 0
    for arm in ARMS:
        counts = tally[arm]
        total = sum(counts.values())
        rate = counts["misread"] / total if total else 0.0
        low, high = wilson(counts["misread"], total)
        misread_total += counts["misread"]
        print(f"[probe-misreads] {arm:>8} arm: misread {counts['misread']}/{total} "
              f"({rate * 100:.0f}%, 95% CI {low * 100:.0f}-{high * 100:.0f}%) "
              f"ambiguous={counts['ambiguous']}")

    raw_total = sum(tally["raw"].values())
    hard_total = sum(tally["hardened"].values())
    delta = (tally["raw"]["misread"] / raw_total if raw_total else 0) - \
            (tally["hardened"]["misread"] / hard_total if hard_total else 0)
    print(f"[probe-misreads] the row bought you: {delta * 100:+.0f} points of misread rate")

    disclosed_total = sum(disclosed.values())
    if misread_total:
        print(f"[probe-misreads] disclosure: {disclosed_total}/{misread_total} of the trials "
              f"the artifact proved wrong said so ({disclosed_total / misread_total * 100:.0f}%)")
    else:
        print("[probe-misreads] disclosure: no misread trials to disclose")

    if out_path:
        with open(out_path, "w", encoding="utf-8") as f:
            for trial in trials:
                f.write(json.dumps(trial, ensure_ascii=False) + "\n")

    return {"tally": tally, "trials": trials, "failures": failures,
            "delta": delta, "disclosed": disclosed_total, "misread_total": misread_total}, 0


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def build_arg_parser():
    p = argparse.ArgumentParser(
        prog="probe_misreads.py",
        description="Measure the misread rate behaviourally, with no self-report in the number.",
    )
    p.add_argument("--runner", default=None,
                   help="Command template for your agent, e.g. \"claude -p {prompt}\". "
                        "{prompt} is the task, {dir} the fixture directory.")
    p.add_argument("--probes", default=None, help="Directory of *.md probe files (default: from config).")
    p.add_argument("--no-auto", action="store_true", help="Skip probes derived from the lexicon.")
    p.add_argument("--runs", type=int, default=None, help="Override each probe's run count.")
    p.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT, help="Seconds per trial.")
    p.add_argument("--isolate-home", action="store_true",
                   help="Point the agent's home at the scratch directory so it cannot read "
                        "your own instruction files - including the lexicon under test. "
                        "Verify your agent still authenticates this way before trusting it.")
    p.add_argument("--list", action="store_true", help="Show the probes that would run, and exit.")
    p.add_argument("--out", default=None, help="Write per-trial results as JSONL here.")
    p.add_argument("--config", default=None, help="Path to a misread-lexicon.json config file.")
    p.add_argument("--self-test", action="store_true", help="Run embedded self-tests and exit.")
    return p


def _resolve_config(explicit):
    path = Path(explicit) if explicit else None
    if path is None:
        cwd = Path.cwd() / "misread-lexicon.json"
        repo = Path(__file__).resolve().parent.parent / "misread-lexicon.example.json"
        path = cwd if cwd.exists() else (repo if repo.exists() else None)
    if path is None or not path.exists():
        return {}, Path.cwd()
    return json.loads(path.read_text(encoding="utf-8")), path.resolve().parent


def main(argv=None):
    args = build_arg_parser().parse_args(argv)
    if args.self_test:
        return run_self_tests()

    config, config_dir = _resolve_config(args.config)

    probes_dir = args.probes or config.get("probes") or "probes"
    probes_dir = Path(os.path.expanduser(probes_dir))
    if not probes_dir.is_absolute():
        probes_dir = config_dir / probes_dir
    probes, errors = load_probes(probes_dir) if probes_dir.exists() else ([], [])

    if not args.no_auto:
        rules = []
        for rel in config.get("lexicons", []):
            rules.extend(parse_lexicon_table(config_dir / os.path.expanduser(rel)))
        derived, skipped = auto_probes(rules)
        # A hand-written probe with the same id wins: it is the real incident.
        # Derived ones also dedupe against each other, since the same row is
        # usually present in every language's lexicon.
        have = {p["id"] for p in probes}
        for probe in derived:
            if probe["id"] not in have:
                have.add(probe["id"])
                probes.append(probe)
        if skipped:
            print(f"[probe-misreads] lexicon rows with no auto probe: {len(skipped)} "
                  f"({', '.join(skipped[:4])})")

    for error in errors:
        print(f"[probe-misreads] bad probe: {error}", file=sys.stderr)

    if args.list:
        print(f"[probe-misreads] alive: probes={len(probes)} bad={len(errors)}")
        for probe in probes:
            print(f'  {probe["id"]:<28} {probe["type"]:<4} '
                  f'raw={probe["raw"]!r} hardened={probe["hardened"]!r} '
                  f'runs={probe["runs"]}  ({probe["source"]})')
        return 2 if (not probes or errors) else 0

    if not args.runner:
        runner = config.get("runner")
        if not runner:
            print("[probe-misreads] no --runner given and none in the config. This harness "
                  "does not ship an agent; tell it how to call yours, e.g. "
                  "--runner \"claude -p {prompt}\".", file=sys.stderr)
            return 2
    else:
        runner = args.runner

    if errors:
        return 2                # a broken probe means the denominator is wrong

    _summary, code = run_probes(probes, runner, runs_override=args.runs,
                                timeout=args.timeout, out_path=args.out,
                                isolate_home=args.isolate_home)
    return code


# ---------------------------------------------------------------------------
# Self-test
# ---------------------------------------------------------------------------

STUB = (
    "import pathlib, sys\n"
    "print(pathlib.Path('answer.txt').read_text(encoding='utf-8'))\n"
)


def run_self_tests():
    results = []

    def record(label, passed):
        print(("PASS: " if passed else "FAIL: ") + label)
        results.append(bool(passed))

    def safe_check(label, fn):
        try:
            record(label, fn())
        except Exception as exc:  # noqa: BLE001 - a self-test must not crash the run
            record(label + f" (raised {exc!r})", False)

    def probe_text(answer, correct="unreal-engine", misread="above-notes"):
        return (
            "---\nid: t\ntype: M1\nraw: UE\nhardened: UE (Unreal Engine)\nruns: 1\n---\n"
            "## task\nWhich file holds the {{term}} list?\n\n"
            f"## fixture: answer.txt\n{answer}\n\n"
            f"## correct\n{correct}\n\n"
            f"## misread\n{misread}\n"
        )

    def with_stub(probe_body, **kw):
        """Run the harness end to end against a stub agent that answers with
        whatever the probe's own answer.txt fixture says."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "probes").mkdir()
            (root / "probes" / "t.md").write_text(probe_body, encoding="utf-8")
            stub = root / "stub.py"
            stub.write_text(STUB, encoding="utf-8")
            probes, errors = load_probes(root / "probes")
            if errors:
                raise ValueError(errors[0])
            runner = f'"{sys.executable}" "{stub}" {{prompt}}'
            import io
            import contextlib
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
                summary, code = run_probes(probes, runner, **kw)
            return summary, code

    # --- the harness separates the two arms, which is the whole point ---
    def arms_are_scored_apart():
        # The stub echoes the spelling it was given, so the raw arm answers
        # "UE" (the misread decoy) and the hardened arm "UE (Unreal Engine)".
        body = probe_text("{{term}}", correct="Unreal Engine", misread="/^UE$/")
        summary, code = with_stub(body)
        raw, hard = summary["tally"]["raw"], summary["tally"]["hardened"]
        return code == 0 and raw["misread"] == 1 and hard["correct"] == 1

    def delta_is_the_effect():
        body = probe_text("{{term}}", correct="Unreal Engine", misread="/^UE$/")
        summary, _ = with_stub(body)
        return abs(summary["delta"] - 1.0) < 1e-9

    def no_effect_reads_as_zero():
        # Both arms answer correctly: the row bought nothing, and the harness
        # must be able to say so rather than always finding an effect.
        body = probe_text("Unreal Engine", correct="Unreal Engine", misread="/^UE$/")
        summary, _ = with_stub(body)
        return abs(summary["delta"]) < 1e-9 and summary["tally"]["raw"]["correct"] == 1

    # --- grading is about the artifact, never about what was confessed ---
    def grading_ignores_confession():
        probe = parse_probe(probe_text("x"))
        confessed = "I may have misread this, but the answer is above-notes"
        return grade(probe, confessed) == "misread"

    def disclosure_is_counted_separately():
        body = probe_text("above-notes - though I am not certain which was meant",
                          correct="Unreal Engine", misread="above-notes")
        summary, _ = with_stub(body)
        return summary["misread_total"] == 2 and summary["disclosed"] == 2

    def silence_does_not_change_the_rate():
        loud = probe_text("above-notes - I am not certain", correct="Unreal Engine",
                          misread="above-notes")
        quiet = probe_text("above-notes", correct="Unreal Engine", misread="above-notes")
        a, _ = with_stub(loud)
        b, _ = with_stub(quiet)
        return a["misread_total"] == b["misread_total"] and a["disclosed"] != b["disclosed"]

    def reasoning_below_the_answer_is_not_graded():
        # The real-run defect: a correct answer whose explanation names the
        # wrong reading in order to rule it out.
        probe = parse_probe(probe_text("x", correct="D-A", misread="D-B"))
        return grade(probe, "D-A\n\nReading it against today would wrongly give D-B.") == "correct"

    def answer_below_a_preamble_still_grades():
        probe = parse_probe(probe_text("x", correct="D-A", misread="D-B"))
        return grade(probe, "Let me check the files first.\n\nD-B") == "misread"

    def ambiguous_is_reported_not_dropped():
        probe = parse_probe(probe_text("x"))
        return grade(probe, "unreal-engine and above-notes both look right") == "ambiguous"

    # --- a probe that cannot discriminate is refused, not scored ---
    def probe_without_correct_is_refused():
        bad = ("---\nid: b\nraw: UE\nhardened: UE (Unreal Engine)\n---\n"
               "## task\nwhat is {{term}}\n")
        try:
            parse_probe(bad, "b.md")
        except ValueError as exc:
            return "cannot discriminate" in str(exc)
        return False

    def probe_without_both_spellings_is_refused():
        bad = "---\nid: b\nraw: UE\n---\n## task\nx\n\n## correct\ny\n"
        try:
            parse_probe(bad, "b.md")
        except ValueError as exc:
            return "raw" in str(exc) and "hardened" in str(exc)
        return False

    # --- zero-fail ---
    def zero_probes_is_not_a_clean_bill():
        import io
        import contextlib
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
            _summary, code = run_probes([], "echo hi")
        return code == 2

    def runner_that_never_runs_is_not_a_result():
        # The agent command is missing entirely. Zero graded trials must not
        # print a 0% misread rate and call it good news.
        import io
        import contextlib
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "probes").mkdir()
            (root / "probes" / "t.md").write_text(probe_text("x"), encoding="utf-8")
            probes, _errors = load_probes(root / "probes")
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
                summary, code = run_probes(probes, "definitely-not-a-real-binary-xyz {prompt}")
        return code == 2 and len(summary["failures"]) == len(ARMS)

    # --- the two arms differ by exactly one word ---
    def arms_differ_only_in_the_term():
        probe = parse_probe(probe_text("{{term}}"))
        with tempfile.TemporaryDirectory() as tmp:
            raw_dir, hard_dir = Path(tmp) / "r", Path(tmp) / "h"
            raw_dir.mkdir()
            hard_dir.mkdir()
            raw_prompt = materialise(probe, "raw", raw_dir)
            hard_prompt = materialise(probe, "hardened", hard_dir)
            return (raw_prompt.replace("UE (Unreal Engine)", "UE")
                    == hard_prompt.replace("UE (Unreal Engine)", "UE")
                    and (raw_dir / "answer.txt").read_text(encoding="utf-8").strip() == "UE")

    # --- filler restores the pressure that makes a misread happen ---
    def filler_surrounds_the_real_note():
        body = probe_text("x").replace("runs: 1", "runs: 1\nfiller: 12")
        probe = parse_probe(body)
        with tempfile.TemporaryDirectory() as tmp:
            materialise(probe, "raw", tmp)
            notes = list((Path(tmp) / "notes").glob("*.md"))
            return len(notes) == 12

    def filler_never_contains_either_spelling():
        files = filler_files(30, ("cache warmup", "log rotation"))
        joined = "\n".join(files.values()).lower()
        return (len(files) == 30 and "cache warmup" not in joined
                and "log rotation" not in joined)

    def a_trailing_note_is_not_a_pattern():
        body = probe_text("x") + "\n<!--\nexplaining above-notes for the reader\n-->\n"
        probe = parse_probe(body)
        return probe["misread"] == ["above-notes"]

    def no_filler_by_default():
        with tempfile.TemporaryDirectory() as tmp:
            materialise(parse_probe(probe_text("x")), "raw", tmp)
            return not (Path(tmp) / "notes").exists()

    # --- runner templates survive a Windows path with spaces ---
    def runner_splitting_keeps_windows_paths():
        argv = split_runner(r'"C:\Program Files\x\claude.exe" -p {prompt}')
        return argv == [r"C:\Program Files\x\claude.exe", "-p", "{prompt}"]

    # --- probes derived from a bare lexicon ---
    def auto_m5_has_two_sided_discrimination():
        rules = [{"type": "M5", "forms": ["yesterday"], "target": "an absolute date", "source": "x"}]
        probes, _skipped = auto_probes(rules, today=date(2026, 9, 1))
        p = probes[0]
        return p["correct"] == ["D-A"] and p["misread"] == ["D-B"] and p["raw"] == "yesterday"

    def auto_m1_needs_an_expansion():
        with_paren = [{"type": "M1", "forms": ["UE"],
                       "target": "UE (Unreal Engine) — expand at first use", "source": "x"}]
        without = [{"type": "M1", "forms": ["PS"], "target": "spell it out", "source": "x"}]
        a, _ = auto_probes(with_paren)
        b, skipped = auto_probes(without)
        return a[0]["correct"] == ["Unreal Engine"] and b == [] and skipped == ["M1:PS"]

    def auto_probe_is_graded_end_to_end():
        # The derived probes are the coverage a bare registry still has, so
        # their grading path needs a real run, not just a shape check.
        import io
        import contextlib
        rules = [{"type": "M5", "forms": ["yesterday"], "target": "an absolute date",
                  "source": "x"}]
        probes, _skipped = auto_probes(rules)
        out = {}
        for answer in ("D-A", "D-B"):
            with tempfile.TemporaryDirectory() as tmp:
                stub = Path(tmp) / "fixed.py"
                stub.write_text(f"print({answer!r})", encoding="utf-8")
                runner = f'"{sys.executable}" "{stub}" {{prompt}}'
                buf = io.StringIO()
                with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
                    summary, _code = run_probes(probes, runner, runs_override=1)
                out[answer] = summary["tally"]
        return (out["D-A"]["raw"]["correct"] == 1 and out["D-B"]["raw"]["misread"] == 1)

    def hand_written_probe_beats_the_derived_one():
        # Same id from both sources: the incident-backed one has to win.
        rules = [{"type": "M5", "forms": ["yesterday"], "target": "date", "source": "x"}]
        derived, _ = auto_probes(rules)
        have = {"auto-m5-yesterday"}
        kept = [p for p in derived if p["id"] not in have]
        return kept == []

    safe_check("the two arms are scored apart", arms_are_scored_apart)
    safe_check("delta reports the effect of the row", delta_is_the_effect)
    safe_check("no effect reads as zero, not as a win", no_effect_reads_as_zero)
    safe_check("grading ignores what was confessed", grading_ignores_confession)
    safe_check("disclosure is counted separately", disclosure_is_counted_separately)
    safe_check("going quiet does not move the misread rate", silence_does_not_change_the_rate)
    safe_check("reasoning below the answer is not graded", reasoning_below_the_answer_is_not_graded)
    safe_check("an answer under a preamble still grades", answer_below_a_preamble_still_grades)
    safe_check("ambiguous is reported, not dropped", ambiguous_is_reported_not_dropped)
    safe_check("a probe with no correct answer is refused", probe_without_correct_is_refused)
    safe_check("a probe with one spelling is refused", probe_without_both_spellings_is_refused)
    safe_check("zero probes is not a clean bill of health", zero_probes_is_not_a_clean_bill)
    safe_check("a runner that produces nothing is not a result", runner_that_never_runs_is_not_a_result)
    safe_check("the arms differ by exactly one word", arms_differ_only_in_the_term)
    safe_check("filler surrounds the real note", filler_surrounds_the_real_note)
    safe_check("filler never contains either spelling", filler_never_contains_either_spelling)
    safe_check("a trailing note is not a match pattern", a_trailing_note_is_not_a_pattern)
    safe_check("no filler unless the probe asks", no_filler_by_default)
    safe_check("runner template keeps a quoted Windows path", runner_splitting_keeps_windows_paths)
    safe_check("auto M5 probe discriminates both ways", auto_m5_has_two_sided_discrimination)
    safe_check("auto M1 probe needs an expansion to check", auto_m1_needs_an_expansion)
    safe_check("an auto probe is graded end to end", auto_probe_is_graded_end_to_end)
    safe_check("a hand-written probe overrides the derived one", hand_written_probe_beats_the_derived_one)

    print(f"[probe-misreads] self-test: {sum(results)}/{len(results)} passed")
    return 0 if all(results) else 1


if __name__ == "__main__":
    sys.exit(main())
