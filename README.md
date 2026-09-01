# agent-misread-lexicon

[![test](https://github.com/kai-chop/agent-misread-lexicon/actions/workflows/test.yml/badge.svg)](https://github.com/kai-chop/agent-misread-lexicon/actions/workflows/test.yml)

**Your agent didn't ignore the instruction. It read a word the wrong way.** This is a registry of
the word-shapes that get misread, plus a linter that stops you from writing them again — and a miner
that recovers the misreads already buried in your transcript history.

> 🇯🇵 **日本語**: [README.ja.md](README.ja.md)

## The problem this is actually about

When an agent does the wrong thing, the usual reflex is to add a rule. But a large share of these
failures are not disobedience and not missing rules — the instruction was there, and one *word* in it
resolved to the wrong thing.

That failure has a property that makes it expensive: **it is silent on both sides.** The writer sees a
sentence that says exactly what they meant. The reader sees a sentence that makes complete sense. There
is no error, no exception, no failing test — just work that goes somewhere else. And because nothing
looks broken, the same word keeps doing it.

The other property: **misreads repeat by shape.** A two-letter abbreviation that collides with a common
word will be re-read as a typo *every* time, by every model, in every session. That is what makes them
worth registering — a class you can name is a class you can check for mechanically.

## What was measured

This started as a sweep of one real agent-transcript corpus: **107 transcripts, 1,220 human messages**,
cross-checked against a hand-kept incident ledger. Every misread found fell into one of six shapes.

![Six shapes a misread takes — two are checked by this linter, two are already owned by other tools, and two deliberately have no detector](assets/six-shapes.svg)

| Type | Mechanism | Detectable? |
|---|---|---|
| **M1** Short ambiguous token | A 2–3 character abbreviation collides with a common word, and the reader silently re-reads it as a typo | ✅ this linter |
| **M2** Non-ASCII identifier mangling | A keyword survives as bytes but arrives corrupted through a pipe/encoding boundary; the corrupted copy is then diagnosed as the real thing | ⛔ belongs to your encoding guard, not here |
| **M3** Vocabulary gap | The doc is written in the author's words; the reader searches in theirs, finds nothing, and concludes the feature doesn't exist | ⛔ undecidable — see below |
| **M4** Markup collision | Correct as text, different meaning once rendered (a raw pipe inside a table cell) | ⛔ belongs to a markdown linter |
| **M5** Relative reference | `yesterday`, `the above`, `the previous session` — fine in chat, referent-free in a durable doc read later | ✅ this linter |
| **M6** One-sided enumeration | A qualifier lists what *is* included and never closes the negative side, so the reader invents an exclusion | ⛔ hand-audited — see below |

**Two of six ship with a detector. That is the point, not a shortfall.**

- **M2 and M4 already have owners.** If you add a third checker for a class an existing guard covers,
  you get two things that disagree and neither gets trusted. Point them at your existing tools.
- **M3 is undecidable by construction.** Detecting it means detecting the *absence* of a synonym that
  nobody wrote down. A linter cannot find a word that isn't there. It stays in the registry as a shape
  you can recognize by hand — in the measured corpus it was the **most common** class, which is exactly
  why pretending to automate it would be the harmful move.
- **M6 was left to hand-audit on purpose.** In the corpus its total population was ~12 occurrences. A
  detector for a 12-item population is a detector you will spend more time tuning than using.

If your instinct is "so add four more detectors" — that instinct is the failure mode this repo is
arguing against. A checker that fires on healthy states trains you to ignore it, and then it protects
nothing.

## M6 deserves its own section

Of the six, M6 is the one most likely to be new to you, and it had the **highest recurrence** in the
measured corpus (the same misread three separate times).

A qualifier like *"for non-trivial tasks (new subsystem, delegation, large edits) do X"* looks
complete. It is not. It says what counts as non-trivial; it never says what doesn't. So a reader facing
an analysis task reasons: *the examples are all code work, this is analysis, therefore excluded* — and
skips the rule. In the measured case the reader's own activity (**delegation**) was sitting right there
in the list.

The instructive part: **adding more examples does not fix it.** The matching example was already
present and was read past. What fixes it is closing the negative side:

> ❌ `for non-trivial tasks (new subsystem, delegation, large edits)`
> ✅ `for non-trivial tasks — trivial means all four of: single file, <10 lines, no new behavior, no search needed. **Everything else qualifies.**`

The same corpus contained a rule written the second way. It has no misread on record.

## Install

Requires Python 3.8+. No dependencies.

```bash
git clone https://github.com/kai-chop/agent-misread-lexicon
cd agent-misread-lexicon
cp misread-lexicon.example.json misread-lexicon.json   # then edit paths for your setup
python tools/check_misread_words.py --self-test        # verify it works on your machine
```

Config (`misread-lexicon.json`) — every path is yours to set; nothing is hardcoded:

```json
{
  "roots": ["~/.claude", "~/.codex", "~/.gemini", "~/.config/github-copilot"],
  "lexicons": ["lexicon/lexicon.en.md", "lexicon/lexicon.ja.md"],
  "transcripts": "~/.claude/projects",
  "scan": [
    { "rules": ["M1"], "include": ["rules/**/*.md", "skills/**/*.md"], "exclude": ["ledgers/**"] },
    { "rules": ["M5"], "include": ["**/HANDOFF.md"], "exclude": ["ledgers/**"] }
  ]
}
```

Note the two scan blocks have **different scopes**. That is deliberate and is the single most important
tuning knob here: M1 applies to instruction docs, where a misread misroutes work. M5 applies only where
your own conventions already demand absolute dates. Point M5 at everything and it will fire on prose
where a relative date is perfectly fine — see *Scope is the tuning knob* below.

### Where your agent's docs live

One config can point at several agent homes at once — `roots` takes a list. **A declared root that
doesn't exist on your machine is skipped silently**, because nobody has all of these installed at
once. The alive line reports `roots=<existing>/<declared>` so you can see what was actually scanned:

```
[misread-lint] alive: rules=4 roots=2/4 targets=138
```

(Measured on a machine that had 2 of the 4 declared roots installed. Your numbers will differ —
`rules` counts the rows in your lexicons, `targets` the files your globs actually matched.)

| Environment | Home | Instruction docs |
|---|---|---|
| Claude Code | `~/.claude` | `CLAUDE.md`, `rules/`, `skills/`, `workflows/` |
| OpenAI Codex | `~/.codex` | `AGENTS.md`, `skills/` |
| Gemini CLI | `~/.gemini` | `GEMINI.md` |
| GitHub Copilot | repo | `.github/copilot-instructions.md` |
| Cursor | repo | `.cursor/rules/**`, `.cursorrules` |
| Any | repo | `AGENTS.md`, `HANDOFF.md` |

Project-level docs live in whichever repo you're working in, not a home directory — add that path to
`roots` too, e.g. `"roots": ["~/.claude", "."]`.

**A limitation, stated plainly:** `mine_misreads.py` is verified only against Claude Code's JSONL
transcript format — records with `type: "user"`/`"assistant"` and Anthropic-shaped `message.content`
blocks. Other vendors store conversation history differently, and the miner has not been tested against
any of them. Treat it as Claude-Code-only until someone verifies otherwise; this repo does not claim
broader support.

## Making it actually fire

Everything above gives you a tool you have to remember to run. That is not a mechanism — it is a
tool plus an intention, and the intention is the part that fails. Two more steps close the loop.

**1. Install the skill so a misread routes to the registry.** Copy the skill for your language into
your agent's skills directory, so that when someone says *"you misread that"* the agent is pointed at
the lexicon instead of just apologising:

```bash
mkdir -p ~/.claude/skills/misread-lexicon
cp skill/en/SKILL.md ~/.claude/skills/misread-lexicon/   # or skill/ja/SKILL.md
```

Edit the copied file's paths to match where you put this repo and your config.

**2. Wire the sweep to session start so nobody has to remember it.** In Claude Code, add one hook to
`~/.claude/settings.json` under `hooks.SessionStart` (merge with any hooks already there — do not
replace the array):

```json
{
  "type": "command",
  "command": "python /path/to/agent-misread-lexicon/tools/check_misread_words.py --config ~/.claude/misread-lexicon.json || true",
  "timeout": 30
}
```

`|| true` matters: the linter exits 1 when it finds something, and an unguarded non-zero exit is
treated as a hook failure. The sweep is ~0.4s over ~60 files, and its first line is ASCII, so it
survives any console encoding.

**Then prove it fires — do not assume.** Installing a hook and observing its output are different
events, and only the second one is evidence:

```bash
# plant a deliberate violation inside your configured M1 scope
echo 'probe: UE bare token' > ~/.claude/templates/probe.md
# start any new session, then confirm the finding line appeared at session start
rm ~/.claude/templates/probe.md
```

If you skip this, you get the failure this repo is about: a mechanism that is installed, green, and
never delivering.

**3. Optional — catch it at write time, not just at read time.** The session-start sweep flags a file
the next time somebody opens a session, which means the writer has already moved on. `--hook` lints
the one file that was just written, right after it lands. In Claude Code:

```json
{
  "type": "command",
  "command": "python /path/to/agent-misread-lexicon/tools/check_misread_words.py --config ~/.claude/misread-lexicon.json --hook --emit claude",
  "timeout": 10
}
```

Register it under `hooks.PostToolUse` with matcher `Edit|Write|MultiEdit`. `--hook` reads the written
file's path on stdin — a tool event in any of the common JSON shapes, or **a bare path on a line**,
which is what an editor save-hook or a CI step sends:

```bash
echo path/to/HANDOFF.md | python tools/check_misread_words.py --config my.json --hook
```

Behavior, in the order it protects you:

- **Only files your configured scan blocks cover are checked** — everything else passes in silence,
  so the vast majority of writes cost one interpreter startup (~0.2s measured) and nothing more;
- **reports; it never blocks.** The write already happened. A post-write gate cannot un-write a
  file, and a checker that refuses writes is one you eventually satisfy by rewording quotations;
- **it goes quiet instead of nagging.** A file re-reported with the *same* finding count gets one
  repeat warning, then silence for the session; any change in the count — up or down — makes it a
  fresh report. The suppression state lives in a temp file (`--state PATH` to relocate it) and
  losing it merely means an extra report — the safe direction to fail in;
- always exits 0, so a finding is never mistaken for a hook failure.

`--emit` names the output contract: the default `text` prints findings to stderr and suits any
editor or CI host; `claude` prints the `{"decision": "block", "reason": ...}` JSON that Claude
Code's PostToolUse feeds back to the agent. The value is named for its consumer on purpose — that
JSON shape is Claude Code's, and defaulting to it would be exactly the kind of quiet vendor
assumption this repo exists to flag.

Scope is resolved from the same config, with one rule worth knowing: **a pattern starting with `**/`
is location-independent, every other pattern is root-anchored.** So the documented config gives you
`**/HANDOFF.md` on a HANDOFF written *anywhere* — including a project checkout that is not a declared
root and that the sweep therefore never sees — while `rules/**/*.md` still only means the rules
directory inside a root you declared.

The same scope rule is available to the CLI as `--paths FILE --respect-scope`. Plain `--paths` is
unchanged: it still applies every rule to every file you name.

## Use

```bash
python tools/check_misread_words.py                    # sweep the configured scope
python tools/check_misread_words.py --paths FILE ...   # lint specific files
python tools/mine_misreads.py --out candidates.jsonl   # recover misreads from past transcripts
python tools/trace_misreads.py --sweep                 # recover the ones nobody reported
python tools/probe_misreads.py --runner "claude -p {prompt}"   # does rewriting the word help?
```

Exit 1 on findings, 0 when clean. **It never rewrites your files** — it prints the location and the
clearer form, and you decide.

## The registry grows; the code doesn't

The linter parses its rules out of the lexicon table at runtime. Adding a word is one table row:

```
| M1 | UE | UE (Unreal Engine) — expand at first use in the file | example |
```

No code change, no redeploy — and once the sweep is wired to session start (above), no re-run either.
**The intended workflow is that you add a row the moment a misread actually happens to you** — the
registry is a record of your own accidents, not a dictionary someone else guessed at. It ships nearly
empty on purpose.

The table is located by a marker comment, not by its header text, so your lexicon can be in any
language. Columns are positional: type, misread form, clearer form, source.

## Recovering the misreads you already had

Forward-only growth means you start from zero and wait for pain. `mine_misreads.py` reads your existing
agent transcripts and surfaces the corrections that already happened — the moments a human said *"no,
that's not what I meant"* — together with what the agent had just read.

It reports candidates in two tiers: messages that explicitly name a reading error, and generic pushback.
Patterns are bilingual (English + Japanese). It is a **report tool, not a gate** — it always exits 0,
because the output needs a human to decide which candidates are real.

Expect a low yield and be glad about it. On the reference corpus, 1,220 human messages produced 51 raw
hits, and most of those were machine-generated conversation summaries that had to be filtered out. The
survivors were few — but they were the ones worth registering.

## The misreads nobody reported

Everything above needs somebody to have noticed. `mine_misreads.py` finds the moments a human said
*"that's not what I meant"*, which makes it blind twice over: the human had to notice, and somebody
had to say so. Write out what a count like that actually measures:

```
reported misreads  =  actual misreads  ×  the odds one gets reported
```

One equation, two unknowns. **A drop in it is not evidence of anything** — fewer misreads and fewer
admissions look identical from this side. If you install this repo and the number falls, you have
learned nothing yet. That is the honest status of every self-reported misread count, this one included.

`trace_misreads.py` reads the other side. A misread leaves mechanical traces in a transcript whether
or not anyone catches it, and those traces come with a denominator.

```bash
python tools/trace_misreads.py --sweep
```

### Signal 1 — assert-on-empty

A search comes back empty and the next reply concludes the thing does not exist. The reader's
vocabulary missed; the claim it produced was about your codebase. That is M3 — the class this README
calls undetectable, and it is undetectable *in the documents*. In behaviour it is plain:

```
[trace-misreads] alive: transcripts=107 searches=635 zero_hit=88
[trace-misreads] assert-on-empty: 8/88 (9.1%) never questioned by a human: 8
```

Eighty-eight searches found nothing. Eight of them turned straight into "it does not exist" — and
**not one of those eight was questioned by the human afterwards.** Hand review of the eight: six
real, two false (an absence claim about a gap rather than a feature — *"there is no window between
the two steps"* — and a shell transcript quoted inside a table).

Note what cannot be gamed. The finding is produced by the agent *not* flagging its uncertainty, so
going quieter raises this number rather than lowering it. Silence is the numerator here.

### Signal 2 — vocabulary gaps, and the axis they generalise along

A search comes back empty; a later search for the same concept, spelled differently, succeeds. The
pair names how the two spellings differed:

```
[trace-misreads] vocabulary-gap: 25 pair(s) axes={anchor:9, case:1, dash:2, other:13}
  [case]   looked for 'rtk'                  -> found under 'rtk|RTK'
  [anchor] looked for 'tests/test_*.py'      -> found under '**/test_*.py'
  [dash]   looked for '能力差|complementarity' -> found under 'Claude–Codex能力差|Claude-Codex能力差'
```

This is the forward-looking half, and it is why the tool records an axis rather than a word. A word
that burned you is one incident. An **axis** that burned you is a prediction: every other term in
your docs exposed on that same axis is a misread that has not happened yet.

`--sweep` makes the prediction. It sweeps only an axis some gap already proved costly, and reports
only a collision it can see both halves of in your own corpus — so nothing is guessed from a
dictionary:

```
[trace-misreads] sweep: axes=['dash'] docs=577 exposed=2
  [dash] A-C / A−C  (adversarial_prompts.md, attempt_agent.md)
```

`A−C` is written with U+2212. No search anyone types for `A-C` will ever reach it. Nothing has gone
wrong yet — which is the entire point of this channel.

### Catching it in the session that is producing it

The audit reads finished transcripts. The same signal is computable while the session is still
running, which turns a monthly statistic into a line at the end of the turn that produced it.
Register this under `hooks.Stop`:

```json
{ "type": "command", "command": "python /path/to/tools/trace_misreads.py --hook", "timeout": 15 }
```

It reads the hook event on stdin, takes `transcript_path` from it, and reports that one session:

```
[trace-misreads] hook: session=33c8e498-9d5c-4c searches=2 zero_hit=2 asserted_absence=1
[trace-misreads] searched 'ledgers/*', found nothing, then concluded: `ledgers/blender-addons.md`
  ・`ue-plugins.md`・`pending-actions.md` はこのPC（PCB）に一度も存在せず …
```

Two things differ from the audit, both deliberately:

- **A quiet session is not a failure.** The audit exits 2 when it inspected nothing, because an audit
  that scanned zero items must never report clean. A session that simply did not search is normal, so
  the hook always exits 0 and moves the distinction into the alive line — `searches=0` is a quiet
  session, while a transcript it could not read says so on stderr.
- **Nothing is labelled caught or uncaught.** That verdict is decided by looking *after* the claim,
  and mid-session there is no after. The field is `None` rather than `False`: "nobody has objected
  yet" and "nobody ever objected" are different facts, and only one of them is knowable here.

**Searches run through your shell are not covered — measured before deciding, not assumed.** In the
reference corpus `Bash` ran 896 search-shaped commands against the search tools' 650, which looks
like a large blind spot. But 873 of them were compound (`grep … && echo …`, pipelines), and in a
compound line "no output" cannot be attributed to the search rather than to any other part of it.
Restricted to bare search commands, where the attribution holds: **23 commands, 0 of them empty.** A
shell path would have added zero findings, so there is no shell path.

### One axis survived measurement; three did not

| axis | findings swept across 577 docs | verdict |
|---|---|---|
| `dash` — a dash you cannot type | 2 | shipped |
| `case` | 52 | cut: nearly all heading title-case (`Multi-Agent` / `Multi-agent`) |
| hyphen vs `_` | 58 | cut: mostly a CLI flag beside a variable of the same name |
| `anchor`, `space` | — | reported as gaps; no doc-side form that isn't a guess |

An axis can stay in the gap report — it is evidence, it cost a real search — without being
sweepable, because a prediction has to be worth reading. The cuts are pinned by self-tests, so
turning one back on has to be a deliberate edit rather than a drift.

### What it still cannot see

A misread that never ran a search leaves no trace here. If the reader resolved your word to the
wrong thing and went straight to work, this tool is as blind as the other one. The claim is narrow
on purpose: **it measures the misreads that passed through a search, with a denominator, and without
anyone having to admit anything.** It is not a misread rate for your session.

It exits 0 normally and **2 when it inspected nothing** — a checker that scanned zero items and
reports "no findings" is the exact failure this repo exists to argue against.

## Does rewriting the word actually help?

The two channels above count misreads. Neither answers the question a registry exists to answer:
**when you rewrite a row into its clearer form, does anything change?**

`probe_misreads.py` runs that as an experiment. A probe is one task with one word in it, written
twice — `raw:` is the spelling that gets misread, `hardened:` is your lexicon's third column. Both
arms get the identical fixture, the identical task, the identical everything else, so the model's
mood and the day it is land on both arms and cancel:

```
misread rate (raw arm) − misread rate (hardened arm) = what that row bought you
```

Nothing in that number is self-reported. Grading reads the artifact — which file the agent named,
which id it answered — so an agent that stops admitting mistakes does not move it. Disclosure is
measured too, but as a **separate** number, conditioned on the trials the artifact already proved
wrong. Two numbers, and now the failure modes come apart:

| | disclosure steady | disclosure falls |
|---|---|---|
| **misread rate falls** | it genuinely reads better | it reads better *and* says less |
| **misread rate flat** | nothing changed | **it just went quiet** |

Bring your own agent; the harness does not ship one:

```bash
python tools/probe_misreads.py --list                          # what would run
python tools/probe_misreads.py --runner "claude -p {prompt}"   # run it
```

`{prompt}` is the task and `{dir}` the scratch directory. The command is run without a shell, so a
quoted Windows path with spaces survives intact.

### A probe is one markdown file

```markdown
---
id: example-m3-vocabulary-gap
type: M3
raw: perimeter throttle
hardened: perimeter throttle (rate limit)
runs: 2
---
## task
Read gateway.md. Does this service limit how many requests one client can send?
Answer with `yes: <feature name>` or with `no`.

## fixture: gateway.md
The {{term}} rejects a client that goes over its budget for the window.

## correct
perimeter throttle

## misread
/^\s*no\b/
```

One `{{term}}` placeholder, two spellings, and the two arms differ by nothing else. `## correct` is
mandatory: a probe that cannot tell a right answer from a crash is refused at load, not scored.
Patterns are substrings, or `/regex/` per line.

Note what this probe measures — **M3, the vocabulary gap**, the class this README calls undetectable.
A linter cannot find the absence of a word nobody wrote. A probe can: ask in the reader's words about
a feature the document names in the author's, and watch which way the answer goes.

### Probes you get without writing any

A registry that ships nearly empty would give you a harness with nothing to run, so probes are also
derived straight from lexicon rows — columns two and three already *are* the two arms:

```
[probe-misreads] alive: probes=4 arms=2 trials=8 failed=0
  example-m3-vocabulary-gap  M3  raw='perimeter throttle'  hardened='perimeter throttle (rate limit)'
  auto-m1-UE                 M1  raw='UE'                  hardened='UE (Unreal Engine)'
  auto-m5-yesterday          M5  raw='yesterday'           hardened='2026-08-02'
```

A hand-written probe with the same id always wins — it came from an incident, the derived one is a
template. Rows that cannot produce a fair question are skipped and counted (`lexicon rows with no
auto probe: 9`) rather than silently dropped.

### The first real run found no effect, and says so

Eight trials against a live agent:

```
[probe-misreads]      raw arm: misread 0/4 (0%, 95% CI 0-49%) ambiguous=0
[probe-misreads] hardened arm: misread 0/4 (0%, 95% CI 0-49%) ambiguous=0
[probe-misreads] the row bought you: +0 points of misread rate
```

Read that interval before reading the zero. **At four trials an arm, this cannot tell a 40-point
improvement from nothing.** The honest summary is not "the lexicon does not work" — it is "this
experiment had no power", and the fix is more runs and probes built from misreads that actually
happened to you, not a bigger claim.

Two things only a real run could have found, both now fixed or documented:

- **Grading the reasoning instead of the answer.** An agent answered `D-A` correctly and then
  explained *"reading it against today would wrongly give D-B"* — and the misread pattern matched
  that sentence. Grading now reads the answer line first and falls back to the whole artifact only
  when the answer line decides nothing.
- **The agent under test reads your instruction files.** One trial resolved a bare token and said
  where from: the tester's own registry, loaded from their home directory. The raw arm was being
  handed the answer.

  Claude Code ships the right switch for this — `--bare` skips CLAUDE.md auto-discovery along with
  hooks and auto-memory — but it carries a condition worth measuring before trusting it:

  | route | suppresses CLAUDE.md? | still authenticated? |
  |---|---|---|
  | `--bare`, or `CLAUDE_CODE_SIMPLE=1` alone | yes | **only with `ANTHROPIC_API_KEY`** — measured `Not logged in` under OAuth |
  | `--setting-sources project` | **no** — measured, the user file still loads | yes |
  | `--isolate-home` (this harness) | yes | same API-key condition |

  So run the probes through an API-key runner and the contamination is gone. Run them as a
  browser-logged-in user and it is not: the raw arm keeps being handed the answer. That is a fact
  about the agent rather than about this harness, and it belongs in how you read the numbers.

### Three attempts to make a probe bite, and what they measured

A probe is only an instrument if it can catch the thing at least once. Three designs, 22 live trials,
and the raw arm never failed:

| design | what the agent actually did | raw arm |
|---|---|---|
| one small fixture | read all of it | 0/5 |
| `filler: 300` — a corpus too big to read | grepped a synonym set, then re-scanned broadly | 0/3 |
| the question buried as item 3 of a 4-item checklist | same | 0/3 |

Its own evidence line on the second attempt: `Grep -i "throttle|rate limit|429|quota|token bucket|…"
→ 1 hit`, then `limit|cap|budget|API key|…` → 27 hits. **A reader that enumerates synonyms before it
starts cannot be caught by a vocabulary gap.** (`filler: N` surrounds the real note with N plausible
ones; the knob is real and it is what the second row measures. It was not enough.)

The reason looks structural rather than fixable. Every incident in the mined transcripts happened
deep inside a long session, where the search was one step of fifty under some other goal. A probe is
a short fresh session whose entire purpose is that one question — and a reader with all of its
attention on one question is careful.

So the scope, stated rather than left as a to-do: **`probe_misreads.py` measures whether a rewrite
changes behaviour in a short, fresh session. The misreads this repo is about happen in long, loaded
ones.** That is a boundary of the method. The channel that watches the real thing is
`trace_misreads.py`, which reads the sessions where it actually occurs.

### Turning a measured gap into a probe

`trace_misreads.py --emit-probes DIR` builds a probe from a gap the miner already found — the pair is
measured, so the two arms are real spellings rather than invented ones. It lists candidates and
writes nothing until you name one with `--gap ID`:

```
[trace-misreads] convertible gaps: 12 of 25. Pick the pair that is two names for one thing.
  gap-codex-claude-complementa   doc says 'codex_claude_complementarity' / reader looked for '能力差'
  gap-character-generator        doc says 'character-generator' / reader looked for 'character generator'
```

Emitting all of them was tried and measured: **12 files, of which 1 was a real pair of names for one
thing.** The pairing can see that two searches ran near each other; whether two words mean the same
thing is not a question string comparison can answer, so it is left to you.

It exits 0 normally and **2 when it graded nothing** — no probes, or a runner that never produced
output. A rate over zero trials is not a result.

## Scope is the tuning knob

The first real sweep of this tool produced three findings and **one false positive**, and the false
positive is the most useful thing in this README.

M5 fired on the word `tomorrow` in a personal note, where it was used conceptually — *"a tomorrow
rebuilt out of nothing but warmth"* — and partly as a direct quote of someone's own words. Nothing was
rotting. The rule was right; the *scope* was wrong.

The fix was not to reword the sentence. **Never let a linter edit a quotation.** The fix was to narrow
M5's default scope to handoff documents, where a stale relative date actually does damage, and to burn
that exact line into the self-test as a negative control so the scope can't silently widen again.

That is the discipline this repo asks for: when a detector misfires, the first question is not "how do I
word this to satisfy the checker" — it is "was this file ever in scope."

### ⚠ This README does not lint clean, on purpose

`--paths` applies every rule regardless of your configured scope. So pointing it at this repo's own
docs flags them — a document *about* relative references necessarily contains the words `yesterday`
and `tomorrow`.

Most of those are **mentions, not uses**, and they are marked as code spans, which the linter exempts.
That leaves 4 across the two READMEs, and **every one of them is a quotation**: the passage in the
section above, one in each file, plus an agent's own explanation quoted in the probe section
(`reading it against today would wrongly give D-B`, one in each file). They stay flagged and they
stay unedited, because the alternative is a tool rewriting someone's words to satisfy itself — and
the second pair arrived by exactly the route this section warns about: writing about the tool
produced new prose the tool then flagged.

Neither of these files is in the default scan scope, so a normal sweep never sees them. That is the
whole lesson repeated in miniature: **the rule was never the problem; the scope was.**

## What this is not

- **Not a spell-checker or style linter.** It has no opinion about your prose. It only knows word-shapes
  that have already been misread, and it only knows the ones you register.
- **Not a guard.** Nothing is blocked, nothing is rewritten. It reports and exits.
- **Not an encoding fixer.** M2 is listed for completeness and deliberately has no detector here.
- **Not a substitute for reading the file.** M3 — the most common class measured — is exactly the one no
  tool can catch. If you claim a feature doesn't exist, open the file that would own it. A search that
  came back empty only proves your vocabulary didn't match, not that the thing is absent.

## License

MIT
