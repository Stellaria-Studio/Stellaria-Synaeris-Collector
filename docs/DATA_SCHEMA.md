# Collector data format

Each sealed Episode contains `visual.mkv`, `manifest.json`, frame-to-video timing, and typed Parquet event streams for input, OCR, UI, pose, tools, observations and performance. All event timestamps are Episode-relative QPC nanoseconds and carry a sequence number. The manifest records capture configuration, availability and provenance. Incomplete, synthetic, unfocused or otherwise unqualified observations remain explicit.

After sealing, `auto_index.json`/`.html` contain weak scene and text candidates. `decision_units.v1.jsonl` and its `.audit.json` are recomputable sidecars; each variable-duration unit records `Goal → World State → Affordance → Intent → Expected Effect → Action → Observed Effect → Progress → Outcome`, with per-field status, source evidence and training weight. Source video, frame map, input, index and manifest hashes bind the sidecar to the mother recording.

Automatic OCR and scene scores are not calibrated probabilities. Physical OS hook events do not establish that the game received an input. Historical Raw Input mouse packets do not establish their human/BGI producer. Old video cannot reconstruct an independently verified Intent, Expected Effect or task Outcome. Candidate semantic supervision remains weight zero until reviewed and admitted outside this recorder.

The default local data path is `%LOCALAPPDATA%\Stellaria Synaeris\data\episodes`; the updater uses a distinct `%LOCALAPPDATA%\Stellaria Synaeris Collector\updates` directory and never edits the recording tree.
