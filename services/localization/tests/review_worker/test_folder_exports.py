"""Save-to-folder on the Mac: real files in a real temporary export root, a mock S3 bucket
that answers like MinIO (a missing key is an `ArtifactMissingError`) and a scratch database
with the downloader's `folder_exports` table, read and written as the `localization` role."""

import dataclasses
import threading
import time
from pathlib import Path

import psycopg
import pytest
from psycopg.pq import TransactionStatus
from psycopg.rows import dict_row, tuple_row

from localization.config import ConfigError
from localization.review_worker import folder_exports, review_scan, studio_requests
from localization.review_worker.config import WorkerConfig
from localization.review_worker.folder_exports import ExportRejected, check_key, check_subdir
from localization.review_worker.main import Worker
from tests.review_worker.conftest import CORE, DELIVERY, PROJECT_KEY, Db
from tests.support.scratch_db import (
    COLLECTIONS,
    DASHBOARD_INTAKE,
    INIT,
    OUTBOX,
    add_channel_collection,
    scratch_database,
)

REV = "a" * 32
FINAL = f"output/review/7001/g1/{REV}.mp4"
SRT = f"output/review/7001/g1/{REV}.srt"
SUBDIR = "Phim/成名的代价"


@pytest.fixture
def db():
    for info, admin in scratch_database(
        INIT, OUTBOX, DASHBOARD_INTAKE, CORE, PROJECT_KEY, DELIVERY, COLLECTIONS
    ):
        admin.row_factory = dict_row
        state = Db(info, admin)
        yield state
        for conn in state.opened:
            conn.close()


@pytest.fixture
def root(tmp_path) -> Path:
    path = tmp_path / "exports"
    path.mkdir()
    return path


@pytest.fixture
def outside(tmp_path) -> Path:
    path = tmp_path / "outside"
    path.mkdir()
    return path


def add_export(db, aweme="7001", generation=1, *, stem=None, subdir=SUBDIR, final=FINAL,
               srt=SRT) -> int:  # fmt: skip
    db.admin.row_factory = tuple_row  # the helper reads rows by position
    try:
        collection = add_channel_collection(db.admin, "chan")
    finally:
        db.admin.row_factory = dict_row
    db.admin.execute(
        "INSERT INTO videos (aweme_id, sec_user_id, create_time, status, collection_id) "
        "VALUES (%s, 'chan', now(), 'done', %s) ON CONFLICT DO NOTHING",
        (aweme, collection),
    )
    return db.admin.execute(
        "INSERT INTO folder_exports (aweme_id, generation, collection_id, target_subdir, "
        "file_stem, final_key, srt_key) VALUES (%s, %s, %s, %s, %s, %s, %s) RETURNING id",
        (aweme, generation, collection, subdir, stem or f"E01-{aweme}", final, srt),
    ).fetchone()["id"]


def export_row(db, row_id):
    return db.admin.execute("SELECT * FROM folder_exports WHERE id = %s", (row_id,)).fetchone()


def put(store, key, data):
    store.put_bytes(key, data)


def export_one(db, store, root):
    conn = db.worker()
    row = folder_exports.claim(conn)
    assert row is not None
    return folder_exports.export_claimed(row, conn=conn, store=store, root=root)


def leftovers(directory: Path) -> list[str]:
    return [p.name for p in directory.iterdir() if p.name.startswith(".reup-export-")]


# -- writing ------------------------------------------------------------------


def test_episode_export_writes_mp4_and_srt_and_records_relative_paths(db, review_store, root):
    put(review_store, FINAL, b"final-mp4")
    put(review_store, SRT, b"1\n00:00:00,000 --> 00:00:01,000\nxin chao\n")
    row_id = add_export(db)
    assert export_one(db, review_store, root) == "done"
    target = root / "Phim" / "成名的代价"
    assert (target / "E01-7001.mp4").read_bytes() == b"final-mp4"
    assert (target / "E01-7001.srt").read_bytes().endswith(b"xin chao\n")
    after = export_row(db, row_id)
    assert after["state"] == "done" and after["error"] is None
    assert after["files"] == [f"{SUBDIR}/E01-7001.mp4", f"{SUBDIR}/E01-7001.srt"]
    assert after["claim"] is None and after["lease_expires_at"] is None
    assert after["handled_at"] is not None
    assert leftovers(target) == []


