# SPOILLESS

SPOILLESS is a spoiler-safe way to watch VALORANT VODs with round-by-round navigation, without scores, timelines, or tournament outcomes getting in the way.

Watch at [spoilless.vercel.app](https://spoilless.vercel.app/).

For the local persistent worker, double-click `Start Worker.cmd`. It starts the existing local PostgreSQL cluster if necessary, launches the worker in publish mode, and opens the authenticated status page at `http://127.0.0.1:8767/`. `Stop Worker.cmd` requests graceful worker shutdown and leaves PostgreSQL running. A manually started worker must be stopped once with Ctrl+C before switching to the launcher.

The persistent canonical YouTube broadcast pipeline, shadow migration, deployment and recovery commands are documented in [indexer/PIPELINE.md](indexer/PIPELINE.md). The legacy production publisher remains available during migration.
