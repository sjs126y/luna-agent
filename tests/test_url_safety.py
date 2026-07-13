from __future__ import annotations


def test_url_safety_checks_every_dns_answer(monkeypatch):
    import socket

    from personal_agent.tools.url_safety import check_url

    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda *_args, **_kwargs: [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 0)),
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 0)),
        ],
    )

    error = check_url("https://mixed.example/path")

    assert error is not None
    assert "127.0.0.1" in error


def test_private_opt_in_never_allows_link_local_metadata():
    from personal_agent.tools.url_safety import check_url

    error = check_url("http://169.254.169.254/latest", allow_private=True)

    assert error is not None
    assert "metadata" in error