def test_video_outside_a_mix_without_srt_writes_only_the_mp4(db, review_store, root):
    put(review_store, "output/7001.mp4", b"legacy-mp4")
    row_id = add_export(db, stem="7001", final="output/7001.mp4", srt=None)
    assert export_one(db, review_store, root) == "done"
    target = root / "Phim" / "成名的代价"
    assert sorted(p.name for p in target.iterdir()) == ["7001.mp4"]
    assert (target / "7001.mp4").read_bytes() == b"legacy-mp4"
    assert export_row(db, row_id)["files"] == [f"{SUBDIR}/7001.mp4"]


def test_an_existing_file_of_the_same_name_is_overwritten(db, review_store, root):
    put(review_store, FINAL, b"new-mp4")
    put(review_store, SRT, b"new-srt")
    target = root / "Phim" / "成名的代价"
    target.mkdir(parents=True)
    (target / "E01-7001.mp4").write_bytes(b"old-mp4-that-was-longer")
    (target / "E01-7001.srt").write_bytes(b"old-srt")
    add_export(db)
    assert export_one(db, review_store, root) == "done"
    assert (target / "E01-7001.mp4").read_bytes() == b"new-mp4"
    assert (target / "E01-7001.srt").read_bytes() == b"new-srt"


# -- staying under the root ----------------------------------------------------


def test_a_subdir_through_a_symlink_out_of_the_root_is_refused(db, review_store, root, outside):
    put(review_store, FINAL, b"mp4")
    put(review_store, SRT, b"srt")
    (root / "Phim").symlink_to(outside, target_is_directory=True)
    row_id = add_export(db)
    assert export_one(db, review_store, root) == "failed"
    assert list(outside.iterdir()) == []
    after = export_row(db, row_id)
    assert after["state"] == "failed" and "liên kết" in after["error"]
    assert str(root) not in after["error"] and str(outside) not in after["error"]
    assert after["files"] is None


def test_a_symlink_at_the_final_name_is_replaced_not_followed(db, review_store, root, outside):
    put(review_store, FINAL, b"mp4")
    put(review_store, SRT, b"srt")
    victim = outside / "victim.mp4"
    victim.write_bytes(b"precious")
    target = root / "Phim" / "成名的代价"
    target.mkdir(parents=True)
    (target / "E01-7001.mp4").symlink_to(victim)
    add_export(db)
    assert export_one(db, review_store, root) == "done"
    assert victim.read_bytes() == b"precious"
    assert not (target / "E01-7001.mp4").is_symlink()
    assert (target / "E01-7001.mp4").read_bytes() == b"mp4"


def test_a_symlink_planted_at_the_temp_name_is_not_followed(
    db, review_store, root, outside, monkeypatch
):
    put(review_store, FINAL, b"mp4")
    put(review_store, SRT, b"srt")
    victim = outside / "victim"
    victim.write_bytes(b"precious")
    target = root / "Phim" / "成名的代价"
    target.mkdir(parents=True)
    fixed = "f" * 32
    monkeypatch.setattr(folder_exports, "random_token", lambda: fixed)
    (target / f".reup-export-{fixed}.part").symlink_to(victim)
    row_id = add_export(db)
    assert export_one(db, review_store, root) == "failed"
    assert victim.read_bytes() == b"precious"
    assert not (target / "E01-7001.mp4").exists()
    error = export_row(db, row_id)["error"]
    assert "Không ghi được" in error and str(root.parent) not in error


