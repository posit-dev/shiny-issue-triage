import pathlib

from triage_verse import db, snapshot


def test_compress_roundtrip(tmp_path):
    src = tmp_path / "a.txt"
    src.write_bytes(b"hello " * 1000)
    packed = tmp_path / "a.zst"
    out = tmp_path / "b.txt"

    snapshot.compress(src, packed)
    snapshot.decompress(packed, out)

    assert out.read_bytes() == src.read_bytes()
    assert packed.stat().st_size < src.stat().st_size


def test_vacuum_to_produces_queryable_copy(tmp_path):
    con = db.connect(tmp_path / "m.sqlite")
    con.execute("INSERT INTO runs (run_id, kind, started_at)"
                " VALUES ('r1', 'sync', '2026-06-12T00:00:00Z')")
    con.commit()
    out = tmp_path / "copy.sqlite"

    snapshot.vacuum_to(tmp_path / "m.sqlite", out)

    copy = db.connect(out)
    assert copy.execute("SELECT COUNT(*) FROM runs").fetchone()[0] == 1


def test_publish_uploads_latest_and_dated(tmp_path):
    db.connect(tmp_path / "m.sqlite").close()
    commands = []

    def fake_gh(args, **kwargs):
        commands.append(args)
        if args[:2] == ["release", "view"]:
            raise snapshot.GhError("release not found")
        if args[:2] == ["release", "list"]:
            return '[{"tagName": "mirror-2026-06-01"}]'
        return ""

    snapshot.publish(tmp_path / "m.sqlite", gh_run=fake_gh, dated=True,
                     today="2026-06-12")

    flat = [" ".join(c) for c in commands]
    assert any(c.startswith("release create mirror-latest") for c in flat)
    assert any(c.startswith("release upload mirror-latest") and "--clobber" in c
               for c in flat)
    assert any(c.startswith("release create mirror-2026-06-12") for c in flat)
    assert any(c.startswith("release upload mirror-2026-06-12") and "--clobber" in c
               for c in flat)


def test_publish_prunes_old_dated_releases(tmp_path):
    db.connect(tmp_path / "m.sqlite").close()
    tags = [f"mirror-2026-05-{d:02d}" for d in range(1, 11)]  # 10 dated tags
    commands = []

    def fake_gh(args, **kwargs):
        commands.append(args)
        if args[:2] == ["release", "view"]:
            return ""  # releases exist
        if args[:2] == ["release", "list"]:
            # list runs after the new dated release was created, so include it
            listed = tags + ["mirror-2026-06-12"]
            return snapshot.json.dumps([{"tagName": t} for t in listed])
        return ""

    snapshot.publish(tmp_path / "m.sqlite", gh_run=fake_gh, dated=True,
                     today="2026-06-12", keep=8)

    deletes = [c for c in commands if c[:2] == ["release", "delete"]]
    deleted_tags = {c[2] for c in deletes}
    # 10 existing + 1 new = 11; keep 8 newest -> delete 3 oldest
    assert deleted_tags == {"mirror-2026-05-01", "mirror-2026-05-02",
                            "mirror-2026-05-03"}


def test_bootstrap_refuses_to_overwrite_without_force(tmp_path):
    target = tmp_path / "m.sqlite"
    target.write_bytes(b"existing")

    try:
        snapshot.bootstrap(target, gh_run=lambda *a, **k: "")
        raised = False
    except snapshot.SnapshotError:
        raised = True

    assert raised
    assert target.read_bytes() == b"existing"


def test_publish_requires_existing_db(tmp_path):
    import pytest
    with pytest.raises(snapshot.SnapshotError, match="does not exist"):
        snapshot.publish(tmp_path / "nope.sqlite", gh_run=lambda *a, **k: "")


def test_bootstrap_force_overwrites_and_is_atomic(tmp_path):
    # Build a real compressed snapshot to "download".
    src = db.connect(tmp_path / "src.sqlite")
    src.execute("INSERT INTO runs (run_id, kind, started_at)"
                " VALUES ('r1', 'sync', '2026-06-12T00:00:00Z')")
    src.commit()
    src.close()
    plain = tmp_path / "plain.sqlite"
    snapshot.vacuum_to(tmp_path / "src.sqlite", plain)
    asset = tmp_path / "asset.zst"
    snapshot.compress(plain, asset)

    target = tmp_path / "out" / "mirror.sqlite"
    target.parent.mkdir()
    target.write_bytes(b"stale")

    def fake_gh(args, **kwargs):
        # `release download ... --output <path>`: copy our prebuilt asset there
        out = args[args.index("--output") + 1]
        pathlib.Path(out).write_bytes(asset.read_bytes())
        return ""

    snapshot.bootstrap(target, gh_run=fake_gh, force=True)

    restored = db.connect(target)
    assert restored.execute("SELECT COUNT(*) FROM runs").fetchone()[0] == 1


def test_bootstrap_creates_parent_dir(tmp_path):
    src = db.connect(tmp_path / "src.sqlite")
    src.commit()
    src.close()
    plain = tmp_path / "plain.sqlite"
    snapshot.vacuum_to(tmp_path / "src.sqlite", plain)
    asset = tmp_path / "asset.zst"
    snapshot.compress(plain, asset)

    target = tmp_path / "fresh" / "nested" / "mirror.sqlite"

    def fake_gh(args, **kwargs):
        out = args[args.index("--output") + 1]
        pathlib.Path(out).write_bytes(asset.read_bytes())
        return ""

    snapshot.bootstrap(target, gh_run=fake_gh)

    assert target.exists()
