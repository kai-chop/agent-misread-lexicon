# Misread Lexicon (English)

This file is a registry, not prose. `tools/check_misread_words.py` parses the
table below to know which tokens and phrases tend to get misread by an agent
(or by a human skimming an agent's output), and what to write instead.

The table is located by the `<!-- misread-lexicon:table -->` marker comment
that immediately precedes it, not by matching the header text — so this file
and its non-English counterpart can each use their own language for the
header row while both feed the same linter. Columns are positional:
1 = type, 2 = misread form, 3 = clearer form, 4 = source.

<!-- misread-lexicon:table -->
| Type | Misread form | Clearer form | Source |
|---|---|---|---|
| M1 | UE | UE (Unreal Engine) — expand at first use in the file | example |
| M5 | yesterday/today/tomorrow/last week/next week | an absolute date (e.g. 2026-08-28) | example |

## How to add a row

Add a row to the table above and the linter picks it up with no code change.
Use `M1` for a short, ambiguous token that should be expanded on first use per
file (acronyms, product names that collide with common words, etc.). Use `M5`
for a relative time reference that should be replaced with an absolute date.
Other rule types may be introduced later; unknown types are parsed but simply
produce no checks until the tool supports them.
