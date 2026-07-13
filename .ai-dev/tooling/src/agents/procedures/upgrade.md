# Procedure: upgrade

Loaded on demand when the upgrade check fires (the trigger line lives in
`orchestrator.md` `## Side-tools`).

`upgrade` executes the protocol's per-version migration notes after a tooling bump. The mechanical half is the installer's: it stamps `.ai-dev/VERSION` on every run and, on a version change, writes the transient marker `.ai-dev/UPGRADING.md` and lays the notes at `.ai-dev/upgrades.md`. Noticing, offering, executing, and deleting are `[persona]`. Side-tool, not a beat.

**When it fires:** on the understand beat when `.ai-dev/UPGRADING.md` exists ⇒ a short declinable offer ("protocol upgraded X → Y — run the migration check?"); declined ⇒ the marker stays and the offer re-fires next session. Explicit on the Operator's ask.

**One pass:** read the marker + the `(old, new]` sections of `.ai-dev/upgrades.md`; execute the applicable notes through the normal loop — a migration is a feature (fixup-grade for a mechanical rename, the full loop where docs need redrafting); nothing applicable ⇒ say so. Delete the marker **LAST**, after the migration ships; record the upgrade in the state pointer.