def test_subdir_rules_match_the_database_and_reject_escapes():
    assert check_subdir("Phim/成名的代价") == ["Phim", "成名的代价"]
    assert check_subdir("a") == ["a"]
    for bad in ("", "/abs", "a/", "a//b", ".", "..", "a/../b", "a/./b", "a\nb", "a\x00b",
                "x" * 513):  # fmt: skip
        with pytest.raises(ExportRejected):
            check_subdir(bad)


# -- trusting the keys only as far as the dashboard does ------------------------


def test_keys_must_lie_in_the_generation_folder_of_the_video():
    check_key(FINAL, "7001", 1, ".mp4", legacy_ok=True)
    check_key(SRT, "7001", 1, ".srt", legacy_ok=False)
    check_key("output/7001.mp4", "7001", 1, ".mp4", legacy_ok=True)
    bad = [
        ("output/7001.mp4", ".srt", False),  # the legacy name is a final, never an SRT
        ("output/7001.srt", ".srt", False),
        ("output/7002.mp4", ".mp4", True),
        (f"output/review/7002/g1/{REV}.mp4", ".mp4", True),  # another video
        (f"output/review/7001/g2/{REV}.mp4", ".mp4", True),  # another generation
        (f"output/review/7001/g1/sub/{REV}.mp4", ".mp4", True),
        ("output/review/7001/g1/../x.mp4", ".mp4", True),
        ("output/review/7001/g1/...mp4", ".mp4", True),
        ("output/review/7001/g1/.mp4", ".mp4", True),
        (f"output/review/7001/g1/{REV}.srt", ".mp4", True),  # wrong kind
        (f"output/review/7001/g1/{REV} x.mp4", ".mp4", True),
        (f"source/7001/{REV}.mp4", ".mp4", True),
        (f"output/review/7001/g1/{REV}.mp4\n", ".mp4", True),
    ]
    for key, ext, legacy in bad:
        with pytest.raises(ExportRejected):
            check_key(key, "7001", 1, ext, legacy_ok=legacy)


class Recording:
    """The real store, noting every download it is asked for."""

    def __init__(self, store):
        self.store, self.asked = store, []

    def download_fileobj(self, key, out, *, produced_by):
        self.asked.append(key)
        return self.store.download_fileobj(key, out, produced_by=produced_by)


def test_a_key_outside_the_folder_fails_without_downloading(db, review_store, root):
    other = f"output/review/7002/g1/{REV}.mp4"
    put(review_store, other, b"someone-else")
    row_id = add_export(db, final=other)
    store = Recording(review_store)
    assert export_one(db, store, root) == "failed"
    after = export_row(db, row_id)
    assert after["state"] == "failed" and "không hợp lệ" in after["error"]
    assert store.asked == [] and not (root / "Phim").exists()


def test_a_missing_object_fails_and_leaves_no_temp_file(db, review_store, root):
    row_id = add_export(db)  # nothing uploaded
    assert export_one(db, review_store, root) == "failed"
    target = root / "Phim" / "成名的代价"
    assert leftovers(target) == [] and list(target.iterdir()) == []
    after = export_row(db, row_id)
    assert "Không tìm thấy bản cuối" in after["error"] and after["files"] is None


def test_a_missing_srt_fails_after_the_mp4_and_records_what_was_written(db, review_store, root):
    put(review_store, FINAL, b"mp4")
    row_id = add_export(db)
    assert export_one(db, review_store, root) == "failed"
    target = root / "Phim" / "成名的代价"
    assert leftovers(target) == []
    after = export_row(db, row_id)
    assert "Không tìm thấy SRT" in after["error"]
    assert after["files"] == [f"{SUBDIR}/E01-7001.mp4"]


def test_an_empty_final_is_not_saved(db, review_store, root):
    put(review_store, FINAL, b"")
    put(review_store, SRT, b"srt")
    row_id = add_export(db)
    assert export_one(db, review_store, root) == "failed"
    target = root / "Phim" / "成名的代价"
    assert list(target.iterdir()) == []
    assert "rỗng" in export_row(db, row_id)["error"]


