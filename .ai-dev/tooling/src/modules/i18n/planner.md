## i18n

The **i18n** module is on. The floor names no locale dimension — this module ADDS it:
where the project serves (or plans) more than one locale, the plan covers the rows
below; on a single-locale project this module is inert. Hardcoded strings, locale-blind
dates, and a layout that breaks on +30% German are the failures this prevents.
`[persona]`: this sharpens the plan, denies nothing.

- `[light]` **Strings externalized** — user-facing strings live in the project's i18n mechanism, never hardcoded — and never assembled by concatenation: grammar differs per language.
- `[light]` **Locale-aware formatting** — dates, numbers, and currency go through locale APIs, never hand-built format strings.
- `[light]` **UTF-8 throughout** — one encoding end to end; a mixed-encoding path corrupts the first non-ASCII name it meets.
- `[rich]` **Plural rules per language** — plural forms come from the locale's plural rules, not a hardcoded singular/plural pair; many languages have more than two forms.
- `[rich]` **Expansion-tolerant layouts** — layouts tolerate text expansion (German runs ~30% longer); no width assumption pinned to the source language.
- `[rich]` **RTL named where targeted** — a right-to-left target locale is named in the plan, and the surface handles direction, not just translation.
