"""The Apify client against a fake Apify (httpx.MockTransport). No network, no cost."""

import asyncio
import functools
import json
from datetime import UTC, datetime

import httpx
import pytest

from services.apify import ApifyClient, ApifyError, best_email, instagram_handle, linkedin_slug


def sync(fn):
    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        return asyncio.run(fn(*args, **kwargs))

    return wrapper


def fake_apify(items, *, status_after_poll="SUCCEEDED", start_status="RUNNING", seen=None, cost=0.0123):
    """Starts a run, reports it finished on the first poll, then serves `items` as its dataset."""
    seen = seen if seen is not None else {}

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        seen.setdefault("calls", []).append((request.method, path))
        seen["auth"] = request.headers.get("authorization")
        if request.method == "POST" and path.endswith("/runs"):
            seen["input"] = json.loads(request.content)
            seen["actor"] = path.split("/acts/")[1].split("/")[0]
            return httpx.Response(201, json={"data": {"id": "run1", "status": start_status, "defaultDatasetId": "ds1"}})
        if path == "/v2/actor-runs/run1":
            return httpx.Response(200, json={"data": {"id": "run1", "status": status_after_poll, "defaultDatasetId": "ds1",
                                                      "usageTotalUsd": cost, "statusMessage": "boom"}})  # fmt: skip
        if path == "/v2/datasets/ds1/items":
            seen["limit"] = request.url.params.get("limit")
            return httpx.Response(200, json=items)
        return httpx.Response(404, json={"error": {"message": "unexpected " + path}})

    return httpx.MockTransport(handler), seen


def client(transport) -> ApifyClient:
    return ApifyClient("tok", transport=transport, poll_seconds=0)


def test_slug_and_handle() -> None:
    assert linkedin_slug("https://www.linkedin.com/in/Ava-Morgan/?x=1") == "ava-morgan"
    assert linkedin_slug("https://linkedin.com/company/google") == "google"
    assert instagram_handle("instagram.com/SameDayCustom/") == "samedaycustom"
    assert instagram_handle("https://www.instagram.com/p/DLNsnpUTdVS/") is None
    assert linkedin_slug(None) is None and instagram_handle("") is None


@sync
async def test_employees_runs_the_actor_and_maps_people() -> None:
    items = [
        {"id": "e1", "firstName": "Ava", "lastName": "Morgan", "headline": "Owner at X",
         "linkedinUrl": "https://www.linkedin.com/in/ava-morgan", "currentPosition": [{"position": "Owner", "companyName": "SameDayCustom"}]},
        {"publicIdentifier": "bo-chen", "firstName": "Bo", "lastName": "Chen", "headline": "CEO", "currentPosition": []},
        {"firstName": "No id"},
    ]  # fmt: skip
    transport, seen = fake_apify(items)
    c = client(transport)
    people, run = await c.company_employees("SameDayCustom", ["Owner"], 10)
    await c.aclose()
    assert seen["actor"] == "harvestapi~linkedin-company-employees"
    assert seen["auth"] == "Bearer tok"
    assert seen["input"] == {"companies": ["SameDayCustom"], "profileScraperMode": "Short ($4 per 1k)", "maxItems": 10,
                             "companyBatchMode": "all_at_once", "jobTitles": ["Owner"]}  # fmt: skip
    assert [(p.external_id, p.first_name, p.last_name, p.title, p.company_name) for p in people] == [
        ("e1", "Ava", "Morgan", "Owner", "SameDayCustom"),
        ("bo-chen", "Bo", "Chen", "CEO", None),
    ]
    assert run.cost_usd == 0.0123 and run.run_id == "run1"


@sync
async def test_employees_reads_the_actors_current_output() -> None:
    """What the actor returned on 2026-10-31: `currentPositions`, with `title` and the company under `company`."""
    items = [
        {"id": "ACwAAFd", "firstName": "Mark", "lastName": "Cordova", "linkedinUrl": "https://www.linkedin.com/in/ACwAAFd",
         "currentPositions": [{"title": "Owner", "companyName": "Chicago Shirt Prints", "company": {"name": "Chicago Shirt Prints"}}]},
        {"id": "e2", "firstName": "Jo", "currentPositions": [{"title": "Manager", "company": {"name": "Ink Co"}}]},
    ]  # fmt: skip
    transport, _ = fake_apify(items)
    c = client(transport)
    people, _ = await c.company_employees("Chicago Shirt Prints", [], 2)
    await c.aclose()
    # Jo works at Ink Co, not Chicago Shirt Prints: LinkedIn's name search can return them, they aren't kept.
    assert [(p.title, p.company_name) for p in people] == [("Owner", "Chicago Shirt Prints")]


