# Released GitHub producer fixture

`event-journal-github-generator-5e367530.py` is an immutable historical test
input, not the current production deployment map.

- Source commit: `5e367530de3ab283e1060c32017091bdf75b25e3`.
- Source path: `etc/sa02m-update-runner.sh`.
- Source range: Python body on lines 680–982 (following the heredoc command
  on line 679), excluding the following newline and `PY` terminator.
- Size: 15179 bytes.
- SHA256: `29e4484a710901667eddfed70a0e74f74e8e89e602c9de93668237b345a01249`.

Extracted from `git show` captured as bytes, with no newline conversion. The
delivery harness checks this digest and executes the complete generator on
the candidate payload. It needs neither a network fetch nor repository history
at test time. The live runner/map/packer remain the production homes.
