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
That takes this repo's docs from 9 flags to 2 — and the 2 that remain are the quotation in the section
above, one in each README. They stay flagged and they stay unedited, because the alternative is a tool
rewriting someone's words to satisfy itself.

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
