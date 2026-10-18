"""The index cleanup: which keys are stale, what is deleted from which index, and the HTTP calls."""

import base64
import json
from pathlib import Path

import httpx
import pytest

from leaklab.setup_a.cleanup import (
    DeleteResult,
    GenerationRegistry,
    HttpIndexAdmin,
    cleanup_indexes,
    item_of,
    split_keys,
)
from leaklab.setup_a.keys import doc_key

SITE, DRIVE = "host,guid1,guid2", "b!drive"


def key(item: str) -> str:
    return doc_key(SITE, DRIVE, item)


CURRENT = {"ITEM-1", "ITEM-2", "ITEM-3"}
OLD = {"OLD-1", "OLD-2", "OLD-3", "OLD-4"}


class FakeAdmin:
    """An index with keys. `filtered` means the service token cannot list it."""

    def __init__(self, index: str, keys: set[str], *, filtered: bool = False) -> None:
        self.index = index
        self.keys = set(keys)
        self.filtered = filtered
        self.deleted: list[list[str]] = []
        self.fail: set[str] = set()
        self.lag_polls = 0  # count reads that still show the old number

    def count_search(self) -> int | None:
        return 0 if self.filtered else len(self.keys)

    def count_stats(self) -> int | None:
        if self.lag_polls > 0:
            self.lag_polls -= 1
            return len(self.keys) + len(self.deleted[-1] if self.deleted else [])
        return len(self.keys)

    def list_keys(self) -> list[str]:
        return [] if self.filtered else sorted(self.keys)

    def delete_keys(self, keys: list[str]) -> DeleteResult:  # type: ignore[override]
        self.deleted.append(list(keys))
        bad = [k for k in keys if k in self.fail]
        for k in keys:
            if k not in self.fail:
                self.keys.discard(k)
        return DeleteResult(requested=len(keys), succeeded=len(keys) - len(bad), failed=len(bad))


def both(current: set[str] = CURRENT, old: set[str] = OLD) -> tuple[FakeAdmin, FakeAdmin]:
    keys = {key(i) for i in current | old}
    return FakeAdmin("b", keys), FakeAdmin("a", keys, filtered=True)


def run(b: FakeAdmin, a: FakeAdmin, registry: GenerationRegistry, **kw):  # type: ignore[no-untyped-def]
    return cleanup_indexes(
        unfiltered=b,
        filtered=a,
        generation="GNOW",
        current_item_ids=CURRENT,
        site_id=SITE,
        drive_id=DRIVE,
        registry=registry,
        settle_s=0.0,
        sleep=lambda s: None,
        **kw,
    )


def test_keys_are_split_into_current_stale_and_foreign() -> None:
    foreign = base64.urlsafe_b64encode(b"not::a-library-doc").decode()
    keys = [key(i) for i in sorted(CURRENT | OLD)] + [foreign, "###"]
    current, stale, other = split_keys(keys, CURRENT)
    assert {item_of(k) for k in current} == CURRENT
    assert {item_of(k) for k in stale} == OLD
    assert set(other) == {foreign, "###"}


def test_the_unfiltered_index_is_listed_and_only_stale_keys_are_deleted(tmp_path: Path) -> None:
    b, a = both()
    report = run(b, a, GenerationRegistry(tmp_path / "g.json"))
    assert {item_of(k) for k in b.keys} == CURRENT
    first = report.indexes[0]
    assert (first.listed, first.current_found, first.stale_found, first.deleted) == (7, 3, 4, 4)
    assert (first.count_search_before, first.count_search_after) == (7, 3)
    assert first.failed == 0 and first.foreign_left == 0


def test_the_filtered_index_cannot_be_listed_so_its_stale_keys_come_from_the_unfiltered_one(
    tmp_path: Path,
) -> None:
    b, a = both()
    report = run(b, a, GenerationRegistry(tmp_path / "g.json"))
    assert {item_of(k) for k in a.keys} == CURRENT
    second = report.indexes[1]
    assert second.listed is None and second.stale_requested == 4 and second.deleted == 4
    assert second.count_search_before == 0  # what the service token sees in a trimmed index


