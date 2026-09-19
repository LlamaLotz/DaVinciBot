from pathlib import Path

from davincibot.cache import ManagedCache
from davincibot.database import Database
from davincibot.models import JobSpec, ProviderKind, WorkspaceMode, WorkspaceProfile


def test_database_round_trip(tmp_path: Path) -> None:
    database = Database(tmp_path / "app.db")
    profile = WorkspaceProfile(name="Short", mode=WorkspaceMode.SHORT_FORM)
    database.save_profile(profile)
    assert database.get_profile(profile.id) == profile

    spec = JobSpec(
        profile_id=profile.id,
        prompt="Make a fast educational video",
        provider=ProviderKind.OFFLINE,
        target_project="Demo",
    )
    record = database.create_job(spec)
    assert database.get_job(record.id).spec.prompt == spec.prompt
    database.close()


def test_cache_key_changes_and_cleanup_stays_inside_root(tmp_path: Path) -> None:
    source = tmp_path / "source.mov"
    source.write_bytes(b"video")
    cache = ManagedCache(tmp_path / "cache")
    first = cache.key(source, {"quality": 1})
    second = cache.key(source, {"quality": 2})
    assert first != second
    cached = cache.path_for(first, ".wav")
    cached.parent.mkdir(parents=True)
    cached.write_bytes(b"x" * 100)
    outside = tmp_path / "outside.wav"
    outside.write_bytes(b"do not delete")
    removed = cache.cleanup(0)
    assert cached in removed
    assert outside.exists()
