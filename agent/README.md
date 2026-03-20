# Synced Runtime Copy

This directory is a synced runtime copy of the Proton9 backend used by `PrimeForgeIDE/`.

## Ownership

- Canonical source lives in `Proton9/`
- This folder may be overwritten by `Proton9/build_pide.ps1`
- Make long-term backend changes in `Proton9/`, then sync them here

## Use This Folder For

- launching the IDE-hosted Python backend
- verifying the synced runtime layout
- debugging issues specific to the IDE deployment copy

## Avoid

- treating this folder as the primary authoring location for backend work
- making durable backend edits here without mirroring them back to `Proton9/`
