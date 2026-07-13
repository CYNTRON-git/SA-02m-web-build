## Modularity

The **modularity** module is on, so the plan's scope check is deepened from
"what files change" to a **boundary pass**: where the change touches a module
interface or introduces a new inter-module dependency, the plan must name the
boundary and confirm the direction is intentional. An unexamined cross-boundary
dependency added silently is the defect this catches early. Where the project
carries a `docs/architecture.md` with module/layer names, cite the boundary there
instead of re-describing it. `[persona]`: this sharpens the plan, denies nothing.

- `[light]` **Boundary named** — does this change touch a module interface described in `docs/architecture.md`? Name it; if the boundary is undocumented, surface that as a plan decision (document it now, or record the gap).
- `[light]` **Dependency direction** — does the change introduce a new inter-module dependency? Confirm it follows the direction rules in `docs/architecture.md`; a dependency against the grain is a structural choice the Operator approves, not an implementation detail.
- `[rich]` **Linter gap** — if the project has no dependency/boundary linter registered in `src/quality/tools.json`, note it: a linter makes this check mechanical at every future diff (`dependency-cruiser` for JS/TS, `import-linter` for Python). Propose it as a setup follow-up if absent.
- `[rich]` **Cohesion check** — does the change scatter responsibilities across modules that were previously separate? Name any cohesion erosion explicitly; a "small" cross-boundary leak compounds.
