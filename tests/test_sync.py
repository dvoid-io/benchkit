from types import SimpleNamespace as NS

from benchkit.langfuse_client import ensure_dataset, plan_items
from benchkit.sync import sync_dataset


class FakeClient:
    def __init__(self, existing_items=None, dataset=None):
        self.items = {i.id: i for i in (existing_items or [])}
        self.dataset = dataset
        self.created_datasets = []
        self.upserts = []
        self.flushed = False
        self.api = NS(
            datasets=NS(get=self._get_dataset),
            dataset_items=NS(list=self._list_items),
        )

    def _get_dataset(self, name):
        if self.dataset is None:
            raise _NotFound()
        return self.dataset

    def _list_items(self, dataset_name, page, limit):
        return NS(data=list(self.items.values()), meta=NS(total_pages=1))

    def create_dataset(self, **kw):
        self.created_datasets.append(kw)
        self.dataset = NS(name=kw["name"], input_schema=kw.get("input_schema"), expected_output_schema=kw.get("expected_output_schema"), description=kw.get("description"), metadata=kw.get("metadata"))
        return self.dataset

    def create_dataset_item(self, **kw):
        self.upserts.append(kw)
        self.items[kw["id"]] = NS(**kw)
        return self.items[kw["id"]]

    def flush(self):
        self.flushed = True


class _NotFound(Exception):
    status_code = 404


def _it(id, input, expected_output=None, metadata=None, status="ACTIVE"):
    return NS(id=id, input=input, expected_output=expected_output, metadata=metadata, status=status)


def test_plan_items():
    items = [_it("a", {"q": 1}, {"x": 1}, {"m": 1}), _it("b", {"q": 2}), _it("c", {"q": 3}, None, {})]
    existing = [
        _it("a", {"q": 1}, {"x": 1}, {"m": 1}),  # unchanged
        _it("b", {"q": 2}, None, None, status="ARCHIVED"),  # archived -> changed (revive)
        _it("c", {"q": 999}),  # changed
        _it("stale1", {"q": 0}),  # stale
        _it("stale2", {"q": 0}, status="ARCHIVED"),  # already archived -> not reported
    ]
    plan = plan_items(items, existing)
    assert plan.unchanged == ["a"] and plan.changed == ["b", "c"] and plan.new == [] and plan.stale == ["stale1"]
    plan = plan_items([_it("new", {})], [])
    assert plan.new == ["new"] and "new=1" in plan.format()


def test_ensure_dataset_create_and_idempotent():
    c = FakeClient()
    ds, created = ensure_dataset(c, "toy-gold", description="d", metadata={"k": 1})
    assert created and ds.name == "toy-gold"
    ds, created = ensure_dataset(c, "toy-gold", description="ignored")
    assert not created and len(c.created_datasets) == 1
    ds, created = ensure_dataset(c, "toy-gold", expected_output_schema={"type": "object"})
    assert created and c.created_datasets[-1]["description"] == "d" and c.created_datasets[-1]["metadata"] == {"k": 1}


def test_sync_dry_run_needs_no_client(toy):
    plan = sync_dataset(toy, "draft", dry_run=True, client=None)
    assert plan.dataset == "toy-draft" and plan.new == ["toy_001", "toy_002"]


def test_sync_upserts_and_archives(toy):
    c = FakeClient(existing_items=[_it("toy_001", {"question": "old"}), _it("toy_999", {"question": "gone"})])
    plan = sync_dataset(toy, "draft", client=c)
    assert c.created_datasets[0]["name"] == "toy-draft" and c.created_datasets[0]["metadata"]["benchmark"] == "toy"
    assert plan.changed == ["toy_001"] and plan.new == ["toy_002"] and plan.stale == ["toy_999"] and plan.archived == []
    assert [u["id"] for u in c.upserts] == ["toy_001", "toy_002"]
    assert all(u["status"] == "ACTIVE" for u in c.upserts)
    assert c.upserts[1]["input"] == {"question": "What is 2 + 2?"} and c.upserts[1]["metadata"] == {"status": "reviewed", "tags": ["math"]}
    assert c.flushed
    # second sync: nothing changed; archive stale
    c.upserts.clear()
    plan = sync_dataset(toy, "draft", client=c, archive_stale=True)
    assert plan.unchanged == ["toy_001", "toy_002"] and plan.archived == ["toy_999"]
    assert c.upserts == [dict(dataset_name="toy-draft", id="toy_999", input={"question": "gone"}, expected_output=None, metadata=None, status="ARCHIVED")]
    # third: stale item is archived -> not stale anymore
    plan = sync_dataset(toy, "draft", client=c)
    assert plan.stale == []


def test_sync_prefixed_ids(toy):
    c = FakeClient()
    plan = sync_dataset(toy, "tagged", client=c)
    assert plan.new == ["tagged:toy_002", "tagged:toy_003"]
