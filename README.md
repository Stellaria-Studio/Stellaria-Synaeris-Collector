# Synaeris Collector

Windows gameplay evidence recorder for the private Stellaria Synaeris research project. This repository owns the recorder, local OCR, BetterGI telemetry adapter, sealed episode format, and evidence-preserving post-recording compiler. It contains no Synaeris model, training code, weights, teacher cache, or private gameplay data.

## Team workflow

Download the latest `SynaerisCollector-Human-<version>.zip` from [Releases](https://github.com/Stellaria-Studio/Stellaria-Synaeris-Collector/releases), extract the complete folder, and double-click `SynaerisCollector.exe`. Accept the normal Windows UAC prompt if the game runs elevated. Open the game, click **开始采集**, play normally, and use **停止并保存** or F10. No per-action human labels are required. The app seals video and synchronized evidence, indexes candidate scenes, compiles Goal-aware decision sidecars, and verifies complete video decoding. The data stays on the local device by default.

The EXE checks the public GitHub latest release manifest in the background without a GitHub account or API quota. A newer stable ZIP is downloaded into `%LOCALAPPDATA%\Stellaria Synaeris Collector\updates`, checked against the published SHA-256 archive and executable hashes, and staged outside the recording directory. The current recording is never interrupted. The next time the team member opens the original EXE, it automatically starts the staged newer EXE. If offline, the current version keeps working. Old versions and all recordings are preserved.

For source development, run `tools\Setup.ps1`, then `Start-Collector.ps1`; run tests with `.venv\Scripts\python.exe -m pytest -q`. Build the portable EXE with `tools\Build-Collector.ps1` and package a release with `tools\Package-Human-Collector.ps1 -Version 0.6.1`. Upload both the versioned ZIP and `dist/releases/collector-update.json` to one stable GitHub Release; the latter must always belong to the latest version. The build requests elevation through its Windows manifest so the EXE itself is the launch entry point.

Data and provenance details are in [DATA_SCHEMA.md](docs/DATA_SCHEMA.md); short user instructions are in [HUMAN_COLLECTOR.md](docs/HUMAN_COLLECTOR.md). Automatic OCR, scene, Goal and effect candidates remain weak until independently reviewed. Old footage cannot reveal a verified mouse producer, the player's Intent, or whether the game accepted a particular input.
