# Arkpedia Regional and alternate voice assets

Operator voice clips used by [Arkpedia](https://github.com/Arkpedia/arkpedia). Current clips live under `current/`. Superseded recordings live under `legacy/<source commit>/`; the commit identifier preserves exact provenance when upstream files are replaced.

Current files are mirrored from the `voice` branch of [ArknightsAssets/ArknightsAssets2](https://github.com/ArknightsAssets/ArknightsAssets2). Legacy files were imported from source-pinned snapshots of [PseudoMon/arknights-audio](https://github.com/PseudoMon/arknights-audio).

Arknights and its audio are property of Hypergryph, Yostar, and their respective rightsholders. Publishing this repository does not grant a license to reuse or redistribute these assets. Open an issue for an incorrect file, attribution correction, or takedown request.

### Sync safeguards

Daily sync runs on standard public GitHub runners with a 20-minute timeout and no uploaded artifacts or upstream cache. Every changed MP3 must match its upstream Git blob and decode successfully with FFmpeg before publication. Deletions and unexpectedly large changes stop for review, preserving previous/unused recordings. Weekly grouped Dependabot PRs maintain Actions versions.