def test_the_registry_covers_generations_the_unfiltered_index_no_longer_shows(
    tmp_path: Path,
) -> None:
    registry = GenerationRegistry(tmp_path / "g.json")
    registry.register("GOLD", {"OLD-1", "OLD-2", "ANCIENT-9"})
    b = FakeAdmin("b", {key(i) for i in CURRENT})  # B was already cleaned
    a = FakeAdmin("a", {key(i) for i in CURRENT | {"ANCIENT-9"}}, filtered=True)
    report = run(b, a, registry)
    assert {item_of(k) for k in a.keys} == CURRENT
    assert report.indexes[0].stale_found == 0 and report.indexes[1].deleted == 3
    assert report.registry_generations == 1


def test_an_elevated_listing_adds_its_stale_keys(tmp_path: Path) -> None:
    b = FakeAdmin("b", {key(i) for i in CURRENT})
    a = FakeAdmin("a", {key(i) for i in CURRENT | {"HIST-1", "HIST-2"}}, filtered=True)
    report = run(b, a, GenerationRegistry(tmp_path / "g.json"), elevated_keys=sorted(a.keys))
    assert {item_of(k) for k in a.keys} == CURRENT
    assert report.indexes[1].listed == 5 and report.indexes[1].deleted == 2


def test_current_keys_are_never_requested_for_deletion_even_if_the_registry_lists_them(
    tmp_path: Path,
) -> None:
    registry = GenerationRegistry(tmp_path / "g.json")
    registry.register("GOLD", CURRENT | {"OLD-1"})  # a registry that wrongly names current items
    b, a = both(old={"OLD-1"})
    run(b, a, registry)
    deleted = {item_of(k) for batch in a.deleted + b.deleted for k in batch}
    assert deleted == {"OLD-1"}
    assert {item_of(k) for k in a.keys} == CURRENT == {item_of(k) for k in b.keys}


def test_nothing_is_deleted_when_the_current_set_is_empty(tmp_path: Path) -> None:
    b, a = both()
    with pytest.raises(ValueError, match="refusing"):
        cleanup_indexes(
            unfiltered=b,
            filtered=a,
            generation="G",
            current_item_ids=set(),
            site_id=SITE,
            drive_id=DRIVE,
            registry=GenerationRegistry(tmp_path / "g.json"),
        )
    assert b.deleted == [] and a.deleted == [] and len(b.keys) == 7


def test_foreign_keys_are_left_alone(tmp_path: Path) -> None:
    foreign = base64.urlsafe_b64encode(b"x::y").decode()
    b = FakeAdmin("b", {key(i) for i in CURRENT | OLD} | {foreign})
    a = FakeAdmin("a", set(), filtered=True)
    report = run(b, a, GenerationRegistry(tmp_path / "g.json"))
    assert foreign in b.keys and report.indexes[0].foreign_left == 1


def test_a_dry_run_changes_nothing_and_reports_what_it_would_do(tmp_path: Path) -> None:
    b, a = both()
    report = run(b, a, GenerationRegistry(tmp_path / "g.json"), dry_run=True)
    assert b.deleted == [] and a.deleted == [] and len(b.keys) == 7
    assert (report.indexes[0].stale_found, report.indexes[0].deleted) == (4, 0)
    assert report.indexes[1].stale_requested == 4


def test_failed_deletes_are_reported_not_hidden(tmp_path: Path) -> None:
    b, a = both()
    b.fail = {key("OLD-1")}
    report = run(b, a, GenerationRegistry(tmp_path / "g.json"))
    assert report.indexes[0].failed == 1 and report.indexes[0].deleted == 3
    assert key("OLD-1") in b.keys


