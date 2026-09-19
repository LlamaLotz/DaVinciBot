import pytest


@pytest.fixture(autouse=True)
def forbid_real_cloud_requests(monkeypatch):
    """A changed adapter must never turn a mocked contract test into a paid API call."""
    import httpx

    def blocked(*args, **kwargs):
        raise AssertionError("Real network access is prohibited in tests; mock the transport")
    monkeypatch.setattr(httpx, "stream", blocked)
    monkeypatch.setattr(httpx, "post", blocked)
