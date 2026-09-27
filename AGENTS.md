# Collector development contract

- This public repository owns only gameplay collection, post-recording candidate compilation, GUI and release/update tooling. Keep model topology, training code, checkpoints, teacher caches, private routes and gameplay data in their separate repositories or local data directories.
- Preserve the existing episode schema and mother video/event streams. Derived labels must retain evidence and qualification status; no OCR/teacher guess becomes verified Intent, reward, mouse producer or game receipt.
- Protect live recordings: updater downloads in the background, stages verified releases outside data directories, and activates only on a later launch. Never delete recorded episodes automatically.
- Portable EXE must launch directly with the appropriate Windows manifest. Windows UAC remains a normal user confirmation, not something to bypass.
- Before a release, run tests, a real encoding/decode self-test, a packaged build smoke when possible, update-path tests, and inspect staged public files for secrets or private model code.
