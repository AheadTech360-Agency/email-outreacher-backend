"""The website reader only reads public addresses, on the first request and on every redirect."""

import asyncio

import httpx
import pytest

from services.website import PublicOnlyTransport, _resolve

ADDRESSES = {"shop.example": ["93.184.216.34"], "inside.example": ["10.0.0.5"], "mixed.example": ["93.184.216.34", "127.0.0.1"],
             "railway.example": ["fd12::1"]}  # fmt: skip


async def fake_resolve(host):
    return ADDRESSES.get(host) or await _resolve(host)


def answer(request):
    if request.url.host == "shop.example" and request.url.path == "/go":
        return httpx.Response(302, headers={"Location": "http://inside.example/admin"})
    return httpx.Response(200, text="hello")


def fetch(url):
    async def go():
        transport = PublicOnlyTransport(httpx.MockTransport(answer), resolve=fake_resolve)
        async with httpx.AsyncClient(transport=transport, follow_redirects=True) as client:
            return await client.get(url)

    return asyncio.run(go())


def test_a_public_site_is_read() -> None:
    assert fetch("https://shop.example/").text == "hello"


@pytest.mark.parametrize("url", ["http://inside.example/", "http://mixed.example/", "http://railway.example/", "http://127.0.0.1/",
                                 "http://169.254.169.254/latest/meta-data/", "http://[::1]/", "http://localhost/"])  # fmt: skip
def test_private_addresses_are_refused(url) -> None:
    with pytest.raises(httpx.ConnectError):
        fetch(url)


def test_a_redirect_into_a_private_network_is_refused() -> None:
    with pytest.raises(httpx.ConnectError, match="inside.example"):
        fetch("https://shop.example/go")


# ---------- At connect time (DNS rebinding) ----------

from services.website import PublicOnlyBackend, pinned_transport  # noqa: E402


class Rebinding:
    """Answers a public address the first time and a private one after: the second answer is the one connected to."""

    def __init__(self) -> None:
        self.n = 0

    async def __call__(self, host):
        self.n += 1
        return ["93.184.216.34"] if self.n == 1 else ["127.0.0.1"]


class Recorder:
    def __init__(self) -> None:
        self.hosts = []

    async def connect_tcp(self, host, port, **kw):
        self.hosts.append(host)
        raise httpcore.ConnectError("stop here")  # no real connection in tests

    async def sleep(self, seconds):
        pass


import httpcore  # noqa: E402


def test_a_name_that_switches_to_a_private_address_is_refused_when_connecting() -> None:
    resolve, inner = Rebinding(), Recorder()

    async def go():
        transport = PublicOnlyTransport(pinned_transport(PublicOnlyBackend(inner, resolve=resolve)), resolve=resolve)
        async with httpx.AsyncClient(transport=transport) as client:
            await client.get("https://rebind.example/")

    with pytest.raises(httpx.ConnectError, match="isn't a public address"):
        asyncio.run(go())
    assert inner.hosts == []  # never connected


def test_it_connects_to_the_address_it_checked() -> None:
    inner = Recorder()

    async def go():
        async with httpx.AsyncClient(transport=pinned_transport(PublicOnlyBackend(inner, resolve=fake_resolve))) as client:
            await client.get("https://shop.example/")

    with pytest.raises(httpx.ConnectError):
        asyncio.run(go())
    assert inner.hosts == ["93.184.216.34"]
