from datetime import UTC, datetime
from unittest.mock import MagicMock

import pytest

from localization.starter.review_ready import READY_SQL, read_ready


def test_discovery_uses_a_read_only_transaction_and_binds_activation_time():
    conn = MagicMock()
    since = datetime(2026, 11, 10, tzinfo=UTC)
    conn.execute.return_value.fetchall.return_value = [
        {"aweme_id": "7001", "generation": 2, "status": "needs_human"}
    ]
    assert read_ready(conn, since)[0]["status"] == "needs_human"
    assert conn.execute.call_args_list[0].args == ("SET TRANSACTION READ ONLY",)
    assert conn.execute.call_args_list[-1].args == (READY_SQL, (since,))


def test_naive_start_time_is_rejected_before_querying():
    conn = MagicMock()
    with pytest.raises(ValueError, match="timezone"):
        read_ready(conn, datetime(2026, 11, 10))
    conn.execute.assert_not_called()
