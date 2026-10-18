# Multitek UMKM Suite - WhatsApp NLU v0.7.10

This folder is a **source snapshot from a deployed system**, not a stand-alone installer.
The full service stack requires separately managed environment configuration,
Docker Compose, persistent databases and WhatsApp sessions (never committed).

- WhatsApp component: v0.7.10 (verified by SHA256 against running container)
- Telegram/core component installed alongside it: 0.7.6-boss-assistant
- Includes backend Python sources, migration sources, requirements, and Dockerfiles.
- Does **not** include .env, API keys, DB dumps, live transactions, session storage,
  Compose files with deployment secrets, worker volumes or backup directories.
- Do not run historical v0.7.4 installers against this folder without a separate audit.

See RELEASE_NOTES.md and SOURCE_SHA256SUMS.txt.
