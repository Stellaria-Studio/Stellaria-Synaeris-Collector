# Collector development contract

- This public repository owns only gameplay collection, post-recording candidate compilation, GUI and release/update tooling. Keep model topology, training code, checkpoints, teacher caches, private routes and gameplay data in their separate repositories or local data directories.
- Preserve the existing episode schema and mother video/event streams. Derived labels must retain evidence and qualification status; no OCR/teacher guess becomes verified Intent, reward, mouse producer or game receipt.
- Protect live recordings: updater downloads in the background, stages verified releases outside data directories, and activates only on a later launch. Never delete recorded episodes automatically.
- Portable EXE must launch directly with the appropriate Windows manifest. Windows UAC remains a normal user confirmation, not something to bypass.
- Before a release, run tests, a real encoding/decode self-test, a packaged build smoke when possible, update-path tests, and inspect staged public files for secrets or private model code.

## SEA 2 project knowledge

Use `sea_context` with this project's repository ID for a compact architecture
bootstrap, then expand only the concept/source/test needed for the task. Shared
runtime belongs to `Stellaria-Studio/Stellaria-SEA`; this repository owns
`.sea/` and `tools/sea/project.json`. The local CLI adapter is
`python3 tools/sea/stellaria-sea/scripts/sea.py context bootstrap`.
SEA is an optional knowledge runtime: no identity binding, approval workflow,
agent/model control or build prerequisite. Keep objectives and unfinished
requirements in local task capsules when useful. Source, actual tests and
product quality evidence remain authoritative; a knowledge query proves no
acceptance. See `.sea/README.md` for installation and legacy compatibility.
