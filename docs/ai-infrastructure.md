# AI infrastructure for iNTUition

Status: recommendation. Supersedes the single `HOUSE_MODEL` proposal in
[compendium.md](compendium.md), which was wrong in a way worth recording.

## The correction

The Compendium spec proposed one house model for every AI surface, on the grounds that
consistency needs one voice. Pointing Ask FRIDAY at free OmniRoute routes disproves it:
a chat turn and a revision note have genuinely different requirements, and no single model
satisfies both.

* A **chat turn** is interactive, disposable, and retried freely. It wants low latency and
  near-zero cost. If it comes back wrong you ask again.
* A **summary note** is compiled, filed, and revised from for months. It wants rigour and
  reproducibility. If it comes back wrong you may not find out until the exam.

Those are opposite priorities. The right abstraction is not one model, it is a small set
of **tiers**, each with a declared requirement, and a **ladder** that walks candidate
routes until one produces a usable answer.

Consistency is preserved where it actually matters - within the scholar tier - rather than
smeared across surfaces that never needed it.

## Historical benchmark notes (non-authoritative)

The following comparison is a historical design exercise, not a current service guarantee. Route availability, model catalogues, latency, and output quality change over time; rerun the same measurement procedure in the target environment before relying on these figures.

### Single-shot comparison

| Route | Served | Latency | Chars | Notation | Cites page |
|---|---|---|---|---|---|
| `auto/coding:free` | claude-haiku-4.5 | 24.2 s | 1821 | correct | no |
| `auto/best-free` | minimax-m2.5 | 23.0 s | 1563 | correct | no |
| `auto/cheap` | gpt-5.3-codex-spark | 28.2 s | 1705 | correct | yes |
| `auto/best-chat` | gpt-5.3-codex-spark | 12.6 s | 1668 | correct | no |
| `cl/google/gemma-4-31b-it:free` | gemma-4-31b | 8.6 s | 850 | correct | no |
| `claude/claude-sonnet-5` | claude-sonnet-5 | 11.5 s | 2305 | correct | yes |
| `claude/claude-opus-5` | claude-opus-5 | 15.7 s | 1628 | correct | yes |
| `felo/felo-scholar` | felo-scholar | 13.8 s | **1** | — | — |

### Repeated runs, five concurrent each

| Route | Usable | Median | Worst | Served |
|---|---|---|---|---|
| `claude/claude-sonnet-5` | 5/5 | 7.3 s | 8.5 s | 5x claude-sonnet-5 |
| `auto/fast` | 5/5 | 9.7 s | 10.8 s | 5x gpt-5.3-codex-spark |
| `auto/coding:free` | 5/5 | 10.5 s | 11.6 s | 5x claude-haiku-4.5 |
| `auto/best-free` | 5/5 | 28.7 s | 31.6 s | 5x gpt-5.3-codex-spark |
| `auto/cheap` | **0/5** | timeout | timeout | 5x TimeoutError (150 s) |

Findings that changed the design:

**Free is viable.** Both free routes returned usable, correctly-notated answers on every
run. The `\( ... \)` convention in the FRIDAY system prompt survived every model tested,
which matters because KaTeX rendering depends on it and dollar-delimiter leakage would
break the panel.

**`auto/coding:free` is the better free route, by a wide margin.** 10.5 s median against
28.7 s, and it resolved to claude-haiku-4.5 on all five runs where `auto/best-free` landed
on a slower model. The label is about the routing pool, not task specialisation. Worth
knowing that the pool can shift under the alias - which is what the degeneracy guard is
for.

**`auto/cheap` must not be used.** It answered the single-shot probe in 28.2 s and then
failed all five repeat runs, every one hitting the 150-second ceiling. The repeats were
issued concurrently, so this may be intolerance of five simultaneous requests rather than
baseline slowness - but the dashboard does issue concurrent calls, and a route that
collapses under mild concurrency is not a route to put in a ladder. It was on the first
draft of the `bulk` tier and has been removed. This is the clearest argument for measuring
repeats: one sample would have shipped it.

**The paid fallback is faster than every free route.** Sonnet's 7.3 s median beats
`auto/fast` at 9.7 s and `auto/coding:free` at 10.5 s. Falling through the ladder
therefore costs money but never costs time, which makes a tight first-rung deadline cheap
to set.

**`felo/felo-scholar` returned exactly one character: `.`** This confirms, on a second
surface, the degeneracy `ai_provider.is_degenerate` already guards against - the
search-shaped `felo/*` models answer a reasoning prompt with a single character, and the
provider's own "no text" guard passes it. Any route reaching that pool needs the guard.

**Every Google path is currently broken.** `auto/gemini`, all `antigravity/*` and all
`agy/*` models return HTTP 422:

> Missing Google projectId for Antigravity account. Auto-discovery via loadCodeAssist
> found no Cloud Code project. Please reconnect OAuth.

`tllm/*` Gemini models return 403 (provider blocked for this egress IP). So Gemini is
present in the catalogue in some 40 variants and none of them can currently answer.

**This means the to-do panel's model dropdown lies.** `dashboard.py:79-80` offers
"Gemini 3.1 Pro" and "Gemini 3.5 Flash" as selectable options. Choosing either fails with
a 422 the user cannot interpret. Model options must be filtered against a live health
check rather than hard-coded - a separate small bug this investigation turned up.

## The tiers

