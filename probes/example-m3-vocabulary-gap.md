---
id: example-m3-vocabulary-gap
type: M3
raw: perimeter throttle
hardened: perimeter throttle (rate limit)
runs: 2
filler: 40
---

## task

Search the notes/ directory. Does this service limit how many requests one client
can send? Answer with `yes: <feature name>` or with `no`.

## fixture: notes/gateway.md

# Gateway

The {{term}} rejects a client that goes over its budget for the window.
Budgets are per API key and reset on the hour.

## correct

perimeter throttle

## misread

/^\s*no\b/
/not implemented/
/does not (?:have|support|provide)/
/there is no/

<!--
This probe ships as a worked example of the file format, the way the lexicon
ships with example rows. Replace it with your own incidents.

It measures M3 - the vocabulary gap - which the linter cannot detect, because
detecting it means detecting the absence of a word nobody wrote. A probe can
still measure it: ask in the reader's words about a feature the document names
in the author's words, and see which way the answer goes.

Both arms describe the same service. Only the raw arm forces the reader to
bridge "rate limit" to "perimeter throttle" unaided.
-->
