# Bloop Product Map

## Recommended naming

- Toolkit brand: **Bloop**
- Dictation app: **Bloop Flow**
- Terminal notifier: **Bloop Ding**

## Why this naming

- "Flow" clearly signals speaking/coding flow state.
- "Ding" is short, memorable, and functionally obvious.
- Both names feel cohesive under `bloop.lol`.

## Repo layout (current)

- `bloop.py`, `run.sh`, `setup.sh`: current `bloop_flow` implementation.
- `apps/bloop-ding/`: placeholder for the notifier app.
- `web/bloop-lol/`: placeholder for site HTML/assets.

## Release approach

- Short term: ship `bloop_flow` directly from this repo.
- Next: add `bloop_ding` as sibling app.
- Then: wire both apps into `bloop.lol` download + docs UX.
