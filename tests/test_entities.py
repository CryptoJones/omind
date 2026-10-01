# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Aaron K. Clark
"""Tests for the name index (#385): identifier extraction and the
``searchindex`` table that maps names to the notes that mention them."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from omind import entities, searchindex
from omind.cli import main


def _note(omi: Path, title: str, details: str, *, created: str = "2026-09-25") -> Path:
    path = omi / f"{title}.md"
    path.write_text(
        f"# {title}\n\n## Metadata\n- Created: {created}\n- Tags:\n- Rev: 1@ronin28-19b4f0\n\n"
        f"## Summary\n{title}\n\n## Details\n{details}\n",
        encoding="utf-8",
    )
    return path


@pytest.fixture
def omi(tmp_path: Path) -> Path:
    d = tmp_path / "OMI"
    d.mkdir()
    return d


@pytest.fixture(autouse=True)
def _flag_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(entities.ENABLE_ENV, raising=False)
    monkeypatch.delenv(entities.MAX_DF_ENV, raising=False)


def _keys(text: str, *, title: str = "", tags: tuple[str, ...] = ()) -> set[str]:
    return set(entities.extract(text, title=title, tags=tags))


# -- extraction ---------------------------------------------------------------


def test_mixed_letter_digit_identifiers_are_extracted() -> None:
    keys = _keys("The As30p drive (serial WX81A255JFYD) sits on an RTL8814AU dongle; dl380 too.")
    assert {"as30p", "wx81a255jfyd", "rtl8814au", "dl380"} <= keys


def test_plain_words_never_become_entities() -> None:
    prose = (
        "The quick brown fox jumps over the lazy dog because it wanted to, and/or "
        "read/write, Section/Chapter. Notes about memory and retrieval."
    )
    assert _keys(prose) == set()


def test_units_versions_ordinals_and_hashes_are_not_names() -> None:
    text = (
        "16GB 100ms 3rd 2e 37GB v10 V2 1080p 6-month-old 8-step 2-gpu "
        "0123456789abcdef0123 81d9684e-da65-4939-ba32-b9e0f1a406fe"
    )
    assert _keys(text) == set()


def test_hostnames_domains_and_ipv4_but_not_files() -> None:
    text = (
        "Served at linear-algebra.cryptojones.dev and pluto.local from 172.16.27.183; "
        "see store.py, README.md, bootstrap.sh, os.environ, Harness.app and 999.1.1.1."
    )
    keys = _keys(text)
    assert {"linear-algebra.cryptojones.dev", "pluto.local", "172.16.27.183"} <= keys
    for not_a_host in ("store.py", "readme.md", "bootstrap.sh", "os.environ", "harness.app"):
        assert not_a_host not in keys
    assert "999.1.1.1" not in keys


def test_repo_slugs_from_urls_and_bare() -> None:
    text = (
        "Clone https://github.com/CryptoJones/omind.git or git@gitlab.com:someone/tool, "
        "pull openai/gpt-oss-20b, but not src/omind/cli.py, tests/test_x.py, 1/2 or 16k/20k."
    )
    keys = _keys(text)
    assert {"cryptojones/omind", "someone/tool", "openai/gpt-oss-20b"} <= keys
    for nope in ("src/omind", "omind/cli.py", "tests/test_x.py", "1/2", "16k/20k"):
        assert nope not in keys


def test_volume_labels_and_wikilinks() -> None:
    text = "Mounted at /Volumes/Extra/books; see [[Books library structure|the library]]."
    keys = _keys(text)
    assert "extra" in keys
    assert "books library structure" in keys


def test_hyphenated_compound_also_yields_its_named_parts() -> None:
    keys = _keys("Relabelled the WD-As30p partition; qwen3-coder-30b runs.")
    assert {"wd-as30p", "as30p", "qwen3-coder-30b", "qwen3"} <= keys


def test_rev_and_agent_lines_do_not_count_as_mentions() -> None:
    text = "- Rev: 3@ronin28-19b4f0\n- Agent: makemake-7f2316\nNothing else here."
    assert _keys(text) == set()


def test_title_and_tags_are_sources_too() -> None:
    keys = _keys("body", title="Seagate As30p drive", tags=("dl380",))
    assert {"as30p", "dl380"} <= keys


def test_extraction_is_deterministic_and_case_folded() -> None:
    text = "AS30P and as30p and As30p on ronin28.LAN"
    first = entities.extract(text)
    assert first == entities.extract(text)
    assert first["as30p"] == "AS30P"  # first spelling seen is kept
    assert "ronin28.lan" in first


def test_extraction_is_bounded_per_note() -> None:
    text = " ".join(f"host{i}x" for i in range(entities.MAX_ENTITIES_PER_NOTE + 50))
    assert len(entities.extract(text)) == entities.MAX_ENTITIES_PER_NOTE


def test_normalize_composes_nfd() -> None:
    assert entities.normalize("Café-2") == entities.normalize("Café-2")


# -- flag and ceiling -----------------------------------------------------------


@pytest.mark.parametrize("value", ["0", "off", "false", "no", " OFF "])
def test_flag_turns_the_index_off(monkeypatch: pytest.MonkeyPatch, value: str) -> None:
    monkeypatch.setenv(entities.ENABLE_ENV, value)
    assert not entities.enabled()


def test_flag_defaults_on() -> None:
    assert entities.enabled()


def test_df_ceiling_default_floor_and_override(monkeypatch: pytest.MonkeyPatch) -> None:
    assert entities.df_ceiling(20) == entities.MIN_DF_CEILING
    assert entities.df_ceiling(1400) == int(1400 * entities.DEFAULT_MAX_DF_FRACTION)
    monkeypatch.setenv(entities.MAX_DF_ENV, "0.5")
    assert entities.df_ceiling(1400) == 700
    for junk in ("abc", "0", "-1", "7"):
        monkeypatch.setenv(entities.MAX_DF_ENV, junk)
        assert entities.df_ceiling(1400) == int(1400 * entities.DEFAULT_MAX_DF_FRACTION)


# -- the index ------------------------------------------------------------------


def test_notes_for_entity_lists_every_mention_newest_first(omi: Path) -> None:
    _note(omi, "Seagate drive", "The old Seagate was labelled As30p.", created="2026-09-20")
    _note(omi, "WD Blue drive", "As30p now lives on the WD Blue.", created="2026-09-27")
    _note(omi, "Samsung SSD", "Copied /Volumes/As30p to a Samsung SSD.", created="2026-09-25")
    _note(omi, "Unrelated", "Nothing about drives here.", created="2026-09-30")
    idx = searchindex.SearchIndex(omi)
    found = idx.notes_for_entity("as30p")
    assert found is not None
    assert [n.title for n in found] == ["WD Blue drive", "Samsung SSD", "Seagate drive"]
    assert [n.last_seen for n in found] == ["2026-09-27", "2026-09-25", "2026-09-20"]
    assert idx.notes_for_entity("nothing9x") == []


def test_common_identifier_is_not_an_entity(omi: Path) -> None:
    """The document-frequency ceiling: a token in every note is not informative."""
    for i in range(30):
        _note(omi, f"Note {i}", f"encoded as utf8, entry {i}")
    _note(omi, "Rare", "The serial is WX81A255JFYD, also utf8.")
    idx = searchindex.SearchIndex(omi)
    lookup = idx.entity_lookup("utf8")
    assert lookup is not None
    assert lookup.common and lookup.df == 31 and lookup.total == 31
    assert lookup.notes == []
    assert idx.notes_for_entity("utf8") == []
    forced = idx.entity_lookup("utf8", include_common=True)
    assert forced is not None and len(forced.notes) == 31
    rare = idx.notes_for_entity("WX81A255JFYD")
    assert rare is not None and [n.title for n in rare] == ["Rare"]


def test_archived_and_deleted_notes_drop_out(omi: Path) -> None:
    keep = _note(omi, "Keep", "dl380 is the HPE box.")
    gone = _note(omi, "Gone", "dl380 again.")
    archived = _note(omi, "Archived", "dl380 history.")
    archived.write_text(
        archived.read_text(encoding="utf-8").replace("- Tags:", "- Disabled: true\n- Tags:"),
        encoding="utf-8",
    )
    idx = searchindex.SearchIndex(omi)
    found = idx.notes_for_entity("dl380")
    assert found is not None and {n.title for n in found} == {"Keep", "Gone"}
    gone.unlink()
    keep.write_text(keep.read_text(encoding="utf-8").replace("dl380", "the box"), encoding="utf-8")
    assert idx.refresh() is not None
    assert idx.notes_for_entity("dl380") == []


def test_flag_off_writes_nothing_and_lookups_return_none(
    omi: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _note(omi, "Drive", "As30p label.")
    idx = searchindex.SearchIndex(omi)
    assert idx.notes_for_entity("as30p")
    monkeypatch.setenv(entities.ENABLE_ENV, "0")
    assert idx.refresh() is not None
    assert idx.notes_for_entity("as30p") is None
    stats = idx.stats()
    assert stats is not None and stats["entity_rows"] == 0
    # Back on: the next refresh backfills from disk without re-ingesting notes.
    monkeypatch.delenv(entities.ENABLE_ENV)
    result = idx.refresh()
    assert result is not None and result.reindexed == 0
    found = idx.notes_for_entity("as30p")
    assert found is not None and [n.title for n in found] == ["Drive"]


def test_existing_index_is_backfilled_without_a_rebuild(
    omi: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An index built before #385 (or by an older extractor) gains the name index
    on the next refresh, without a schema wipe or a single note re-ingested."""
    _note(omi, "Drive", "As30p label.")
    idx = searchindex.SearchIndex(omi)
    assert idx.refresh() is not None
    monkeypatch.setattr(entities, "EXTRACTOR_VERSION", "test-next")
    result = idx.refresh()
    assert result is not None and result.reindexed == 0
    found = idx.notes_for_entity("as30p")
    assert found is not None and len(found) == 1