def test_counts_are_polled_until_the_deletes_show(tmp_path: Path) -> None:
    b, a = both()
    b.lag_polls = 2  # the statistic still shows the old number twice
    clock = {"t": 0.0}
    slept: list[float] = []

    def sleep(s: float) -> None:
        slept.append(s)
        clock["t"] += s

    report = cleanup_indexes(
        unfiltered=b,
        filtered=a,
        generation="GNOW",
        current_item_ids=CURRENT,
        site_id=SITE,
        drive_id=DRIVE,
        registry=GenerationRegistry(tmp_path / "g.json"),
        settle_s=60.0,
        sleep=sleep,
        clock=lambda: clock["t"],
    )
    assert report.indexes[0].count_search_after == 3  # the search count was right at once
    assert report.indexes[0].count_stats_after in (3, 7, 4)  # whatever the last poll saw


def test_the_registry_only_grows_and_persists(tmp_path: Path) -> None:
    path = tmp_path / "sub" / "g.json"
    first = GenerationRegistry(path)
    first.register("G1", {"a", "b"})
    first.register("G1", {"b", "c"})
    first.register("G2", {"z"})
    again = GenerationRegistry(path)
    assert again.generations() == ["G1", "G2"]
    assert again.other_item_ids("G1") == {"z"} and again.other_item_ids("G2") == {"a", "b", "c"}
    assert json.loads(path.read_text(encoding="utf-8"))["generations"]["G1"]["item_ids"] == [
        "a",
        "b",
        "c",
    ]
    assert GenerationRegistry(tmp_path / "missing.json").other_item_ids("G") == set()


# -- the HTTP calls -------------------------------------------------------------------------


def admin(handler):  # type: ignore[no-untyped-def]
    seen: list[httpx.Request] = []

    def wrapped(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return handler(request)

    return (
        HttpIndexAdmin(
            "https://svc.search.windows.net/",
            "ll-sp-index-b",
            client=httpx.Client(transport=httpx.MockTransport(wrapped)),
            token_source=lambda: "service-bearer",
            clock=lambda: 0.0,
        ),
        seen,
    )


def test_counts_use_search_and_statistics() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/stats"):
            return httpx.Response(200, json={"documentCount": 45})
        return httpx.Response(200, json={"@odata.count": 45, "value": []})

    a, seen = admin(handler)
    assert (a.count_search(), a.count_stats()) == (45, 45)
    assert json.loads(seen[0].content) == {"search": "*", "count": True, "top": 0}
    assert seen[0].headers["authorization"] == "Bearer service-bearer"


def test_listing_pages_through_the_index() -> None:
    pages = [[{"id": f"k{i}"} for i in range(1000)], [{"id": "last"}]]

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        return httpx.Response(200, json={"value": pages[body["skip"] // 1000]})

    a, seen = admin(handler)
    keys = a.list_keys()
    assert len(keys) == 1001 and keys[-1] == "last" and len(seen) == 2


def test_deletes_go_in_batches_with_the_delete_action_and_failures_are_counted() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        actions = json.loads(request.content)["value"]
        assert request.url.path == "/indexes/ll-sp-index-b/docs/index"
        assert all(x["@search.action"] == "delete" for x in actions)
        rows = [
            {
                "key": x["id"],
                "status": x["id"] != "bad",
                "errorMessage": None if x["id"] != "bad" else "boom",
            }
            for x in actions
        ]
        return httpx.Response(200, json={"value": rows})

    a, seen = admin(handler)
    result = a.delete_keys([f"k{i}" for i in range(1100)] + ["bad"])
    assert len(seen) == 3 and result.requested == 1101
    assert (result.succeeded, result.failed) == (1100, 1) and "boom" in result.errors[0]


def test_a_non_200_is_an_error_and_never_leaks_the_token() -> None:
    a, _ = admin(lambda r: httpx.Response(403, text="forbidden"))
    with pytest.raises(RuntimeError) as err:
        a.count_search()
    assert "403" in str(err.value) and "service-bearer" not in str(err.value)