| Tier | Surfaces | Requirement | Ladder (OmniRoute) |
|---|---|---|---|
| `CHAT` | Ask FRIDAY | fast, cheap, disposable | `auto/coding:free` → `auto/fast` → `claude/claude-sonnet-5` |
| `SCHOLAR` | Compendium, Research | rigorous, reproducible, cited | `claude/claude-opus-5` **only** |
| `BULK` | triage, announcements, to-do enrichment | high volume, structured, low stakes | `auto/coding:free` → `auto/fast` |
| `VISION` | lasso snapshots | multimodal | `auto/best-vision` |

`auto/coding:free` leads the chat ladder rather than the marginally faster `auto/fast`
because it resolved to claude-haiku-4.5 on every run while `auto/fast` resolved to a
coding model. For a surface that reads lecture material, a 0.8 s difference is worth less
than the better reader.

The rule that gives the whole scheme its meaning:

> **The scholar tier never ladders.** If the pinned model is unavailable, Compendium and
> Research fail loudly rather than quietly producing a summary from a different model. A
> revision note whose provenance is "whichever route was up" is exactly the artefact the
> rigour requirements exist to prevent.

Everything else ladders freely, because a chat turn that falls through to a different
model has cost the user nothing but a few seconds.

## The ladder

```python
# ai_provider.py

class Attempt(NamedTuple):
    model: str
    deadline: float          # seconds; a slow rung is a failed rung


# Ladders are written in OmniRoute's vocabulary. On the CLI and API backends these names
# mean nothing, so each tier collapses to that backend's single equivalent - the same
# translation problem HOUSE_MODEL solved, now per tier rather than globally.
TIERS = {
    "chat":    [Attempt("auto/coding:free", 25), Attempt("auto/fast", 25),
                Attempt("claude/claude-sonnet-5", 60)],
    "scholar": [Attempt("claude/claude-opus-5", 300)],
    "bulk":    [Attempt("auto/coding:free", 45), Attempt("auto/fast", 45)],
    "vision":  [Attempt("auto/best-vision", 60)],
}


def complete_tier(tier, prompt, system, **kw):
    """Walk the tier's ladder; the first usable answer wins.

    A rung fails if it errors, exceeds its deadline, or returns a degenerate answer.
    The returned dict names the rung that answered, because a caller that files the
    result - a summary, a recall card - has to be able to record who wrote it.
    """
```

Three things this has to get right:

**One shared degeneracy guard.** `ai_provider.is_degenerate` - fewer than 20 characters is a
routing failure, not an answer - applies on every rung of every ladder. It currently
protects one surface. The free routes make it necessary everywhere, and `felo-scholar`
returning `.` is the proof.

**Deadlines are per rung, not per request.** A rung that stalls has failed even if it
would eventually answer, because the next rung is under 10 s away. Measured medians make
these numbers concrete rather than guessed: 25 s on a route with a 10.5 s median and an
11.6 s worst case is generous without being useless, and `auto/cheap` would have been
caught by it on the first run rather than hanging for 150 s.

**The rung that answered is reported.** The existing `materialMeta` line already prints
backend and model; it should distinguish "answered on the free route" from "fell through
to Sonnet". Users tolerate a cheap default; they do not tolerate not knowing which model
produced the note they are revising from.

## Google / Gemini integration

**The fix is configuration, not code.** OmniRoute reaches Gemini through an Antigravity
OAuth connection that is missing its Cloud Code project id. Reconnecting it is an
interactive OAuth flow and needs your OmniRoute API key:

```
omniroute oauth status   --api-key <key>          # see what is connected
omniroute oauth start    --provider antigravity   # reconnect, pick a GCP project
```

The gateway's management endpoints return 401 without that key, so this has to be run by
you rather than from here - and the OAuth sign-in itself is yours to complete regardless.

**Once it works, Gemini earns two places** and not a third:

* **The `chat` ladder.** `antigravity/gemini-3.6-flash-medium` and the flash-lite variants
  are free through the Antigravity account and built for latency. They belong on the first
  rung alongside `auto/coding:free`, chosen between on measured median once they answer.
* **The `vision` tier.** Lasso snapshots currently go to `auto/best-vision`, and the
  gemini flash models are strong and cheap at that.
* **Not the scholar tier.** Compendium stays pinned to Opus. Gemini's million-token context
  is genuinely useful for Compendium's ring-3 whole-course map-reduce, but as a *retrieval*
  pass that extracts anchored claims which Opus then composes into the document. The author
  of a revision note stays fixed even when the reader is not.

**Add a Gemini line to the provider status panel.** The dashboard already surfaces
OmniRoute health. A 422 that silently removes 40 models from the catalogue should be
visible, not discovered by selecting a dropdown option that fails.

## What to build, in order

1. **`is_degenerate` moves to `ai_provider`** and is applied on every completion. Smallest
   change, protects everything, needed before free routes go anywhere near a user.
2. **`Attempt` / `TIERS` / `complete_tier`.** Pure logic, testable with a fake completer:
   assert that a degenerate first rung falls through and that the scholar tier does not.
3. **Point Ask FRIDAY at `CHAT`**, deleting its hand-rolled conditional.
4. **Report the answering rung** through to `materialMeta`.
5. **Filter the to-do model dropdown against live health**, fixing the Gemini options that
   currently fail on selection.
6. **Reconnect the Antigravity OAuth**, then add the Gemini flash models to the `chat` and
   `vision` ladders on measured latency.
7. **Compendium builds on `SCHOLAR`**, per [compendium.md](compendium.md), unchanged apart
   from calling `complete_tier("scholar", ...)` instead of a house model.

Steps 1-4 are worth doing whether or not Compendium is built. They fix a live problem: Ask
FRIDAY currently answers on Haiku or a stochastic route by accident, with no guard and no
disclosure.
