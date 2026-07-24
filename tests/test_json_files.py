from datetime import datetime, timezone
import json

import pytest

from core.storage.json_files import (
    ensure_utc,
    utc_now,
    write_json_atomic,
    write_jsonl_atomic,
)


def test_utc_now_is_timezone_aware():
    assert utc_now().tzinfo is not None


def test_ensure_utc_treats_naive_as_utc_and_converts_aware():
    naive = datetime(2026, 1, 1, 12, 0, 0)
    assert ensure_utc(naive) == datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)

    aware = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
    assert ensure_utc(aware) == aware


def test_ensure_utc_lets_legacy_and_new_records_sort_together():
    legacy_naive = datetime(2026, 1, 1, 9, 0, 0)
    new_aware = utc_now()
    # Mixing aware and naive datetimes raises TypeError when compared; after
    # normalization they sort without error.
    ordered = sorted([ensure_utc(new_aware), ensure_utc(legacy_naive)])
    assert ordered[0] == ensure_utc(legacy_naive)


def test_write_json_atomic_replaces_existing_document(tmp_path):
    destination = tmp_path / "record.json"
    write_json_atomic(destination, {"version": 1})
    write_json_atomic(destination, {"version": 2, "items": ["asset"]})

    assert json.loads(destination.read_text(encoding="utf-8")) == {
        "items": ["asset"],
        "version": 2,
    }
    assert list(tmp_path.glob("*.tmp")) == []


def test_write_json_atomic_preserves_previous_document_on_serialization_failure(tmp_path):
    destination = tmp_path / "record.json"
    write_json_atomic(destination, {"status": "ready"})

    with pytest.raises(TypeError):
        write_json_atomic(destination, {"invalid": object()})

    assert json.loads(destination.read_text(encoding="utf-8")) == {"status": "ready"}
    assert list(tmp_path.glob("*.tmp")) == []


def test_write_jsonl_atomic_is_deterministic_and_preserves_destination_on_failure(tmp_path):
    destination = tmp_path / "records.jsonl"
    write_jsonl_atomic(destination, [{"z": 2, "a": 1}])
    assert destination.read_text(encoding="utf-8") == '{"a": 1, "z": 2}\n'

    with pytest.raises(TypeError):
        write_jsonl_atomic(destination, [{"invalid": object()}])

    assert destination.read_text(encoding="utf-8") == '{"a": 1, "z": 2}\n'
    assert list(tmp_path.glob("*.tmp")) == []
