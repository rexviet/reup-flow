import concurrent.futures

import pytest

from localization.review_bridge.auth import Auth, Unauthorized


def test_grant_is_single_use_across_instances_and_sessions_rotate(tmp_path):
    auth = Auth(tmp_path / "private")
    grant = auth.issue("7001", 1)
    with concurrent.futures.ThreadPoolExecutor(2) as pool:
        futures = [pool.submit(Auth(auth.root).redeem, grant) for _ in range(2)]
    success = [f.result() for f in futures if f.exception() is None]
    assert len(success) == 1
    assert isinstance(next(f.exception() for f in futures if f.exception()), Unauthorized)
    cookie, record = success[0]
    assert auth.session(cookie)["csrf"] == record["csrf"]
    auth.issue("7001", 1)
    with pytest.raises(Unauthorized, match="replaced"):
        auth.session(cookie)
    assert auth.root.stat().st_mode & 0o777 == 0o700


def test_expiry_and_path_traversal(tmp_path):
    now = [100.0]
    auth = Auth(tmp_path, now=lambda: now[0])
    grant = auth.issue("7001", 1)
    now[0] += 60
    with pytest.raises(Unauthorized, match="expired"):
        auth.redeem(grant)
    with pytest.raises(Unauthorized):
        auth.redeem("../outside")
    cookie, _ = auth.redeem(auth.issue("7001", 1))
    now[0] += 8 * 3600
    with pytest.raises(Unauthorized, match="expired"):
        auth.session(cookie)
