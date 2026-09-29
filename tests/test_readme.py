"""The README's YAML examples must stay valid: each block loads through ``load_config`` and every
source in it passes its collector's ``validate``. Together the blocks show every option of the
imap, changedetection and web sources (so the docs cannot drift from the real option names)."""

from __future__ import annotations

import re
from pathlib import Path

from since.config import SourceConfig, load_config
from since.model import SOURCE_TYPES
from since.sources import get_collector
from since.sources.changedetection import _ALLOWED_OPTIONS as CHANGEDETECTION_OPTIONS
from since.sources.imap import _ALLOWED_OPTIONS as IMAP_OPTIONS
from since.sources.web import _KNOWN_OPTIONS as WEB_OPTIONS

README = Path(__file__).resolve().parents[1] / "README.md"
BLOCK = re.compile(r"^```yaml\r?\n(.*?)^```", re.DOTALL | re.MULTILINE)


def readme_sources(tmp_path: Path) -> list[SourceConfig]:
    blocks = BLOCK.findall(README.read_text(encoding="utf-8"))
    assert len(blocks) >= 4, "the README should have YAML examples"
    sources: list[SourceConfig] = []
    for number, text in enumerate(blocks):
        path = tmp_path / f"block{number}.yaml"
        path.write_text(text, encoding="utf-8")
        sources.extend(load_config(path).sources)
    return sources


def test_every_yaml_block_in_the_readme_loads_and_validates(tmp_path: Path) -> None:
    sources = readme_sources(tmp_path)
    for cfg in sources:
        get_collector(cfg.type).validate(cfg)
    assert {cfg.type for cfg in sources} == set(SOURCE_TYPES)


def test_the_readme_examples_show_every_option_of_the_new_sources(tmp_path: Path) -> None:
    used: dict[str, set[str]] = {}
    for cfg in readme_sources(tmp_path):
        used.setdefault(cfg.type, set()).update(cfg.options)
    assert used["imap"] >= set(IMAP_OPTIONS)
    assert used["changedetection"] >= set(CHANGEDETECTION_OPTIONS)
    assert used["web"] >= set(WEB_OPTIONS)
