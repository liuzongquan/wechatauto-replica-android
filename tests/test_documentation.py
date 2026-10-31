"""离线文档/发行物契约；不把链接或样例通过当作真机兼容性证据。"""
from __future__ import annotations

import re
from pathlib import Path

from wechatauto_android.archive import ExportArchive

ROOT = Path(__file__).parents[1]


def test_local_documentation_links_resolve():
    documents = [ROOT / "README.md", *sorted((ROOT / "docs").glob("*.md"))]
    for document in documents:
        for target in re.findall(r"\]\(([^)\s]+)\)", document.read_text(encoding="utf-8")):
            if target.startswith(("https://", "http://", "mailto:", "#")):
                continue
            local_path = target.split("#", 1)[0]
            assert not local_path or (document.parent / local_path).exists(), (document, target)


def test_matrix_contains_each_reference_semantic_group_once():
    matrix = (ROOT / "docs" / "FEATURE_MATRIX.md").read_text(encoding="utf-8")
    rows = re.findall(r"^\| (F\d{2}) \|", matrix, flags=re.MULTILINE)
    assert rows == [f"F{index:02}" for index in range(1, 54)]
    for state in ("implemented", "automated-fixture", "live-pending", "unavailable"):
        assert state in matrix


def test_readme_archive_is_explicitly_synthetic_local_data():
    archive = ExportArchive.open(ROOT / "docs" / "examples" / "archive.json", "demo-account")
    messages = archive.get_messages("demo-chat", limit=20)
    assert len(messages) == 2
    assert all(message.origin == "user_export" for message in messages)
    assert all(message.account_id == "demo-account" for message in messages)


def test_source_distribution_manifest_keeps_test_helpers_and_operator_script():
    manifest = (ROOT / "MANIFEST.in").read_text(encoding="utf-8")
    for rule in ("recursive-include tests *.py", "recursive-include scripts *.py",
                 "recursive-include docs *.md *.json", "recursive-include profiles *.json"):
        assert rule in manifest
    assert (ROOT / "tests" / "fakes.py").is_file()
    assert (ROOT / "scripts" / "hardware_smoke.py").is_file()
