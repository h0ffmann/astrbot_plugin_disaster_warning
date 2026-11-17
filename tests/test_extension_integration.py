"""Integration test. Load small extensions with the real host registries.

The extensions are written in a temporary directory. Thus, the test checks that the
host works for any third-party extension, and the repository has no business extension.
"""

from __future__ import annotations

import importlib
import json
import types
from pathlib import Path

import pytest

PLUGIN_DIR = Path(__file__).resolve().parent.parent
FIRST, SECOND = "demo_first", "demo_second"

MANIFEST = """\
id: {ext_id}
name: Demo
version: 0.1.0
api_version: 1
min_host_version: 1.6.5
entry: extension:setup
description: This extension is a test extension.
author: test
permissions: [network]
"""

SETUP = """\
from datetime import datetime, timezone

ID = "{ext_id}"
SOURCE = f"{{ID}}_source"
PRESENTER = f"{{ID}}_presenter"


class Presenter:
    @classmethod
    def present(cls, context, _options=None):
        return f"{{ID}}: {{context.title}}"


def entry(api):
    m = api.models
    return m.SourceEntry(
        source_id=SOURCE, source_enum=SOURCE, source_type=m.SourceType.WEATHER,
        provider_family=m.ProviderFamily.EXTENSION, config_group="extensions",
        config_key=SOURCE, parser_name="", presentation_type="weather",
        text_presenter_key=PRESENTER, report_policy="none", intensity_mode="none",
        priority=1, display_name="Demo", description="Demo", default_timezone="UTC",
        fingerprint_prefix=SOURCE, dispatch_family=SOURCE,
        provider_aliases=(SOURCE,), routing_tags=("extension",),
    )


def setup(api):
    async def fetch(_session):
        m = api.models
        now = datetime.now(timezone.utc)
        identity = m.EventIdentity(
            event_id=f"{{ID}}_1", source_id=SOURCE, event_type="weather_alarm",
            provider_family="extension", source_enum=SOURCE, published_at=now,
        )
        event = m.WeatherEvent(
            title="Alert", headline="Headline", effective_at=now,
            metadata={{"severity_level": 3}},
        )
        payload = m.SourcePayload(source_id=SOURCE, provider_family="extension", raw={{}})
        return [m.EventEnvelope(identity=identity, event=event, payload=payload)]

    api.register_presenter(PRESENTER, Presenter)
    api.register_source(entry(api))
    api.register_poller(SOURCE, fetch, interval_seconds=300)
"""

BROKEN_SETUP = """\
def setup(api):
    raise RuntimeError("setup failed on purpose")
"""

# A source that references a parser_name that the extension does not register.
SETUP_WITH_GHOST_PARSER = SETUP.replace('parser_name=""', 'parser_name="ghost_parser"')


class _FakeService:
    def __init__(self, config):
        self.config = config


def _write_extension(root: Path, ext_id: str, code: str = SETUP) -> None:
    directory = root / ext_id
    directory.mkdir(parents=True)
    (directory / "extension.yaml").write_text(MANIFEST.format(ext_id=ext_id), "utf-8")
    (directory / "__init__.py").write_text("", "utf-8")
    (directory / "extension.py").write_text(code.format(ext_id=ext_id), "utf-8")


def _make_manager(tmp_path, ids, roots=None):
    importlib.import_module("astrbot_plugin_disaster_warning.core.app.disaster_service")
    from astrbot_plugin_disaster_warning.core.extensions.host_registry import (
        HostRegistry,
    )
    from astrbot_plugin_disaster_warning.core.extensions.manager import (
        ExtensionManager,
    )

    config = {"extensions": {"enabled_ids": ids, "settings": json.dumps({})}}
    return ExtensionManager(
        _FakeService(config), HostRegistry(), roots=roots, data_dir=tmp_path
    )


def _cleanup(*owners: str) -> None:
    from astrbot_plugin_disaster_warning.core.extensions.host_registry import (
        HostRegistry,
    )

    registry = HostRegistry()
    for owner in owners:
        registry.unregister_source_entries(owner=owner)
        registry.unregister_presenters(owner=owner)


@pytest.fixture
def two_loaded(tmp_path):
    """Load two extensions. Unregister them after the test."""
    root = tmp_path / "root"
    _write_extension(root, FIRST)
    _write_extension(root, SECOND)
    manager = _make_manager(tmp_path, [FIRST, SECOND], [root])
    manager.load_all()
    yield manager
    _cleanup(FIRST, SECOND)


async def _first_event(manager, ext_id):
    (poller,) = manager._apis[ext_id].pollers
    (envelope,) = await poller.fetch(None)
    return envelope


