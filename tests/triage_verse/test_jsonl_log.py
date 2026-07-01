import json

from triage_verse import jsonl_log


def test_append_weekly_creates_partition_and_appends(tmp_path):
    recs = [{"a": 1}]
    path = jsonl_log.append_weekly(recs, tmp_path / "log", today="2026-06-29")
    assert path.exists()
    assert "2026/W27.jsonl" in str(path).replace("\\", "/")
    line = json.loads(path.read_text().splitlines()[0])
    assert line == {"a": 1}
    # appends, not overwrites
    jsonl_log.append_weekly(recs, tmp_path / "log", today="2026-06-29")
    assert len(path.read_text().splitlines()) == 2
