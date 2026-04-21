"""Run during image build before pip: normalize pyproject.toml and fail with hints."""
from __future__ import annotations

import pathlib
import tomllib

ROOT = pathlib.Path(__file__).resolve().parent.parent
PYPROJECT = ROOT / "pyproject.toml"


def main() -> None:
    if not PYPROJECT.is_file():
        raise SystemExit(
            "pyproject.toml is missing from the build context. "
            "Confirm the clone is complete and .dockerignore does not exclude it."
        )
    raw = PYPROJECT.read_bytes()
    if raw.startswith(b"\xef\xbb\xbf"):
        raw = raw[3:]
        PYPROJECT.write_bytes(raw)
    try:
        text = raw.decode()
    except UnicodeDecodeError as e:
        raise SystemExit(
            "pyproject.toml is not valid UTF-8 (often UTF-16 if saved from some Windows editors). "
            "Re-save the file as UTF-8 in the repo on the build host, then rebuild."
        ) from e
    stripped = text.lstrip()
    if stripped.startswith("version https://git-lfs.github.com/spec/v1"):
        raise SystemExit(
            "pyproject.toml is a Git LFS pointer (first line looks like an LFS version header). "
            "On the host: install git-lfs, run `git lfs install` and `git lfs pull` in this repo, "
            "then confirm `head -n1 pyproject.toml` shows `[project]` before rebuilding."
        )
    try:
        tomllib.loads(text)
    except tomllib.TOMLDecodeError as e:
        raise SystemExit(
            f"pyproject.toml is not valid TOML ({e}). "
            f"First 80 bytes (repr): {raw[:80]!r}. "
            "Typical causes: file saved as UTF-16 on Windows, truncated copy, or corrupt git checkout."
        ) from e


if __name__ == "__main__":
    main()
