"""Which outside service did the work is counted where the call is dispatched.

"How many installs use provider X" could not be answered: the
``generations`` counter says a model was made and not who made it, and
which marketplace a file came from was named by one tool on one of its
two paths.  A generation sent to a provider with the user's own key, and
a marketplace searched from their own machine, make no call to Kiln's
servers, so the day file is the only place either can be seen.

The count therefore lives in the two base classes every provider and
every marketplace inherits, keyed by the name each gives itself.  These
tests hold that: a service nobody has written yet is counted with no
wiring, a call that failed is not use, one use is never counted twice,
and nothing but the name is written down.
"""

from __future__ import annotations

import warnings

import pytest

import kiln.generation  # noqa: F401 — registers the shipped providers
import kiln.marketplaces  # noqa: F401 — registers the shipped adapters
from kiln import daily_stats
from kiln.generation.base import (
    GenerationError,
    GenerationJob,
    GenerationProvider,
    GenerationStatus,
)
from kiln.marketplaces import MarketplaceRegistry
from kiln.marketplaces.base import MarketplaceAdapter, MarketplaceError
from kiln.marketplaces.thingiverse import ThingiverseAdapter
from kiln.thingiverse import MARKETPLACE_NAME, ThingiverseClient

PROMPT = "a bracket for my unreleased product, model tripo-v9-secret"
QUERY = "replacement hinge for the thing in my garage"


# ---------------------------------------------------------------------------
# Stand-ins for a service nobody has written yet
# ---------------------------------------------------------------------------


class _LaterProvider(GenerationProvider):
    """A provider added after this code shipped: no registry row, no map entry."""

    def __init__(self, *, outcome: GenerationStatus | Exception = GenerationStatus.PENDING) -> None:
        self._outcome = outcome

    @property
    def name(self) -> str:
        return "laterprov"

    @property
    def display_name(self) -> str:
        return "Later"

    def generate(self, prompt, *, format="stl", style=None, **kwargs):
        if isinstance(self._outcome, Exception):
            raise self._outcome
        return GenerationJob(id="job-1", provider=self.name, prompt=prompt, status=self._outcome)

    def get_job_status(self, job_id):  # pragma: no cover — not exercised
        raise NotImplementedError

    def download_result(self, job_id, output_dir=""):  # pragma: no cover
        raise NotImplementedError


class _BuiltOnLater(_LaterProvider):
    """A provider that reuses another's generate, the way a V2 reuses a V1."""

    @property
    def name(self) -> str:
        return "builtonlater"

    def generate(self, prompt, *, format="stl", style=None, **kwargs):
        return super().generate(prompt, format=format, style=style, **kwargs)


class _LaterMarketplace(MarketplaceAdapter):
    """A marketplace added after this code shipped."""

    def __init__(self, name: str, *, down: bool = False) -> None:
        self._name = name
        self._down = down

    @property
    def name(self) -> str:
        return self._name

    @property
    def display_name(self) -> str:
        return self._name.title()

    def search(self, query, *, page=1, per_page=20, sort="relevant"):
        if self._down:
            raise MarketplaceError("unreachable")
        return []

    def get_details(self, model_id):  # pragma: no cover — not exercised
        raise NotImplementedError

    def get_files(self, model_id):
        return []

    def download_file(self, file_id, dest_dir, *, file_name=None):
        if self._down:
            raise MarketplaceError("unreachable")
        return f"{dest_dir}/{file_id}.stl"


class _MetadataOnly(_LaterMarketplace):
    """Search only: inherits the base ``download_file``, which refuses."""

    download_file = MarketplaceAdapter.download_file


def _counts(key: str) -> dict:
    return daily_stats.get_daily_stats()[key]


def _thingiverse_client(monkeypatch) -> ThingiverseClient:
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", DeprecationWarning)
        client = ThingiverseClient(token="not-a-real-token")
    monkeypatch.setattr(client, "_request", lambda *a, **k: [])
    return client


# ---------------------------------------------------------------------------
# Generation providers
# ---------------------------------------------------------------------------


def test_a_provider_added_later_is_counted_under_its_own_name():
    provider = _LaterProvider()
    provider.generate(PROMPT)
    provider.generate(PROMPT)
    assert _counts("generation_providers") == {"laterprov": 2}


def test_a_generation_the_provider_refused_is_not_use():
    # A missing key or an unreachable API raises.  Counting that would
    # report a provider as used by everyone who merely tried it.
    with pytest.raises(GenerationError):
        _LaterProvider(outcome=GenerationError("no key", code="AUTH_ERROR")).generate(PROMPT)
    assert _counts("generation_providers") == {}


def test_a_job_that_came_back_failed_is_not_counted():
    _LaterProvider(outcome=GenerationStatus.FAILED).generate(PROMPT)
    assert _counts("generation_providers") == {}


def test_a_provider_built_on_another_is_one_use_under_its_own_name():
    # Without the outermost-call rule this is two rows for one model:
    # the provider the user chose, and the one it happens to reuse.
    _BuiltOnLater().generate(PROMPT)
    assert _counts("generation_providers") == {"builtonlater": 1}


def test_every_shipped_provider_goes_through_the_counted_door():
    # Read from the class tree, not a list: a provider module added later
    # is held to this without anyone remembering to name it here.
    providers = GenerationProvider.__subclasses__()
    shipped = [p for p in providers if p.__module__.startswith("kiln.generation.")]
    assert len(shipped) >= 5, [p.__name__ for p in shipped]
    for cls in shipped:
        assert getattr(cls.generate, "_kiln_service_counted", False), (
            f"{cls.__name__}.generate reaches its provider without being counted"
        )


# ---------------------------------------------------------------------------
# Marketplaces
# ---------------------------------------------------------------------------