def test_rebuild_recreates_the_name_index(omi: Path) -> None:
    _note(omi, "Drive", "As30p label.")
    idx = searchindex.SearchIndex(omi)
    assert idx.notes_for_entity("as30p")
    idx.drop()
    found = searchindex.SearchIndex(omi).notes_for_entity("as30p")
    assert found is not None and len(found) == 1


def test_stats_report_the_name_index(omi: Path) -> None:
    _note(omi, "Drive", "As30p on dl380.")
    idx = searchindex.SearchIndex(omi)
    assert idx.refresh() is not None
    stats = idx.stats()
    assert stats is not None
    assert stats["entities"] == 2 and stats["entity_rows"] == 2


def test_lookup_fails_open_when_the_index_is_disabled(
    omi: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _note(omi, "Drive", "As30p label.")
    monkeypatch.setenv(searchindex.DISABLE_ENV, "1")
    assert searchindex.SearchIndex(omi).notes_for_entity("as30p") is None


# -- CLI ------------------------------------------------------------------------


def _cli(tmp_path: Path, *extra: str) -> int:
    return main(["entity", *extra, "--vault", str(tmp_path), "--folder", "OMI"])


def test_cli_lists_notes_newest_first(
    tmp_path: Path, omi: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _note(omi, "Seagate As30p", "old spinner", created="2026-09-20")
    _note(omi, "WD Blue As30p", "new home", created="2026-09-27")
    assert _cli(tmp_path, "As30p") == 0
    out = capsys.readouterr().out
    assert "2 note(s) mention 'As30p'" in out
    assert out.index("WD Blue As30p") < out.index("Seagate As30p")
    assert _cli(tmp_path, "As30p", "--limit", "1") == 0
    assert "1 more" in capsys.readouterr().out


def test_cli_json_and_missing(
    tmp_path: Path, omi: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _note(omi, "Seagate As30p", "old spinner")
    assert _cli(tmp_path, "as30p", "--json") == 0
    data = json.loads(capsys.readouterr().out)
    assert data["token"] == "as30p" and data["df"] == 1 and not data["common"]
    assert data["notes"][0]["title"] == "Seagate As30p"
    assert _cli(tmp_path, "zz99top") == 1
    assert "no note mentions" in capsys.readouterr().out


def test_cli_reports_a_common_token(
    tmp_path: Path, omi: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    for i in range(12):
        _note(omi, f"Note {i}", "utf8 everywhere")
    assert _cli(tmp_path, "utf8") == 0
    assert "too common" in capsys.readouterr().out
    assert _cli(tmp_path, "utf8", "--all") == 0
    assert "12 note(s)" in capsys.readouterr().out


def test_cli_flag_off(
    tmp_path: Path, omi: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv(entities.ENABLE_ENV, "0")
    assert _cli(tmp_path, "As30p") == 2
    assert "disabled" in capsys.readouterr().err