@sync
async def test_a_listing_name_is_searched_without_its_seo_tail_and_lookalikes_are_dropped() -> None:
    """2026-11-02: "Quick Transfers - Same Day Custom DTF Transfers" found a freelancer at "Quick Airport Transfers"."""
    items = [
        {"id": "a", "firstName": "Hammad", "currentPositions": [{"position": "SEO Specialist", "companyName": "Quick Airport Transfers"}]},
        {"id": "b", "firstName": "Dana", "currentPositions": [{"position": "Owner", "companyName": "QuickTransfers, LLC"}]},
    ]  # fmt: skip
    transport, seen = fake_apify(items)
    c = client(transport)
    people, run = await c.company_employees("Quick Transfers - Same Day Custom DTF Transfers", ["Owner"], 5, domain="quicktransfers.com")
    await c.aclose()
    assert seen["input"]["companies"] == ["Quick Transfers"]
    assert [p.first_name for p in people] == ["Dana"] and run.skipped_elsewhere == 1


@sync
async def test_the_linkedin_page_is_searched_when_known_and_matches_by_url() -> None:
    page = "https://www.linkedin.com/company/qt-dtf/"
    items = [{"id": "a", "firstName": "Sam", "currentPositions": [{"position": "CEO", "companyName": "QT", "companyLinkedinUrl": "https://linkedin.com/company/qt-dtf"}]}]
    transport, seen = fake_apify(items)
    c = client(transport)
    people, _ = await c.company_employees("Quick Transfers", [], 5, linkedin_url=page)
    await c.aclose()
    assert seen["input"]["companies"] == [page] and [p.first_name for p in people] == ["Sam"]


@sync
async def test_linkedin_posts_input_and_mapping() -> None:
    items = [
        {"type": "post", "id": "7329", "linkedinUrl": "https://www.linkedin.com/posts/ava_x-activity-7329",
         "content": "We just opened a second shop", "author": {"publicIdentifier": "Ava-Morgan"},
         "postedAt": {"timestamp": 1747419119821, "date": "2025-05-16T18:11:59.821Z"}, "postImages": [{"url": "x"}]},
        {"type": "comment", "linkedinUrl": "https://x"},
    ]  # fmt: skip
    transport, seen = fake_apify(items)
    c = client(transport)
    posts, _ = await c.linkedin_posts(["https://www.linkedin.com/in/ava-morgan"], 5, datetime(2026, 9, 1, tzinfo=UTC))
    await c.aclose()
    assert seen["actor"] == "harvestapi~linkedin-profile-posts"
    assert seen["input"]["targetUrls"] == ["https://www.linkedin.com/in/ava-morgan"]
    assert seen["input"]["maxPosts"] == 5 and seen["input"]["postedLimitDate"] == "2026-09-01"
    assert seen["input"]["scrapeComments"] is False and seen["input"]["scrapeReactions"] is False
    [p] = posts
    assert (p.source, p.text, p.media_type, p.owner_key) == ("linkedin", "We just opened a second shop", "image", "ava-morgan")
    assert p.posted_at == datetime(2025, 5, 16, 18, 11, 59, 821000, tzinfo=UTC)


@sync
async def test_instagram_posts_input_and_mapping() -> None:
    items = [
        {"url": "https://www.instagram.com/p/A1/", "id": "1", "type": "Sidecar", "caption": "New colours in",
         "timestamp": "2026-10-01T10:00:00.000Z", "ownerUsername": "SameDayCustom"},
        {"error": "not_found", "url": "https://www.instagram.com/nobody/"},
    ]  # fmt: skip
    transport, seen = fake_apify(items)
    c = client(transport)
    posts, _ = await c.instagram_posts(["https://www.instagram.com/samedaycustom/"], 3, datetime(2026, 9, 1, tzinfo=UTC))
    await c.aclose()
    assert seen["actor"] == "apify~instagram-scraper"
    assert seen["input"] == {"directUrls": ["https://www.instagram.com/samedaycustom/"], "resultsType": "posts",
                             "resultsLimit": 3, "onlyPostsNewerThan": "2026-09-01", "addParentData": False}  # fmt: skip
    [p] = posts
    assert (p.source, p.media_type, p.owner_key, p.text) == ("instagram", "carousel", "samedaycustom", "New colours in")


@sync
async def test_failed_run_is_reported() -> None:
    transport, _ = fake_apify([], status_after_poll="FAILED")
    c = client(transport)
    with pytest.raises(ApifyError) as exc:
        await c.instagram_posts(["https://www.instagram.com/x/"], 3, datetime.now(UTC))
    await c.aclose()
    assert "ended as FAILED" in exc.value.message


