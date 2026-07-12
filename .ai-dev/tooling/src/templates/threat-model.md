# Threat model

> The one home for **who attacks this product and what they can take** — seeded as a short sketch at inception or doc bootstrap, deepened by threat discovery (`src/agents/procedures/threat-discovery.md`), kept current; a security-relevant feature plan cites the named actor / boundary / asset it touches.
>
> Filled gather-first, conclude-last: record what the brief, the tree, and the Operator give; an unassessed axis is `[?]` — never an invented threat, never a graded answer. Current state only — supersede a changed assessment, don't accumulate. ~40–80 lines of normal prose. Secret *locations*, never values. The conclusion (§6) comes at the end.
>
> *Delete this guidance block when filling.*

## 1. Actors — who attacks, and why

`<the adversaries this product realistically faces — opportunist (hits whatever is open), targeted (wants THIS product or its users), insider (holds legitimate access), automated (bots, scrapers, credential stuffing). Per actor: what they are after and how hard they will try. "Nobody would bother" is a §6 conclusion to earn, not a reason to skip the question.>`

## 2. Assets — what is worth taking or breaking

`<what an actor gains here: data (whose, how sensitive), access (accounts, keys, the deploy path), money (payments, resources billed to the product), reputation (abuse sent in the product's name). Name where each lives — a secret's location, never its value.>`

## 3. Trust boundaries — where untrusted meets trusted

`<each point where outside input crosses into code that trusts it: public endpoints, uploads, webhooks, third-party integrations, the user↔admin seam. Per boundary: what crosses, and what validates it AT the line.>`

## 4. Abuse cases — the system as designed, turned against the user

`<how a real feature is used hostilely WITHOUT breaking anything: spam through invites, harassment through messaging, scraping through the public API, fraud through refunds. Features that move value or reach other users are the ones worth abusing. Genuinely none is answered "N/A — <why>" (a recorded answer), never silently skipped. Example (a configurable-guard product): disabling a safeguard removes its protection — mitigation: the un-disablable protections are the deny-class mechanical floor (never config-toggleable, default-on); each loosenable guard is a conscious, recorded opt-in, never a silent default.>`

## 5. Consciously out of scope

`<the threats this product ACCEPTS, each with its reason — "nation-state attackers: not our adversary class", "physical theft of a user's device: the platform's problem". An accepted risk is a recorded decision; a threat merely never considered belongs in §6 as [?], not here.>`

## 6. Currently exposed *(the conclusion — at the end)*

`<having gathered all the above: the strongest UNMITIGATED threat — the actor, the asset, the open path — named honestly. "This is currently exposed" is a legal verdict and the reason this document exists; a threat model that cannot reach it is theater. List what was never assessed as [?].>`