def test_a_missing_root_fails_without_creating_it_or_naming_it(db, review_store, tmp_path):
    put(review_store, FINAL, b"mp4")
    gone = tmp_path / "unplugged-drive"
    row_id = add_export(db)
    assert export_one(db, review_store, gone) == "failed"
    assert not gone.exists()
    error = export_row(db, row_id)["error"]
    assert "Thư mục gốc" in error and str(gone) not in error


# -- claiming -------------------------------------------------------------------


def test_overlapping_claimers_never_take_the_same_row(db):
    """The first claim stays in an open transaction, holding its row lock, while the second
    claimer runs. Without SKIP LOCKED the second blocks on the lock and, once the first
    commits, takes the same row as well."""
    first_id = add_export(db, "7001")
    second_id = add_export(db, "7002")
    first, second = db.worker(), db.worker()
    second.execute("SET lock_timeout = '6s'")
    seen: dict[str, object] = {}

    def claim_twice():
        try:
            seen["a"] = folder_exports.claim(second)
            seen["b"] = folder_exports.claim(second)
        except Exception as exc:  # noqa: BLE001
            seen["error"] = exc
        seen["first_open"] = first.info.transaction_status == TransactionStatus.INTRANS

    with first.transaction():
        mine = folder_exports.claim(first)
        assert mine is not None and mine["id"] == first_id
        thread = threading.Thread(target=claim_twice)
        thread.start()
        thread.join(3)
        assert not thread.is_alive(), "second claimer waited for the first one's lock"
        assert first.info.transaction_status == TransactionStatus.INTRANS
    thread.join(10)
    assert "error" not in seen and seen["first_open"] is True
    assert seen["a"]["id"] == second_id and seen["b"] is None
    assert export_row(db, first_id)["claim"] == mine["claim"]
    assert export_row(db, second_id)["claim"] == seen["a"]["claim"]


def test_a_live_lease_is_not_claimed_and_an_expired_one_is(db):
    row_id = add_export(db)
    one = folder_exports.claim(db.worker())
    assert one["id"] == row_id and export_row(db, row_id)["state"] == "exporting"
    assert folder_exports.claim(db.worker()) is None
    db.admin.execute("UPDATE folder_exports SET lease_expires_at = now() - interval '1 min'")
    again = folder_exports.claim(db.worker())
    assert again["id"] == row_id and again["claim"] != one["claim"]


def test_finished_rows_are_never_claimed(db):
    for state in ("done", "failed"):
        row_id = add_export(db, f"70{len(state)}")
        db.admin.execute(
            "UPDATE folder_exports SET state = %s, handled_at = now() WHERE id = %s",
            (state, row_id),
        )
    assert folder_exports.claim(db.worker()) is None


def test_a_reclaimed_row_is_not_settled_by_the_old_claim(db, review_store, root):
    put(review_store, FINAL, b"mp4")
    put(review_store, SRT, b"srt")
    row_id = add_export(db)
    conn = db.worker()
    row = folder_exports.claim(conn)
    db.admin.execute("UPDATE folder_exports SET claim = gen_random_uuid()")
    assert folder_exports.export_claimed(row, conn=conn, store=review_store, root=root) == "lost"
    after = export_row(db, row_id)
    assert after["state"] == "exporting" and after["files"] is None
    assert not (root / "Phim" / "成名的代价" / "E01-7001.mp4").exists()