@pytest.mark.parametrize(("status", "text"), [(401, "rejected the token"), (402, "out of credit"), (429, "rate limit")])
@sync
async def test_refusals_are_readable(status: int, text: str) -> None:
    transport = httpx.MockTransport(lambda r: httpx.Response(status, json={"error": {"message": "nope"}}))
    c = client(transport)
    with pytest.raises(ApifyError) as exc:
        await c.company_employees("X", [], 5)
    await c.aclose()
    assert exc.value.status == status and text in exc.value.message


@sync
async def test_plan_limit_hidden_in_a_succeeded_run_is_an_error() -> None:
    transport, _ = fake_apify([], status_after_poll="SUCCEEDED")

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v2/actor-runs/run1":
            return httpx.Response(200, json={"data": {"id": "run1", "status": "SUCCEEDED", "defaultDatasetId": "ds1",
                                                      "statusMessage": "free user run limit exceeded"}})  # fmt: skip
        return transport.handle_request(request)

    c = client(httpx.MockTransport(handler))
    with pytest.raises(ApifyError) as exc:
        await c.company_employees("X", [], 3)
    await c.aclose()
    assert exc.value.status == 402 and "free user run limit exceeded" in exc.value.message


@sync
async def test_linkedin_repost_is_matched_by_query_and_flagged() -> None:
    items = [{"type": "post", "id": "1", "linkedinUrl": "https://li/1", "content": "x",
              "author": {"publicIdentifier": "someone-else"}, "query": {"targetUrl": "https://www.linkedin.com/in/ava-morgan/"},
              "header": {"text": "Ava Morgan reposted this"}}]  # fmt: skip
    transport, _ = fake_apify(items)
    c = client(transport)
    [p], _ = await c.linkedin_posts(["https://www.linkedin.com/in/ava-morgan/"], 2, datetime.now(UTC))
    await c.aclose()
    assert p.owner_key == "ava-morgan" and p.is_repost


@sync
async def test_instagram_profiles_keep_raw_data_and_latest_posts() -> None:
    items = [{
        "username": "InkCo", "fullName": "Ink Co", "biography": "Custom tees", "followersCount": 1200,
        "businessCategoryName": "Printing service", "private": False, "verified": False,
        "latestPosts": [{"url": "https://www.instagram.com/p/X1/", "id": "9", "type": "Video", "caption": "Fresh drop",
                         "timestamp": "2026-10-20T10:00:00.000Z"}],
    }]  # fmt: skip
    transport, seen = fake_apify(items)
    c = client(transport)
    profiles, _ = await c.instagram_profiles(["@InkCo"])
    await c.aclose()
    assert seen["input"] == {"usernames": ["inkco"]}
    ink = profiles["inkco"]
    assert ink.raw["biography"] == "Custom tees" and "latestPosts" not in ink.raw
    [post] = ink.posts
    assert (post.url, post.media_type, post.text, post.owner_key) == ("https://www.instagram.com/p/X1/", "video", "Fresh drop", "inkco")


def test_the_best_email_is_valid_deliverable_work_email() -> None:
    """The shape the actor's email search returned on 2026-11-02."""
    item = {"emails": [
        {"email": "Cory@iodev.com", "catchAllDomain": True, "free": False, "status": "risky", "qualityScore": 60},
        {"email": "cory.keane@gmail.com", "free": True, "status": "valid", "deliverable": True, "qualityScore": 95},
        {"email": "ck@iodev.com", "free": False, "status": "valid", "deliverable": True, "qualityScore": 90}]}  # fmt: skip
    assert best_email(item) == ("ck@iodev.com", "valid")
    assert best_email({"emails": [item["emails"][0]]}) == ("cory@iodev.com", "risky")  # risky is kept, and says so
    assert best_email({"emails": []}) == (None, None) and best_email({}) == (None, None)


@sync
async def test_employees_with_email_use_the_email_mode() -> None:
    items = [{"id": "k1", "firstName": "Kevin", "currentPosition": [{"position": "Founder, CEO", "companyName": "UpMerch"}],
              "emails": [{"email": "kevin@upmerch.com", "status": "valid", "deliverable": True, "qualityScore": 100}]}]  # fmt: skip
    transport, seen = fake_apify(items)
    c = client(transport)
    [kevin], _ = await c.company_employees("UpMerch", [], 1, with_email=True)
    await c.aclose()
    assert seen["input"]["profileScraperMode"] == "Full + email search ($12 per 1k)"
    assert (kevin.title, kevin.email, kevin.email_status) == ("Founder, CEO", "kevin@upmerch.com", "valid")