def test_repository_has_no_business_extension():
    """Check the rule of the main repository: capability, not content."""
    assert list(PLUGIN_DIR.glob("extensions/*/extension.yaml")) == []


def test_extension_in_the_data_directory_loads(tmp_path):
    """Check the path of an extension that a user installs. Use the default roots."""
    _write_extension(tmp_path / "extensions", FIRST)
    manager = _make_manager(tmp_path, [FIRST])
    try:
        manager.load_all()
        (record,) = manager.status()
        assert record["state"] == "loaded", record["error"]
    finally:
        _cleanup(FIRST)


def test_enabled_extension_that_is_not_installed_is_reported(tmp_path):
    """Check that a missing extension does not stop the load."""
    manager = _make_manager(tmp_path, ["not_installed"])
    manager.load_all()
    (record,) = manager.status()
    assert record["state"] == "not_found"


def test_both_loaded_without_collision(two_loaded):
    """Check that both extensions load and that their sources are distinct in the catalog."""
    from astrbot_plugin_disaster_warning.core.sources.source_catalog import (
        SOURCE_CATALOG,
        get_source_owner,
    )

    records = {r["id"]: r for r in two_loaded.status()}
    assert set(records) == {FIRST, SECOND}
    for ext_id, record in records.items():
        assert record["state"] == "loaded", record["error"]
        assert record["sources"] == [f"{ext_id}_source"]
        assert record["sources"][0] in SOURCE_CATALOG
        assert get_source_owner(record["sources"][0]) == ext_id


async def test_event_passes_rule_chain_and_renders(two_loaded):
    """Check that an event passes the rule chain and uses the presenter of the extension."""
    from astrbot_plugin_disaster_warning.core.message.presenters.presenter_registry import (
        present_message,
    )
    from astrbot_plugin_disaster_warning.core.message.push.push_policy import (
        evaluate_push_decision_with_components,
    )

    envelope = await _first_event(two_loaded, FIRST)
    runtime_config = {"weather_config": {"weather_filter": {"enabled": True}}}
    decision = evaluate_push_decision_with_components(
        envelope, runtime_config=runtime_config, policy_state={}
    )
    assert decision.accepted, decision.reason
    assert present_message(envelope, envelope.source_id) == f"{FIRST}: Alert"


async def test_each_event_uses_its_own_presenter(two_loaded):
    """Check that each event is presented by the presenter of its own extension."""
    from astrbot_plugin_disaster_warning.core.message.presenters.presenter_registry import (
        present_message,
    )

    first = await _first_event(two_loaded, FIRST)
    second = await _first_event(two_loaded, SECOND)
    assert present_message(first, first.source_id).startswith(FIRST)
    assert present_message(second, second.source_id).startswith(SECOND)


def test_failing_extension_does_not_affect_the_others(tmp_path):
    """Check that an extension that raises in setup is removed and the others stay."""
    from astrbot_plugin_disaster_warning.core.sources.source_catalog import (
        SOURCE_CATALOG,
    )

    root = tmp_path / "root"
    _write_extension(root, FIRST)
    _write_extension(root, "broken", BROKEN_SETUP)
    _write_extension(root, SECOND)
    manager = _make_manager(tmp_path, [FIRST, "broken", SECOND], [root])
    try:
        manager.load_all()
        records = {r["id"]: r for r in manager.status()}
        assert records[FIRST]["state"] == "loaded"
        assert records[SECOND]["state"] == "loaded"
        assert records["broken"]["state"] == "error"
        assert "setup failed on purpose" in records["broken"]["error"]
        assert f"{FIRST}_source" in SOURCE_CATALOG
        assert f"{SECOND}_source" in SOURCE_CATALOG
    finally:
        _cleanup(FIRST, SECOND, "broken")


def test_extension_with_unregistered_parser_is_isolated(tmp_path):
    """A source that references an unregistered parser_name isolates the extension.

    The host itself must still start: validate_catalog_parser_names() passes
    after the rollback removed the source of the extension.
    """
    importlib.import_module("astrbot_plugin_disaster_warning.core.app.disaster_service")
    from astrbot_plugin_disaster_warning.core.parsers.parser_registry import (
        validate_catalog_parser_names,
    )

    root = tmp_path / "root"
    _write_extension(root, FIRST)
    _write_extension(root, "bad_parser", SETUP_WITH_GHOST_PARSER)
    manager = _make_manager(tmp_path, [FIRST, "bad_parser"], [root])
    try:
        manager.load_all()
        records = {r["id"]: r for r in manager.status()}
        assert records[FIRST]["state"] == "loaded", records[FIRST]["error"]
        assert records["bad_parser"]["state"] == "error"
        assert "解析器" in records["bad_parser"]["error"]
        # The rollback removed the source: the host starts with no missing parser.
        validate_catalog_parser_names()
    finally:
        _cleanup(FIRST, "bad_parser")


