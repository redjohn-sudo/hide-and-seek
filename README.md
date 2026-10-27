# Hide and Seek

An AI buyer agent for the **Tacto Negotiation Arena**. You are the procurement team of a fictional German pump maker; the agent reads the company's data, negotiates by e-mail with the arena's supplier bots, and closes the best total deal or walks away. Built by Team Pegasus, finalist at the EHL Grand Final in Munich.

<p align="center"><img src="demos/hide-and-seek.gif" width="640" alt="The buyer agent negotiating a supplier price increase down by e-mail, each argument backed by the contract and public price indices"></p>

## What I built

The agent lives in [`starter-kit/hide_and_seek/`](starter-kit/hide_and_seek/). The design: **the code decides every number** (total cost of ownership, concession steps, the relationship floor), a template writes the e-mail, and a guard checks each message before it is sent. One worker per supplier thread, decisions under a shared lock.

- `hide_and_seek/agent.py`, `hide_and_seek/play.py` — the negotiation loop and the per-thread workers.
- `hide_and_seek/economics.py`, `hide_and_seek/claims.py` — the numbers and the citable claims, each backed by the contract or a public price index.
- `hide_and_seek/guard.py` — the pre-send check on every outgoing message.
- `hide_and_seek/memory.py`, `hide_and_seek/reader.py` — supplier memory and document reading.
- [`starter-kit/hollerau/evidence/`](starter-kit/hollerau/evidence/) — the evidence layer that compiles the fictional company's case from its data.
- [`starter-kit/intel/`](starter-kit/intel/) — supplier dossiers.

Full agent notes: [`starter-kit/hide_and_seek/README.md`](starter-kit/hide_and_seek/README.md) and [`RUNBOOK.md`](starter-kit/hide_and_seek/RUNBOOK.md).

## Run

```sh
cd starter-kit
uv sync --extra sim
uv run pytest -q tests                        # kit + agent, offline
uv run python -m hide_and_seek.dryrun_final         # offline dry run against the mock arena
```

Live sessions need an arena key (`ARENA_API_KEY` in `.env`, never committed). The offline dry run and the tests need no key.

## Credit and license

This repository is built on the **Tacto Negotiation Arena** starter kit, published by **Tacto Technology GmbH** and credited here in full:

- The kit code (`starter-kit/arena_kit/`, `data/`, `brief/`, banner) is theirs, under the **MIT License** ([`LICENSE`](LICENSE)). Their original kit README is kept as [`README.arena.md`](README.arena.md).
- The data and documentation are under **CC BY 4.0** ([`LICENSE-data`](LICENSE-data)); sources are listed in [`data/ATTRIBUTION.md`](data/ATTRIBUTION.md). Attribution: "Tacto Negotiation Arena, Tacto Technology GmbH".

My own contribution (the `hide_and_seek/`, `hollerau/evidence/` and `intel/` code) is released under the same MIT License as the kit it extends.