def test_stale_temp_files_of_expired_exports_are_removed_and_nothing_else(db, root):
    add_export(db)
    folder_exports.claim(db.worker())
    target = root / "Phim" / "成名的代价"
    target.mkdir(parents=True)
    stale = target / f".reup-export-{'0' * 32}.part"
    stale.write_bytes(b"half")
    keep = [target / "E01-7001.mp4", target / "notes.part", target / ".reup-export-x.part"]
    for path in keep:
        path.write_bytes(b"keep")
    assert folder_exports.clean_stale(db.worker(), root) == 0  # the lease is still live
    assert stale.exists()
    db.admin.execute("UPDATE folder_exports SET lease_expires_at = now() - interval '1 min'")
    assert folder_exports.clean_stale(db.worker(), root) == 1
    assert not stale.exists() and all(path.exists() for path in keep)


def test_stale_cleanup_never_follows_a_symlinked_subdir(db, root, outside):
    add_export(db)
    folder_exports.claim(db.worker())
    db.admin.execute("UPDATE folder_exports SET lease_expires_at = now() - interval '1 min'")
    (root / "Phim").symlink_to(outside, target_is_directory=True)
    (outside / "成名的代价").mkdir()
    planted = outside / "成名的代价" / f".reup-export-{'0' * 32}.part"
    planted.write_bytes(b"not ours")
    assert folder_exports.clean_stale(db.worker(), root) == 0
    assert planted.exists()


def test_a_directory_named_like_a_temp_file_neither_goes_nor_blocks_exports(db, review_store, root):
    put(review_store, FINAL, b"mp4")
    put(review_store, SRT, b"srt")
    row_id = add_export(db)
    folder_exports.claim(db.worker())
    db.admin.execute("UPDATE folder_exports SET lease_expires_at = now() - interval '1 min'")
    target = root / "Phim" / "成名的代价"
    planted = target / f".reup-export-{'2' * 32}.part"
    planted.mkdir(parents=True)
    (planted / "inside").write_bytes(b"x")
    stale = target / f".reup-export-{'3' * 32}.part"
    stale.write_bytes(b"half")
    stop = threading.Event()
    assert folder_exports.drain(db.worker(), review_store, root, min_free_gb=0,
                                stopping=stop) == 1  # fmt: skip
    assert planted.is_dir() and not stale.exists()
    assert export_row(db, row_id)["state"] == "done"


def test_a_lost_claim_between_the_mp4_and_the_srt_stops_and_settles_nothing(db, review_store, root):
    put(review_store, FINAL, b"mp4")
    put(review_store, SRT, b"srt")
    row_id = add_export(db)
    conn = db.worker()
    row = folder_exports.claim(conn)

    class Reclaimed:
        def download_fileobj(self, key, out, *, produced_by):
            review_store.download_fileobj(key, out, produced_by=produced_by)
            db.admin.execute("UPDATE folder_exports SET claim = gen_random_uuid()")

    assert folder_exports.export_claimed(row, conn=conn, store=Reclaimed(), root=root) == "lost"
    target = root / "Phim" / "成名的代价"
    assert sorted(p.name for p in target.iterdir()) == ["E01-7001.mp4"]
    assert export_row(db, row_id)["state"] == "exporting"


def test_no_read_permission_on_output_is_said_plainly(db, root):
    from botocore.exceptions import ClientError

    class Denied:
        def download_fileobj(self, key, out, *, produced_by):
            raise ClientError({"Error": {"Code": "AccessDenied"}}, "GetObject")

    row_id = add_export(db)
    assert export_one(db, Denied(), root) == "failed"
    assert export_row(db, row_id)["error"] == "Worker không có quyền đọc output/ trên MinIO."
    assert leftovers(root / "Phim" / "成名的代价") == []


def test_drain_stops_when_the_worker_stops(db, review_store, root):
    row_id = add_export(db)
    stop = threading.Event()
    stop.set()
    assert folder_exports.drain(db.worker(), review_store, root, min_free_gb=0,
                                stopping=stop) == 0  # fmt: skip
    assert export_row(db, row_id)["state"] == "pending"