def test_a_marketplace_added_later_is_counted_for_search_and_download(tmp_path):
    adapter = _LaterMarketplace("printables")
    adapter.search(QUERY)
    adapter.download_file("file-1", str(tmp_path))
    adapter.download_file("file-2", str(tmp_path))
    assert _counts("marketplace_searches") == {"printables": 1}
    assert _counts("marketplace_sources") == {"printables": 2}


def test_a_fan_out_search_counts_only_the_marketplaces_that_answered():
    # search_all asks every connected marketplace at once.  One that was
    # down did not serve the user and must not read as used.
    registry = MarketplaceRegistry()
    registry.register(_LaterMarketplace("printables"))
    registry.register(_LaterMarketplace("makerworld"))
    registry.register(_LaterMarketplace("brokenplace", down=True))
    results = registry.search_all(QUERY)
    assert sorted(results.searched) == ["makerworld", "printables"]
    assert _counts("marketplace_searches") == {"printables": 1, "makerworld": 1}


def test_a_marketplace_that_cannot_download_records_no_download(tmp_path):
    with pytest.raises(MarketplaceError):
        _MetadataOnly("cults3d").download_file("file-1", str(tmp_path))
    assert _counts("marketplace_sources") == {}


def test_every_shipped_marketplace_goes_through_the_counted_door():
    adapters = MarketplaceAdapter.__subclasses__()
    shipped = [a for a in adapters if a.__module__.startswith("kiln.marketplaces.")]
    assert len(shipped) >= 4, [a.__name__ for a in shipped]
    for cls in shipped:
        assert getattr(cls.search, "_kiln_service_counted", False), (
            f"{cls.__name__}.search reaches its marketplace without being counted"
        )
        if "download_file" in cls.__dict__:
            assert getattr(cls.download_file, "_kiln_service_counted", False), (
                f"{cls.__name__}.download_file is not counted"
            )


# ---------------------------------------------------------------------------
# The older Thingiverse client, which several tools call directly
# ---------------------------------------------------------------------------


def test_the_thingiverse_client_counts_calls_made_straight_to_it(monkeypatch):
    # search_models, browse_models and the default download path never
    # touch the adapter, so the adapter's count cannot see them.
    client = _thingiverse_client(monkeypatch)
    client.search(QUERY)
    client.popular()
    client.category_things("household")
    assert _counts("marketplace_searches") == {"thingiverse": 3}


def test_a_search_through_the_adapter_is_one_use_not_two(monkeypatch):
    # The adapter delegates to that same client; both are counted doors.
    adapter = ThingiverseAdapter(_thingiverse_client(monkeypatch))
    adapter.search(QUERY)
    assert _counts("marketplace_searches") == {"thingiverse": 1}


def test_the_client_and_its_adapter_agree_on_the_name(monkeypatch):
    # Two spellings would split one marketplace across two dashboard rows.
    adapter = ThingiverseAdapter(_thingiverse_client(monkeypatch))
    assert adapter.name == MARKETPLACE_NAME


# ---------------------------------------------------------------------------
# Names only
# ---------------------------------------------------------------------------


def test_nothing_but_the_service_name_is_written(tmp_path):
    _LaterProvider().generate(PROMPT, style="secret-style", image_url="https://example.com/me.png")
    adapter = _LaterMarketplace("printables")
    adapter.search(QUERY)
    adapter.download_file("file-771", str(tmp_path), file_name="my-private-part.stl")

    written = daily_stats._STATS_PATH.read_text(encoding="utf-8")
    for private in (PROMPT, "tripo-v9-secret", QUERY, "secret-style", "example.com",
                    "file-771", "my-private-part", str(tmp_path)):
        assert private not in written, private
    assert _counts("generation_providers") == {"laterprov": 1}


@pytest.mark.parametrize(
    "not_a_name",
    ["https://api.example.com/v2", "two words", "tripo/v3", "model:large", "", "x", 5, None, "a" * 41],
)
def test_a_value_that_is_not_a_service_name_is_dropped(not_a_name):
    # The key's shape is the boundary: whatever a caller passes, a URL, a
    # model id or free text cannot become a key that leaves the machine.
    daily_stats.record_generation_provider(not_a_name)
    daily_stats.record_marketplace_use(not_a_name, "search")
    daily_stats.record_marketplace_use(not_a_name, "download")
    assert _counts("generation_providers") == {}
    assert _counts("marketplace_searches") == {}
    assert _counts("marketplace_sources") == {}


def test_one_service_spelled_two_ways_is_one_row():
    daily_stats.record_generation_provider("meshy")
    daily_stats.record_generation_provider(" Meshy ")
    assert _counts("generation_providers") == {"meshy": 2}


def test_a_runaway_writer_cannot_grow_the_day_file_without_bound():
    for i in range(daily_stats._SERVICE_NAMES_MAX_DISTINCT + 10):
        daily_stats.record_generation_provider(f"provider-{i}")
    daily_stats.record_generation_provider("provider-0")  # known names keep counting
    counted = _counts("generation_providers")
    assert len(counted) == daily_stats._SERVICE_NAMES_MAX_DISTINCT
    assert counted["provider-0"] == 2


def test_a_marketplace_call_of_an_unknown_kind_is_dropped():
    daily_stats.record_marketplace_use("printables", "details")
    assert _counts("marketplace_searches") == {}
    assert _counts("marketplace_sources") == {}


def test_the_download_counter_cannot_name_a_source_a_second_time():
    # The fetch itself names the marketplace.  If the tool-level counter
    # could still file a source too, one download would land twice in the
    # same map: once from the tool, once from the adapter under it.
    daily_stats.record_event("downloads", detail="thingiverse")
    assert daily_stats.get_daily_stats()["downloads"] == 1
    assert _counts("marketplace_sources") == {}
