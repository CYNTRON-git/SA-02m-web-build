# Architecture

> The engineer's mental model of `<project>` — how the pieces fit and where to change them. Current state only; the *why* of a past decision lives in git. Readable in one sitting. *Delete this line when filling.*

## What it is

<2–3 sentences: what the system does, its main parts, the one core idea.>

## Components & data flow

<the main components and how a request or data flows through them — a short ASCII diagram beats prose.>

## Behavioral contract (taxonomies & invariants)

<the machine grammars the product promises depend on: id / name formats, enums, value ranges, topic conventions. The single home — `contracts.md` references this, never restates it. Delete if the project has none.>

## Security surface

<auth, secrets, untrusted-input and network boundaries — what each is and how it is guarded. Delete if none.>

## Operational limits & budgets

<the engineering bounds behind any user-facing limit: memory ceiling, max-N, throughput. A bound the team hasn't measured is `[?]` — never invented.>

## Extension points

<how to add the next thing of each kind — by the logic of the build, not a static file listing.>

## Decisions

<one line per significant architectural decision; the *why* detail stays in git. Supersede a line in place when its decision changes.>