async def test_a_broken_extension_rule_does_not_block_events(two_loaded):
    """A rule of an extension that raises must not stop the push decision of the host."""
    importlib.import_module("astrbot_plugin_disaster_warning.core.app.disaster_service")
    from astrbot_plugin_disaster_warning.core.message.push.push_policy import (
        evaluate_push_decision_with_components,
    )

    rule_chain = importlib.import_module(
        "astrbot_plugin_disaster_warning.core.rules.rule_chain"
    )

    class BrokenRule:
        rule_name = "test_broken_ext_rule"

        def evaluate(self, context):
            raise RuntimeError("rule boom")

    rule_chain.register_rule(BrokenRule(), owner=FIRST)
    try:
        envelope = await _first_event(two_loaded, SECOND)
        runtime_config = {"weather_config": {"weather_filter": {"enabled": True}}}
        decision = evaluate_push_decision_with_components(
            envelope, runtime_config=runtime_config, policy_state={}
        )
        # The broken rule is isolated: the chain accepts, as it does without it.
        assert decision.accepted, decision.reason
    finally:
        rule_chain.unregister_rules(FIRST)


async def test_a_rule_that_returns_nothing_is_isolated(two_loaded):
    """A rule of an extension that returns None must not stop the chain.

    RuleChain.evaluate reads decision.accepted: a rule that forgets the
    return value would raise an AttributeError outside the isolation
    boundary of the extension rule. The wrapper catches it too.
    """
    importlib.import_module("astrbot_plugin_disaster_warning.core.app.disaster_service")
    from astrbot_plugin_disaster_warning.core.message.push.push_policy import (
        evaluate_push_decision_with_components,
    )

    rule_chain = importlib.import_module(
        "astrbot_plugin_disaster_warning.core.rules.rule_chain"
    )

    class NoneRule:
        rule_name = "test_none_ext_rule"

        def evaluate(self, context):
            return None  # third-party code can forget the return value

    rule_chain.register_rule(NoneRule(), owner=FIRST)
    try:
        envelope = await _first_event(two_loaded, SECOND)
        runtime_config = {"weather_config": {"weather_filter": {"enabled": True}}}
        decision = evaluate_push_decision_with_components(
            envelope, runtime_config=runtime_config, policy_state={}
        )
        # The rule without a decision is isolated: the chain accepts.
        assert decision.accepted, decision.reason
    finally:
        rule_chain.unregister_rules(FIRST)


async def test_stop_flushes_the_weather_buffer_before_the_extensions():
    """Check the shutdown order: drain the aggregation buffer before unregistering.

    A buffered extension event needs its extension source and presenter when
    the flush callback re-evaluates the rule chain (SourceEnabledRule rejects
    an unregistered source). The flush must run before extension stop().
    """
    import asyncio
    import types

    importlib.import_module("astrbot_plugin_disaster_warning.core.app.disaster_service")
    lifecycle_mod = importlib.import_module(
        "astrbot_plugin_disaster_warning.core.app.runtime.disaster_service_lifecycle"
    )

    order = []

    class FakePart:
        def __init__(self, name):
            self._name = name

        async def stop(self):
            order.append(self._name)

    class FakeAggregation:
        async def flush_all(self):
            order.append("flush_all")

    service = types.SimpleNamespace(
        _stop_lock=asyncio.Lock(),
        _stopping=False,
        running=False,
        stop_started_at=None,
        connection_tasks=[],
        scheduled_tasks=[],
        background_tasks=[],
        ws_manager=FakePart("ws_manager"),
        http_fetcher=None,
        statistics_manager=None,
        _telemetry=None,
        notification_center=None,
        extension_manager=FakePart("extension_manager"),
        _weather_aggregation_service=FakeAggregation(),
    )
    lifecycle = lifecycle_mod.DisasterServiceLifecycleService(service)
    await lifecycle.stop()
    # The buffered weather events leave the host before the extension
    # registrations are removed and before the connections go down.
    assert order.index("flush_all") < order.index("extension_manager")
    assert order.index("flush_all") < order.index("ws_manager")
    assert order.index("extension_manager") < order.index("ws_manager")


