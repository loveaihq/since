"""Tests for paths and config loading/validation."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from since import paths
from since.config import (
    Config,
    ConfigError,
    HighlightRule,
    SourceConfig,
    load_config,
    parse_config,
)

CREDENTIALS_MSG = "credentials must come from an env var (url_env) or OS keyring, never YAML"

# The example from CLAUDE.md "Config example".
CLAUDE_MD_EXAMPLE = """\
sources:
  - id: po-table
    type: sql
    priority: high
    schedule: every 15m
    url_env: SINCE_PO_DB_URL        # credentials only via env var or OS keyring, never in YAML
    query: "select po_no, status, eta from purchase_orders"
    key: [po_no]
    track_fields: [status, eta]
    highlight: [{field: status, changed_to: Cancelled}]
  - id: sps-portal
    type: web
    priority: high
    schedule: every 30m
    url: https://example.invalid/orders
    profile_dir: ~/.since/profiles/sps   # Playwright persistent profile
    login_detect: {url_contains: /login}
    extract:
      rows: "table#orders tbody tr"
      key: po
      fields: {po: "td:nth-child(1)", status: "td:nth-child(4)"}
"""


def write(tmp_path: Path, text: str, name: str = "since.yaml") -> Path:
    p = tmp_path / name
    p.write_text(text, encoding="utf-8")
    return p


def one(**overrides) -> dict:
    src = {"id": "docs", "type": "dir", "path": "/tmp/docs"}
    src.update(overrides)
    return src


# --- paths -----------------------------------------------------------------------------------


def test_autouse_fixture_points_since_home_at_tmp(since_home_dir, tmp_path):
    assert paths.since_home() == since_home_dir
    assert tmp_path in since_home_dir.parents
    assert paths.db_path() == since_home_dir / "since.db"
    assert paths.config_path() == since_home_dir / "since.yaml"


def test_since_home_defaults_to_dot_since_in_home(monkeypatch):
    monkeypatch.delenv("SINCE_HOME", raising=False)
    assert paths.since_home() == Path.home() / ".since"


def test_since_home_expands_user(monkeypatch):
    monkeypatch.setenv("SINCE_HOME", "~/somewhere")
    assert paths.since_home() == Path.home() / "somewhere"


# --- loading ---------------------------------------------------------------------------------


def test_claude_md_example_loads(tmp_path):
    cfg = load_config(write(tmp_path, CLAUDE_MD_EXAMPLE))
    assert cfg.retention_days == 30
    assert [s.id for s in cfg.sources] == ["po-table", "sps-portal"]

    po = cfg.sources[0]
    assert po == SourceConfig(
        id="po-table",
        type="sql",
        priority="high",
        schedule_s=900,
        track_fields=["status", "eta"],
        highlight=[HighlightRule(field="status", op="changed_to", value="Cancelled", bonus=10)],
        options={
            "url_env": "SINCE_PO_DB_URL",
            "query": "select po_no, status, eta from purchase_orders",
            "key": ["po_no"],
        },
    )

    web = cfg.sources[1]
    assert (web.type, web.priority, web.schedule_s) == ("web", "high", 1800)
    assert web.track_fields is None
    assert web.highlight == []
    assert web.options["url"] == "https://example.invalid/orders"  # url is fine on non-sql
    assert web.options["extract"]["key"] == "po"


def test_default_path_is_since_yaml_in_since_home(since_home_dir):
    since_home_dir.mkdir(parents=True)
    text = "sources:\n  - {id: a, type: dir}\n"
    (since_home_dir / "since.yaml").write_text(text, encoding="utf-8")
    assert [s.id for s in load_config().sources] == ["a"]


def test_missing_file_names_the_path(tmp_path):
    missing = tmp_path / "nope" / "since.yaml"
    with pytest.raises(ConfigError) as exc:
        load_config(missing)
    assert str(missing) in str(exc.value)


def test_missing_default_file_names_the_path(since_home_dir):
    with pytest.raises(ConfigError) as exc:
        load_config()
    assert str(since_home_dir / "since.yaml") in str(exc.value)


def test_invalid_yaml_is_config_error(tmp_path):
    with pytest.raises(ConfigError, match="invalid YAML"):
        load_config(write(tmp_path, "sources: [unclosed"))


def test_bom_is_tolerated(tmp_path):
    p = tmp_path / "since.yaml"
    p.write_bytes(b"\xef\xbb\xbfsources:\n  - {id: a, type: dir}\n")
    assert load_config(p).sources[0].id == "a"


def test_empty_file_and_empty_sources_are_valid(tmp_path):
    assert load_config(write(tmp_path, "")) == Config(sources=[], retention_days=30)
    assert load_config(write(tmp_path, "sources: []\n")).sources == []


def test_top_level_must_be_mapping(tmp_path):
    with pytest.raises(ConfigError, match="mapping"):
        load_config(write(tmp_path, "- a\n- b\n"))


def test_sources_must_be_list():
    with pytest.raises(ConfigError, match="sources"):
        parse_config({"sources": {"id": "a"}})


def test_error_from_file_includes_path(tmp_path):
    p = write(tmp_path, "sources:\n  - {id: a, type: bogus}\n")
    with pytest.raises(ConfigError) as exc:
        load_config(p)
    assert str(p) in str(exc.value)


# --- defaults --------------------------------------------------------------------------------


def test_defaults():
    cfg = parse_config({"sources": [{"id": "docs", "type": "dir", "path": "/x"}]})
    src = cfg.sources[0]
    assert src.priority == "normal"
    assert src.schedule_s == 900
    assert src.track_fields is None
    assert src.highlight == []
    assert src.options == {"path": "/x"}
    assert cfg.retention_days == 30


def test_retention_days_configurable_and_validated():
    assert parse_config({"retention_days": 7}).retention_days == 7
    for bad in (0, -1, "30", 1.5, True):
        with pytest.raises(ConfigError, match="retention_days"):
            parse_config({"retention_days": bad})


def test_all_source_types_accepted():
    for t in ("dir", "sql", "imap", "web", "changedetection"):
        src = parse_config({"sources": [{"id": "s", "type": t}]}).sources[0]
        assert src.type == t


# --- validation errors -----------------------------------------------------------------------


def test_id_missing_or_not_string():
    with pytest.raises(ConfigError, match=r"sources\[0\].*'id'"):
        parse_config({"sources": [{"type": "dir"}]})
    with pytest.raises(ConfigError, match=r"sources\[1\].*'id'"):
        parse_config({"sources": [one(), {"id": 5, "type": "dir"}]})


@pytest.mark.parametrize("bad", ["PO", "-a", "_a", "a b", "a/b", "a.b", "a" * 65, "é"])
def test_id_regex(bad):
    with pytest.raises(ConfigError) as exc:
        parse_config({"sources": [one(id=bad)]})
    assert f"'{bad}'" in str(exc.value)
    assert "'id'" in str(exc.value)


def test_duplicate_ids():
    with pytest.raises(ConfigError, match=r"source 'docs'.*'id'.*duplicate"):
        parse_config({"sources": [one(), one()]})


def test_source_must_be_mapping():
    with pytest.raises(ConfigError, match=r"sources\[0\]"):
        parse_config({"sources": ["docs"]})


def test_bad_type():
    with pytest.raises(ConfigError, match=r"source 'docs'.*'type'"):
        parse_config({"sources": [one(type="ftp")]})
    with pytest.raises(ConfigError, match=r"source 'docs'.*'type'"):
        parse_config({"sources": [{"id": "docs"}]})


def test_bad_priority():
    with pytest.raises(ConfigError, match=r"source 'docs'.*'priority'"):
        parse_config({"sources": [one(priority="urgent")]})


@pytest.mark.parametrize("bad", ["every 5s", "hourly", 15, "every 15w"])
def test_bad_schedule(bad):
    with pytest.raises(ConfigError, match=r"source 'docs'.*'schedule'"):
        parse_config({"sources": [one(schedule=bad)]})


@pytest.mark.parametrize("bad", ["status", [], ["a", 1], ["a", ""], {"a": 1}])
def test_bad_track_fields(bad):
    with pytest.raises(ConfigError, match=r"source 'docs'.*'track_fields'"):
        parse_config({"sources": [one(track_fields=bad)]})


def test_track_fields_ok():
    src = parse_config({"sources": [one(track_fields=["status", "eta"])]}).sources[0]
    assert src.track_fields == ["status", "eta"]


@pytest.mark.parametrize("key", ["password", "passwd", "secret", "token", "api_key", "Password"])
def test_credential_keys_rejected(key):
    with pytest.raises(ConfigError) as exc:
        parse_config({"sources": [one(**{key: "hunter2"})]})
    msg = str(exc.value)
    assert "source 'docs'" in msg
    assert f"'{key}'" in msg
    assert CREDENTIALS_MSG in msg
    assert "hunter2" not in msg


def test_url_rejected_on_sql_sources():
    src = {"id": "po-table", "type": "sql", "url": "postgresql://u:pw@host/db"}
    with pytest.raises(ConfigError) as exc:
        parse_config({"sources": [src]})
    msg = str(exc.value)
    assert "source 'po-table'" in msg
    assert "'url'" in msg
    assert CREDENTIALS_MSG in msg
    assert "pw@host" not in msg


def test_url_allowed_on_other_types():
    src = parse_config({"sources": [{"id": "w", "type": "web", "url": "https://x.invalid"}]})
    assert src.sources[0].options["url"] == "https://x.invalid"


def test_url_env_is_fine_on_sql():
    src = parse_config({"sources": [{"id": "q", "type": "sql", "url_env": "MY_DB"}]}).sources[0]
    assert src.options == {"url_env": "MY_DB"}


# --- highlight -------------------------------------------------------------------------------


def test_highlight_all_ops_and_bonus():
    rules = [
        {"field": "status", "equals": "Cancelled"},
        {"field": "subject", "contains": "urgent", "bonus": 25},
        {"field": "status", "changed_to": "Late"},
    ]
    src = parse_config({"sources": [one(highlight=rules)]}).sources[0]
    assert src.highlight == [
        HighlightRule("status", "equals", "Cancelled", 10),
        HighlightRule("subject", "contains", "urgent", 25),
        HighlightRule("status", "changed_to", "Late", 10),
    ]


def test_highlight_scalar_values_become_strings():
    src = parse_config({"sources": [one(highlight=[{"field": "qty", "equals": 0}])]}).sources[0]
    assert src.highlight[0].value == "0"


@pytest.mark.parametrize(
    "rule",
    [
        {"field": "f"},  # no op
        {"field": "f", "equals": "a", "contains": "b"},  # two ops
        {"field": "f", "equals": "a", "changed_to": "b"},
        {"equals": "a"},  # no field
        {"field": "", "equals": "a"},
        {"field": "f", "equals": None},
        {"field": "f", "equals": ["a"]},
        {"field": "f", "equals": "a", "bonus": "10"},
        {"field": "f", "equals": "a", "bonus": True},
        {"field": "f", "equals": "a", "extra": 1},
        "status",
    ],
)
def test_highlight_invalid_rules(rule):
    with pytest.raises(ConfigError, match=r"source 'docs'.*'highlight\[0\]"):
        parse_config({"sources": [one(highlight=[rule])]})


def test_highlight_must_be_list():
    with pytest.raises(ConfigError, match=r"source 'docs'.*'highlight'"):
        parse_config({"sources": [one(highlight={"field": "a", "equals": "b"})]})


def test_no_test_touches_real_home():
    # Guard for the autouse fixture: SINCE_HOME must be set and not be the real ~/.since.
    home = Path(os.environ["SINCE_HOME"])
    assert home != Path.home() / ".since"