def test_drain_exports_every_open_row_and_stops_when_the_disk_is_low(db, review_store, root):
    for aweme in ("7001", "7002"):
        final, srt = f"output/review/{aweme}/g1/{REV}.mp4", f"output/review/{aweme}/g1/{REV}.srt"
        put(review_store, final, aweme.encode())
        put(review_store, srt, b"srt")
        add_export(db, aweme, final=final, srt=srt)
    stop = threading.Event()
    assert folder_exports.drain(db.worker(), review_store, root, min_free_gb=10**9,
                                stopping=stop) == 0  # fmt: skip
    states = {r["state"] for r in db.admin.execute("SELECT state FROM folder_exports")}
    assert states == {"pending"}
    assert folder_exports.drain(db.worker(), review_store, root, min_free_gb=0,
                                stopping=stop) == 2  # fmt: skip
    target = root / "Phim" / "成名的代价"
    assert (target / "E01-7001.mp4").read_bytes() == b"7001"
    assert (target / "E01-7002.mp4").read_bytes() == b"7002"


# -- the worker loop ------------------------------------------------------------


class Gate:
    """The real store, holding every download until released."""

    def __init__(self, store):
        self.store = store
        self.started, self.release = threading.Event(), threading.Event()

    def download_fileobj(self, key, out, *, produced_by):
        self.started.set()
        assert self.release.wait(15)
        return self.store.download_fileobj(key, out, produced_by=produced_by)

    def __getattr__(self, name):
        return getattr(self.store, name)


@pytest.fixture
def studio_cfg(tmp_path, root) -> WorkerConfig:
    data = tmp_path / "data" / "review"
    data.mkdir(parents=True)
    return WorkerConfig(
        database_url="unused", data_root=data, work_root=tmp_path / "data" / "review-render",
        roles=frozenset({"studio"}), export_root=root,
    )  # fmt: skip


def test_studio_requests_are_handled_while_an_export_is_running(
    db, review_store, root, studio_cfg, monkeypatch
):
    put(review_store, FINAL, b"mp4")
    put(review_store, SRT, b"srt")
    row_id = add_export(db)
    monkeypatch.setattr(review_scan, "scan", lambda *a: None)
    opened = []
    real = studio_requests.handle_requests
    monkeypatch.setattr(
        studio_requests, "handle_requests",
        lambda conn, cfg, env: real(conn, cfg, env, runner=lambda c, a, g, e: opened.append(a)),
    )  # fmt: skip
    gate = Gate(review_store)
    worker = Worker(studio_cfg, gate, connector=lambda _cfg: db.worker(),
                    probe=lambda: {"ok": True})  # fmt: skip
    try:
        ticking = threading.Thread(target=worker.tick)
        ticking.start()
        ticking.join(5)
        assert not ticking.is_alive(), "the loop waited for the export"
        assert gate.started.wait(5)
        db.admin.execute(
            "INSERT INTO studio_open_requests (aweme_id, generation) VALUES ('7009', 1)"
        )
        worker.studio_thread.join(5)
        worker.tick()
        worker.studio_thread.join(5)
        assert opened == ["7009"] and worker.export_thread.is_alive()
        assert export_row(db, row_id)["state"] == "exporting"
    finally:
        gate.release.set()
    worker.export_thread.join(10)
    assert export_row(db, row_id)["state"] == "done"
    assert (root / "Phim" / "成名的代价" / "E01-7001.mp4").read_bytes() == b"mp4"


