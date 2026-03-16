# Contributing to KaiView

Thanks for your interest in improving KaiView!

## Ways to contribute

- **New stack detectors** — add detection logic in `detect_stack()` in `server.py`
- **UI improvements** — all frontend is in `index.html` (vanilla JS, no build step)
- **Bug fixes** — open an issue first for significant changes
- **Documentation** — improve README, add examples

## Setup

```bash
git clone https://github.com/Kabi10/kaiview.git
cd kaiview
pip install -r requirements.txt
python server.py
```

## Code style

- Python: follow PEP 8, keep functions short and focused
- JavaScript: vanilla ES2020+, no frameworks, no build step
- Keep the single-file architecture (`server.py` + `index.html`)

## Submitting a PR

1. Fork the repo
2. Create a branch: `git checkout -b feat/my-feature`
3. Make your changes and test locally
4. Open a pull request with a clear description

## Adding a stack detector

In `server.py`, `detect_stack()` checks files in the project root. Example:

```python
if "Cargo.toml" in files:
    stack.append("Rust")
```

## Issues

Use GitHub Issues for bug reports and feature requests. Include:
- OS and Python version
- Steps to reproduce
- Expected vs actual behaviour
