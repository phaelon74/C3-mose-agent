# Contributing to Mose Agent

Thanks for your interest in contributing! Mose Agent is a small, focused project and we want to keep it that way.

## Development Setup

```bash
git clone https://github.com/phaelon74/C3-luna-agent.git
cd C3-luna-agent
python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
```

## Running Tests

```bash
pytest tests/ -v
```

All 106 tests should pass. Tests don't require a GPU or running LLM server — they mock the LLM client.

## Code Style

- No frameworks — keep dependencies minimal
- Standard library over third-party when reasonable
- Type hints on public functions
- Keep files under 500 lines

## Making Changes

1. Fork the repo and create a branch from `main`
2. Make your changes
3. Add tests for new functionality
4. Run `pytest tests/ -v` and ensure all tests pass
5. Submit a pull request

## Pull Request Process

- Keep PRs focused — one feature or fix per PR
- Write a clear description of what changed and why
- Reference any related issues

## Architecture

See [DESIGN.md](DESIGN.md) for architectural decisions and rationale. New features should follow the existing patterns described there.

## Reporting Bugs

Use [GitHub Issues](https://github.com/phaelon74/C3-luna-agent/issues) with the bug report template.

## Security

See [SECURITY.md](SECURITY.md) for reporting security vulnerabilities.
