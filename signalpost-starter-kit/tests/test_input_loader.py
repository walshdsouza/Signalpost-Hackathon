"""main.load_target_organisations: every supported batch format yields clean 9-digit org numbers."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from main import load_target_organisations  # noqa: E402

A, B = "923609016", "982463718"


def load(tmp_path, content, name="batch.txt"):
    f = tmp_path / name
    f.write_text(content, encoding="utf-8")
    return load_target_organisations(f)


def test_plain_lines_blank_lines_and_crlf(tmp_path):
    assert load(tmp_path, f"{A}\r\n\r\n{B}\r\n") == [A, B]


def test_jsonl_with_various_keys(tmp_path):
    content = f'{{"orgnr": "{A}"}}\n{{"organisation_number": "{B}", "name": "X"}}\n{{"organisasjonsnummer": 914778271}}\n'
    assert load(tmp_path, content, "b.jsonl") == [A, B, "914778271"]


def test_json_array_whole_file_and_quoted_lines(tmp_path):
    assert load(tmp_path, f'["{A}", "{B}"]') == [A, B]
    assert load(tmp_path, f'[\n  "{A}",\n  "{B}"\n]') == [A, B]
    assert load(tmp_path, f'"{A}",\n"{B}"\n') == [A, B]


def test_spaced_and_dotted_numbers(tmp_path):
    assert load(tmp_path, "923 609 016\n982.463.718\n") == [A, B]


def test_csv_lines_and_header(tmp_path):
    assert load(tmp_path, f"orgnr,name\n{A},Equinor\n{B},Telenor\n") == [A, B]


def test_duplicates_bom_and_junk_skipped(tmp_path):
    content = "\ufeff" + f"{A}\n{A}\nnot a number\n12345\n1234567890\n{B}\n"
    assert load(tmp_path, content) == [A, B]


def test_repo_batch_file_unchanged():
    path = Path(__file__).resolve().parent.parent / "batch_1000.txt"
    if path.exists():
        orgs = load_target_organisations(path)
        assert len(orgs) == 1000 and all(len(o) == 9 and o.isdigit() for o in orgs)
