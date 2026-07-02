# blooop npm wrapper

This package is a thin launcher for Blooop.

- It does **not** bundle Whisper/MLX runtime.
- It delegates to your installed local command (`~/.local/bin/blooop`).

## Install

```bash
npm install -g blooop
```

## Run

```bash
blooop
```

or

```bash
npx blooop --help
```

## Runtime support

- macOS on Apple Silicon (M1/M2/M3/M4)

## If runtime is not installed yet

```bash
git clone https://github.com/rumblelab/blooop
cd blooop
./setup.sh
blooop
```