def test_an_expired_export_is_cleaned_up_before_it_is_claimed_again(db, review_store, root):
    """A worker stopped mid-download leaves its temp file and a lease that is still live when
    it starts again; the running worker reclaims the row later and must clean up then."""
    put(review_store, FINAL, b"mp4")
    put(review_store, SRT, b"srt")
    row_id = add_export(db)
    folder_exports.claim(db.worker())  # the worker that was stopped
    target = root / "Phim" / "成名的代价"
    target.mkdir(parents=True)
    stale = target / f".reup-export-{'1' * 32}.part"
    stale.write_bytes(b"half")
    stop = threading.Event()
    assert folder_exports.drain(db.worker(), review_store, root, min_free_gb=0,
                                stopping=stop) == 0  # fmt: skip
    assert stale.exists()  # its lease is live: it may still be writing
    db.admin.execute("UPDATE folder_exports SET lease_expires_at = now() - interval '1 min'")
    assert folder_exports.drain(db.worker(), review_store, root, min_free_gb=0,
                                stopping=stop) == 1  # fmt: skip
    assert not stale.exists() and export_row(db, row_id)["state"] == "done"
    assert sorted(p.name for p in target.iterdir()) == ["E01-7001.mp4", "E01-7001.srt"]


def test_without_an_export_root_the_worker_leaves_requests_pending(
    db, review_store, studio_cfg, monkeypatch
):
    row_id = add_export(db)
    monkeypatch.setattr(review_scan, "scan", lambda *a: None)
    worker = Worker(dataclasses.replace(studio_cfg, export_root=None), review_store,
                    connector=lambda _cfg: db.worker(), probe=lambda: {"ok": True})  # fmt: skip
    worker.tick()
    worker.studio_thread.join(5)
    assert worker.export_thread is None and export_row(db, row_id)["state"] == "pending"


def test_a_render_only_worker_never_exports(db, review_store, studio_cfg):
    row_id = add_export(db)
    worker = Worker(dataclasses.replace(studio_cfg, roles=frozenset({"render"})), review_store,
                    connector=lambda _cfg: db.worker(), probe=lambda: {"ok": True},
                    render=lambda row, **kw: "rendered")  # fmt: skip
    worker.tick()
    time.sleep(0.2)
    assert worker.export_thread is None and export_row(db, row_id)["state"] == "pending"


# -- settings -------------------------------------------------------------------


def env_for(tmp_path, **extra):
    return {"REVIEW_WORKER_DATA_ROOT": str(tmp_path / "data" / "review"), **extra}


def test_export_root_setting(tmp_path, monkeypatch, root):
    monkeypatch.setenv("LOC_DATABASE_URL", "postgresql://localization@db/reup")
    assert WorkerConfig.from_environ(env_for(tmp_path)).export_root is None
    assert WorkerConfig.from_environ(env_for(tmp_path, REVIEW_WORKER_EXPORT_ROOT="")).export_root \
        is None  # fmt: skip
    cfg = WorkerConfig.from_environ(env_for(tmp_path, REVIEW_WORKER_EXPORT_ROOT=str(root)))
    assert cfg.export_root == root
    afile = tmp_path / "afile"
    afile.write_text("x")
    with pytest.raises(ConfigError, match="REVIEW_WORKER_EXPORT_ROOT"):
        WorkerConfig.from_environ(env_for(tmp_path, REVIEW_WORKER_EXPORT_ROOT="relative/dir"))
    # A drive not mounted yet must not stop the heartbeat and Studio requests: each export
    # then fails with a sentence instead.
    for later in (str(tmp_path / "unmounted"), str(afile)):
        cfg = WorkerConfig.from_environ(env_for(tmp_path, REVIEW_WORKER_EXPORT_ROOT=later))
        assert cfg.export_root == Path(later)


def test_the_worker_role_may_do_everything_the_export_needs(db):
    """The column grants of the migration are the whole contract: claim, renew, settle."""
    row_id = add_export(db)
    conn = db.worker()
    row = folder_exports.claim(conn)
    ids = {"id": row_id, "claim": row["claim"]}
    assert folder_exports.renew(conn, ids)
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        conn.execute("UPDATE folder_exports SET target_subdir = 'x' WHERE id = %s", (row_id,))
    settled = conn.execute(
        folder_exports.SETTLE_SQL, {**ids, "state": "done", "error": None, "files": None}
    ).rowcount
    assert settled == 1 and export_row(db, row_id)["state"] == "done"
