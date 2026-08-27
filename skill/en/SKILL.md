---
name: misread-lexicon
description: Register and check word-shapes that get misread in files. Use when a term was read as something else (an abbreviation taken for a typo, a relative date with no referent, a search that came back empty for a feature that exists), when the user says you misread or misunderstood something you read, when hardening instruction docs / skill descriptions / handoff documents so a later session or a delegated agent reads them the same way you meant them, or when mining past transcripts for misreads worth registering. Triggers - misread, misinterpreted, "that's not what I meant", ambiguous abbreviation, relative date in a doc, wrong subsystem after delegation, wording that keeps being read wrong.
---

# misread-lexicon — stop the same word from being misread twice

A misread is not disobedience and not a missing rule: the instruction was there and one **word** in it
resolved to the wrong thing. It is invisible from both sides — the writer sees their meaning, the reader
sees a sentence that makes sense — so nothing fails, and the same word keeps doing it.

Misreads repeat **by shape**, which is what makes them registerable.

## The six shapes

| Type | Mechanism | Who owns it |
|---|---|---|
| M1 | Short ambiguous token: a 2–3 char abbreviation collides with a common word and is re-read as a typo | this linter |
| M2 | Non-ASCII identifier mangled by an encoding/pipe boundary; the corrupted copy gets diagnosed as real | your encoding guard |
| M3 | Vocabulary gap: doc uses the author's words, reader searches with theirs, empty result read as absence | **nobody — hand discipline** |
| M4 | Markup collision: correct as text, different meaning once rendered (raw pipe in a table cell) | markdown linter |
| M5 | Relative reference: `yesterday`, `the above` — referent-free once read later | this linter |
| M6 | One-sided enumeration: a qualifier lists inclusions, never closes the negative, reader invents an exclusion | **hand audit** |

## When you hit a misread — do this

1. **Name the shape** from the table. If it is M1 or M5, it is registerable and will be caught next time.
2. **Add one row** to the lexicon table. That is the whole change — the linter parses the table at runtime,
   so no code edit is needed:
   `| M1 | UE | UE (Unreal Engine) — expand at first use | 2026-08-28 |`
3. **Fix the source**, but fix the *right* thing:
   - M1 → expand at first use in that file. Pay special attention to **skill `description:` fields** —
     they are read every session for routing, so a misread there misroutes delegated work.
   - M5 → replace with an absolute date, **unless the word is a quotation or conceptual**, in which case
     the scope is wrong, not the word (see below).
   - M6 → **do not add more examples.** The matching example is usually already present and was read past.
     Close the negative side: `... — everything else qualifies.`
4. **Run the sweep** and confirm it goes to zero: `python tools/check_misread_words.py`

## Never let the linter edit a quotation

When a detector fires, the first question is not "how do I reword this to satisfy it" — it is
**"was this file ever in scope?"**

A measured example: M5 fired on `tomorrow` in a personal note where the word was conceptual and partly a
verbatim quote of someone's own words. Nothing was rotting. Rewording it would have destroyed meaning to
please a checker. The correct fix was to narrow the rule's default scope and burn that exact line into the
self-test as a negative control.

Scope is the tuning knob. M1 belongs on instruction docs (where a misread misroutes work); M5 belongs only
where your conventions already demand absolute dates (handoff documents).

## Do not add detectors for M2, M3, M4, M6

- M2 / M4 already have owners. A second checker over the same ground produces two verdicts that disagree,
  and then neither is trusted.
- M3 is undecidable: detecting it means detecting the **absence** of a synonym nobody wrote. It was the
  **most common** shape measured — which is exactly why faking automation for it is the harmful move.
  Discipline instead: *before claiming a feature does not exist, open the file that would own it.* An empty
  search proves your vocabulary didn't match, not that the thing is absent.
- M6's measured population was ~12 occurrences. A detector for 12 items costs more to tune than to skip.

A checker that fires on healthy states trains people to ignore it, and then it protects nothing.

## Recovering past misreads

`python tools/mine_misreads.py --out candidates.jsonl` reads existing agent transcripts and surfaces
corrections that already happened, with what the agent had just read. Two tiers (explicit reading-error
language, and generic pushback), bilingual patterns. It is a report, not a gate — always exits 0, because
a human decides which candidates are real.

Expect low yield: on the reference corpus, 1,220 human messages produced 51 raw hits, most of which were
machine-generated conversation summaries that had to be filtered out.

## Files

- `lexicon/lexicon.en.md`, `lexicon/lexicon.ja.md` — the registry the linter parses (marker-located table,
  positional columns, any language)
- `tools/check_misread_words.py` — detection only, never rewrites; exit 1 on findings
- `tools/mine_misreads.py` — retroactive miner; always exit 0
- `misread-lexicon.json` — your paths and scan scopes
