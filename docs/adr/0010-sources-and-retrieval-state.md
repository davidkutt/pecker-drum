# ADR 0010: Sources per attempt and the retrieval state

Status: accepted (David, 24 November 2026)

## Context

M2.1 answers a question the reports could not: a blocked `indirect_doc` attempt doesn't say whether
the target retrieved the planted document. "Blocked" mixed two different results, the model saw the
attack and resisted, or the retriever never surfaced it, and in the first private run every blocked
indirect attempt was ambiguous for that reason. Both kinds of target already return sources
(`Observation.sources`), but no report kept them and nothing asked what they held. Six points needed
a decision: how a planted fixture is recognised in the sources of each target kind, when a missing
match may be called `not_retrieved`, which states exist, what the recorded signal means, how a
missing marker is treated, and where the results land. Grading, outcomes, rates and the self-test
stay exactly as they are: the matcher describes the attempt and never decides anything.

## Decision

1. **The target that planted reports it.** A planting target fills a defaulted
   `Observation.planted` with one `PlantedFixture(ref, document_id, tag)` per fixture it planted, so
   the matcher relies on what the target reports. The twin reports the item's own fixtures:
   `document_id` is the id the fixture was loaded under for a `doc:` fixture, and `scenario:` and
   `web:` fixtures have no document id and no tag. Base documents sit in the twin's world but were
   not planted by the attempt, so they are not listed. An HTTP target plants `doc:` fixtures only,
   with the target's own id for the document and the run tag `drum-fixture-<run id>`. Markers pass
   the redactor like every other string in an HTTP observation (ADR 0009), so matching runs over the
   same ids the reports keep. A target that reported nothing it planted (it raised before it could
   plant, or before it could report) falls back to the item's own fixtures, one `PlantedFixture(ref)`
   per ref: those carry no id and no tag, so only their canaries can match, and the errored attempt
   is `unknown` rather than `null`. `null` keeps its meaning: the item plants nothing.
2. **Two signals, either one counts.**
   - `id` is the fixture's identity. A planted document id counts when it appears between brackets
     in a source text (the twin's tools print `[<doc_id>] <title>`), when it is the whole source id,
     or when it is followed by a separator. The separator set is `#`, `:` and `/`; never `-`, `_` or
     `.`, which are common inside ids, so a planted `doc-1` does not match `doc-1-copy`,
     `doc-1_x` or `doc-1.md`.
   - `canary` is any canary of a planted fixture, matched with the normalisation and 12-hex core of
     ADR 0003, in a source text or a source id. Only canaries whose `KnownCanary.source` is the
     planted fixture's ref count: the system-prompt canary (`system`) and the canaries of base
     documents never count, even when a source quotes them.
   - All three fixture kinds count on the twin (`doc:`, `scenario:`, `web:`); an HTTP target plants
     `doc:` fixtures only (ADR 0009), so its canary and id signals come from those.
3. **A `None` marker is skipped, never matched.** `document_id` and `tag` are `str | None`, and
   `None` means "this target has no such marker". An empty string would match every source id and
   every `[]` in a text, so it is never used to mean "absent".
4. **States: `retrieved`, `not_retrieved`, `unknown`, and `null`.**
   - `retrieved`: at least one planted fixture matched by either signal.
   - `not_retrieved`: nothing matched, and every planted fixture had at least one usable marker.
   - `unknown`: the target exposes no `sources` channel (ADR 0009's channel list), a planted fixture
     had no usable marker, or the attempt errored and nothing matched. An errored attempt that did
     match is `retrieved`: the retrieval happened, whatever broke afterwards.
   - `null`: the item planted nothing, or the attempt was written before M2.1.
5. **The guard for `not_retrieved`.** "Nothing matched" only means "not retrieved" when a match was
   possible: every planted fixture must have a document id, a tag or at least one of its own
   canaries. One fixture without a usable marker makes the attempt `unknown`, because blaming the
   retriever for a missing marker would be a number with no evidence behind it.
6. **Signal: `id`, `canary`, `both`, or `null`.** It is the union across all planted fixtures of the
   attempt and is recorded only with `retrieved`. It matters because it shows how a match was made:
   on a private target whose source ids are chunk UUIDs that hold neither the document id nor the
   tag, the canary is the only signal, and the signal in the record makes that visible instead of
   hidden behind a rate.
7. **Placement.** One pure matcher (`grading/retrieval.py`) decides over the whole `Observation`
   (never over the escaped, cut ids the report stores); the runner calls it when it builds an
   `AttemptResult`, which keeps `source_ids` (the target's order, deduplicated, escaped and cut to
   120 characters; source texts stay out because they can be long and private), `retrieved` and
   `retrieval_signal`. The JSON report carries them per attempt inside `attempts[]` and has no
   per-item rollup. The Markdown report shows a per-item `Retrieved` count, with unknown states
   outside the denominator, only when some attempt has a known state. The run record keeps the same
   fields and stays `format: 1`: they are additive and optional, so a pre-M2.1 record loads and
   renders with `retrieved: null`.

## Consequences

- A blocked indirect attempt can now be read for what it is: the provider that never retrieved the
  document shows up as `not_retrieved` instead of looking like a control that worked.
- Nothing measured changes. The matcher is not a predicate, the outcome is computed as before, and
  the self-test rows are the same.
- The Markdown count is a count of attempts, not a rate: no interval, and unknown states don't enter
  the denominator, so a target that exposes no sources shows `—` rather than a misleading `0/n`.
- **Limits:**
  - A private target whose source ids carry neither the planted document id nor the run tag depends
    on the canary. If its chunking keeps the canary paragraph out of the retrieved chunk, the
    attempt reads `not_retrieved` while the model may have seen the poisoned text.
  - The bracketed form is the twin's convention. A target that prints documents another way is
    matched by source id and canary only.
  - Source ids are cut at 120 characters like every excerpt, so a very long id is not complete in
    the report; the state is decided over the uncut id.
  - The per-attempt data is what the reports keep. Anyone wanting a per-item rate works it out from
    `attempts[]`, where every state and signal is.

## Alternatives considered

- **The document id as the only signal.** Rejected: `scenario:` and `web:` fixtures have no document
  id, and a target's source ids need not contain one, so those attempts could never be `retrieved`.
- **`not_retrieved` whenever nothing matched.** Rejected: a fixture without a usable marker would
  produce a confident "not retrieved" with no evidence, which is the ambiguity M2.1 removes.
- **Storing the source texts.** Rejected: they are long and can hold private documents. The ids and
  the state answer the question the brief asks.
- **A per-item count in the JSON report, or a rate with an interval.** Rejected: this is a count of
  attempts with different states, not a rate, and attempts of one item are correlated (ADR 0006).
- **A new record format.** Rejected: the fields are additive and optional, so `format: 1` keeps
  pre-M2.1 records readable and needs no migration.
- **Matching over the escaped, cut `source_ids`.** Rejected: escaping and cutting can break an id
  that the target really returned, so the matcher reads the full `Observation`.