async def test_unregister_one_leaves_the_other(two_loaded):
    """Check that the removal of one extension keeps the source and presenter of the other."""
    from astrbot_plugin_disaster_warning.core.message.presenters.presenter_registry import (
        present_message,
    )
    from astrbot_plugin_disaster_warning.core.sources.source_catalog import (
        SOURCE_CATALOG,
        get_source_owner,
    )

    second = await _first_event(two_loaded, SECOND)
    _cleanup(FIRST)
    assert f"{FIRST}_source" not in SOURCE_CATALOG
    assert get_source_owner(f"{SECOND}_source") == SECOND
    assert present_message(second, second.source_id).startswith(SECOND)


def _context(metadata, provider_family="extension"):
    importlib.import_module("astrbot_plugin_disaster_warning.core.app.disaster_service")
    from astrbot_plugin_disaster_warning.core.services.display.builders import (
        weather_context_builder as builder,
    )

    event = types.SimpleNamespace(
        metadata=metadata, headline="Headline", description=""
    )
    envelope = types.SimpleNamespace(
        id="host_id", event_type="weather_alarm", event=event
    )
    return builder.build_weather_display_context(
        {
            "envelope": envelope,
            "resolved_source_id": "host_source",
            "source_descriptor": types.SimpleNamespace(provider_family=provider_family),
            "source_payload": {},
            "metadata": {"status": "from_display", "other": 1},
            "title": "Title",
        }
    ).metadata


def test_extension_fields_win_over_display_fields():
    """Check the merge direction of the event metadata of an extension."""
    metadata = _context({"status": "from_extension"})
    assert metadata["status"] == "from_extension"
    assert metadata["other"] == 1


def test_extension_cannot_replace_the_host_fields():
    """Check that the identity of an event stays with the host."""
    metadata = _context({"event_id": "fake", "source_id": "fake"})
    assert (metadata["event_id"], metadata["source_id"]) == ("host_id", "host_source")


def test_built_in_source_ignores_event_metadata():
    """Check that the display metadata of a built-in source does not change."""
    metadata = _context({"status": "from_extension"}, provider_family="fan_studio")
    assert metadata["status"] == "from_display"


def test_extension_merge_with_the_real_enum_provider_family():
    """Check the merge when the descriptor carries the ProviderFamily enum member.

    A SourceEntry (not the display SourceDescriptor) has the enum member in
    provider_family. The merge must work for both the member and the value.
    """
    importlib.import_module("astrbot_plugin_disaster_warning.core.app.disaster_service")
    from astrbot_plugin_disaster_warning.core.sources.source_entry import (
        ProviderFamily,
    )

    metadata = _context(
        {"status": "from_extension"}, provider_family=ProviderFamily.EXTENSION
    )
    assert metadata["status"] == "from_extension"
    assert (metadata["event_id"], metadata["source_id"]) == ("host_id", "host_source")


@pytest.mark.parametrize("metadata", [None, "text", 3, [1]])
async def test_rules_accept_event_metadata_that_is_not_a_dict(two_loaded, metadata):
    """Check the weather rule and the aggregation with a bad metadata value."""
    from astrbot_plugin_disaster_warning.core.message.push.push_policy import (
        evaluate_push_decision_with_components,
    )
    from astrbot_plugin_disaster_warning.core.message.push.weather_aggregation_service import (
        WeatherAggregationService,
    )

    envelope = await _first_event(two_loaded, FIRST)
    envelope.event.metadata = metadata
    runtime_config = {"weather_config": {"weather_filter": {"enabled": True}}}
    evaluate_push_decision_with_components(
        envelope, runtime_config=runtime_config, policy_state={}
    )
    service = WeatherAggregationService.__new__(WeatherAggregationService)
    assert service._resolve_color_level(envelope)[0] == 0


@pytest.mark.parametrize("level", [True, 5, -1, "3", 2.0])
async def test_bad_severity_level_is_ignored(two_loaded, level):
    """Check that only an int from 0 to 4 sets the color level."""
    from astrbot_plugin_disaster_warning.core.message.push.weather_aggregation_service import (
        WeatherAggregationService,
    )

    envelope = await _first_event(two_loaded, FIRST)
    envelope.event.metadata = {"severity_level": level}
    service = WeatherAggregationService.__new__(WeatherAggregationService)
    assert service._resolve_color_level(envelope)[0] == 0


async def test_severity_level_sets_the_color(two_loaded):
    """Check the normal path of the severity level."""
    from astrbot_plugin_disaster_warning.core.message.push.weather_aggregation_service import (
        WeatherAggregationService,
    )

    envelope = await _first_event(two_loaded, FIRST)
    service = WeatherAggregationService.__new__(WeatherAggregationService)
    assert service._resolve_color_level(envelope) == (3, "橙色")
