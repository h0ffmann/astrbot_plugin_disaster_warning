# Extension system: MVP design (contract)

Status: this pull request contains the MVP (2026-10-28). The developer guide is in `README.md`.
The language is Python only. The principle is "the smallest change that works" (YAGNI).
Items that this document does not list are not in the MVP.

The maintainer gave these goals:

- The system is modular and pluggable.
- The system isolates faults. A broken extension does not stop the host.
- Extensions are declarative and the host can find them.
- The system is backward-compatible. If no extension is enabled, the behavior does not change.
- The system supports translations (i18n) from the first version.

## 1. Layout and discovery

An extension is a Python package. The host looks for extensions in two root directories:

1. `<plugin_dir>/extensions/<id>/`: bundled extensions. This repository has none.
2. `<data_dir>/extensions/<id>/`: extensions that the user installs.
   `data_dir` is `StarTools.get_data_dir("astrbot_plugin_disaster_warning")`.

If two roots contain the same `id`, the host uses the first root. The host records the
second one as an error. If the host cannot read a root directory, it logs a warning and
continues with the other root.

The host scans both root directories. An extension on disk that is not in `enabled_ids` has the
state `disabled` in `status()`. The host reads its manifest, but does not import its code.

```
extensions/<id>/
  extension.yaml
  __init__.py
  extension.py        # default entry module; it contains setup(api)
  locale/<lang>.json  # for example zh-CN.json, en.json, pt-BR.json and ja-JP.json
  meta.json           # optional; maintainer, coverage and credits. The host does not read it.
  extension.png       # optional; the icon of the extension
```

`extension.yaml`:

```yaml
id: my_ext                 # required; [a-z0-9_]+; it must be the same as the directory name
name: My extension
version: 0.1.0
api_version: 1             # required; the host supports only 1
min_host_version: 1.6.5    # optional; the host compares it with the version in metadata.yaml
entry: extension:setup     # optional; the default is "extension:setup"
description: ...
author: ...
permissions: [network]     # register_poller needs "network"
dependencies: [other_ext]  # optional; extension ids that must load first
```

`dependencies` is an optional list of extension ids (`[a-z0-9_]+`). An extension cannot list
its own `id`. The host loads each dependency before the extension that names it, whatever the
order of `enabled_ids`. If a dependency is not enabled, fails to load, or forms a cycle, the
dependent extension gets the `error` state and does not load. The host records the reason in
`status()`.

## 2. Configuration

`_conf_schema.json` has a new top-level key. It is off by default. Thus, existing users see no change.

```json
"extensions": {
  "enabled_ids": [],        // the ids of the extensions to load
  "settings": "{}"          // JSON text: {"<id>": {...private settings...}}
}
```

If `extensions` is not a mapping, the host logs a warning and uses `{}`. A string in
`enabled_ids` is treated as one extension `id`, not one character per `id`. Any other
non-list value in `enabled_ids` is ignored with a warning. `settings` can be JSON text or a
dict; if the JSON text is not valid or does not decode to a dict, the host uses `{}`.

## 3. Host registry functions

Each registration function has the argument `owner=<extension id>`. Thus, the host can remove all
items of an extension if its `setup` fails. The built-in behavior does not change.

| Module | Functions |
|---|---|
| `core/sources/source_entry.py` | New enum member `ProviderFamily.EXTENSION = "extension"` |
| `core/sources/source_catalog.py` | `register_source_entry(entry, *, owner) -> None`. It raises ValueError for a duplicate `source_id` and rebuilds all `SOURCE_IDS_BY_*` indexes. `unregister_source_entries(owner) -> list[str]`. `get_source_owner(source_id) -> str \| None`, which gives None for a built-in source. |
| `core/parsers/parser_registry.py` | `register_parser(parser_name, parser_class, *, owner)`, `unregister_parsers(owner)` and `missing_catalog_parser_names() -> dict[str, str]`. |
| `core/message/presenters/presenter_registry.py` | `register_text_presenter(key, presenter_class, *, owner)` and `unregister_presenters(owner)`. The integrity check includes the registered keys. |
| `core/rules/rule_chain.py` | `register_rule(rule, *, after="local_rule", owner)` puts the rule after the built-in rule with that `rule_name`. If no rule has that name, it puts the rule at the end. `unregister_rules(owner)`. `build_default_rule_chain()` includes the registered rules. |

An extension source uses `config_group="extensions"` and `config_key=<source_id>`. A source with
`config_group == "extensions"` is **enabled globally while it is registered**. All checks of
the `data_sources` switches use this rule (`SourceEnabledRule`, `SourceRuntimeQueryService`,
`_is_source_enabled_by_catalog`). The MVP does not support session overrides for extension sources.

Extension rules run **in their registration order** after their anchor. When two extensions
register rules after the same built-in rule, the first registration runs first. An extension
rule is wrapped in `_IsolatedRule`. If `evaluate()` raises or returns a value that is not a
`RuleDecision`, the host logs a warning and accepts the event. One broken rule cannot block
the events of the other sources.

The load boundary of an extension validates its parser references. If a source names an
unregistered `parser_name`, only that extension enters the `error` state and rolls back. The
host still starts because `validate_catalog_parser_names()` finds no missing parser.

### Weather severity

`WeatherEvent.metadata["severity_level"]` is an optional `int` (not a `bool`). Values 0 to 4 mean
white, blue, yellow, orange and red. If `WeatherEvent.metadata` is a `dict` and
`severity_level` is in `0..4`, `WeatherRule` and `WeatherAggregationService` use it. If the
value is not set or not valid, the host finds the color word in the Chinese title, as before.

## 4. ExtensionAPI (the argument of `setup(api)`)

```python
class ExtensionAPI:
    MIN_INTERVAL_SECONDS = 60

    id: str
    version: str
    config: dict            # the private settings of the extension (extensions.settings[id])
    logger                  # a logger that adds the prefix "[扩展:<id>]"
    data_dir: pathlib.Path  # <data_dir>/extensions_data/<id>/; the host makes it when necessary
    host_version: str
    sources: list[str]
    pollers: list[PollerSpec]
    models: types.SimpleNamespace  # host classes; extensions do not import host modules
    # SourceEntry, SourceType, ProviderFamily, FusionRole, WeatherEvent, EventEnvelope,
    # EventIdentity, SourcePayload, RuleDecision

    def t(self, key: str, **fmt) -> str: ...
    def register_source(self, entry: SourceEntry) -> None: ...
    def register_parser(self, name: str, parser_class) -> None: ...
    def register_presenter(self, key: str, presenter_class) -> None: ...
    def register_rule(self, rule, *, after: str = "local_rule") -> None: ...
    def register_poller(
        self,
        source_id: str,
        fetch,                   # async def fetch(session: aiohttp.ClientSession) -> list[EventEnvelope]
        *,
        interval_seconds: float, # clamped to at least MIN_INTERVAL_SECONDS (60)
        timeout_seconds: float = 30,
    ) -> None: ...
```

- Extension code does not import host modules, because the host package name is different
  in different installations. Use `api.models`.
- A presenter is any class with the classmethod `present(display_context, options) -> str`.
  For weather events, `display_context` has `.title`, `.headline`, `.metadata` and `.source_id`.
  For extension sources, `.metadata` merges `WeatherEvent.metadata` over the envelope metadata,
  while keeping the host fields (`event_id`, `source_id`, `event_type`, `description`).
- A rule is any object with `rule_name: str` and `evaluate(context) -> RuleDecision`.
- `register_source` raises ValueError if `entry.config_group` is not `"extensions"`.
  It also raises ValueError if `entry.provider_family` is not `ProviderFamily.EXTENSION`.
- `register_poller` needs `"network"` in `permissions`. The same extension must register the `source_id` first.
- `t()` looks for the key in this sequence:
  1. `locale/<lang>.json`. `lang` is `config.get("language")`. If it is not set, `lang` is the host language `zh-CN`.
  2. `locale/zh-CN.json` (the host language).
  3. The key itself.

  `t()` applies `.format(**fmt)` to the result. If the format operation fails, `t()` gives the text without a change.

## 5. Runtime behavior (ExtensionManager)

`core/extensions/manager.py` contains `ExtensionManager(service, host_registry)`.

- `DisasterWarningService.__init__` calls `load_all()` one time, **before** `_initialize_parsers()`.
  Thus, the host builds the extension parsers in the same way as the built-in parsers.
  Before loading an extension, `load_all()` checks each `id` in `enabled_ids` against
  `[a-z0-9_]+`. The resolved extension directory must be a direct child of a configured root.
  Thus, a path traversal or symlink cannot load code outside the roots.
  For each `id` in `enabled_ids`, `load_all()` runs these steps:
  1. Find and read the manifest after loading any `dependencies`.
  2. Check `api_version` and `min_host_version`.
  3. Clear any prior `_dw_ext_<id>` modules and registrations, import the entry module, and call `setup(api)`.
  4. Validate the parser references of the sources that the extension registered.

  If a step raises an exception, the manager runs these steps:
  - Set the state of the extension to `error` and keep the message.
  - Purge the imported `_dw_ext_<id>` modules and call each `unregister_*(owner=id)` function.
  - Write a warning to the log and continue with the next extension.
- The lifecycle start calls `start()`. The manager serializes `start()` and `stop()` with one
  manager-level `asyncio.Lock`, because the host locks (`_start_lock`, `_stop_lock`) do not
  exclude each other. A `start()` that runs while a `stop()` is in flight waits for `stop()` to
  finish. After a completed `stop()` on the same manager, `start()` calls `load_all()` again and
  starts one asyncio task for each poller:
  - All pollers share one `aiohttp.ClientSession` with the `certifi` root certificates.
  - The manager calls `fetch` with `asyncio.wait_for(timeout_seconds)`.
  - Each polling task records the identity (`unique_key` plus `published_at`, or `unique_key`
    when `published_at` is empty) when `fetch()` returns an envelope, before delivery. Each
    polling task keeps only the **1024 most recently recorded identities** (`MAX_SEEN_EVENTS`).
    Because registration happens at fetch time, envelopes whose delivery fails or is rejected
    also take a slot in those 1024 identities. While an identity stays in that set, the poller
    skips a fetched envelope with the same identity. An update passes when the extension gives
    the same `event_id` a new `published_at`. When more than 1024 newer identities are recorded,
    the oldest identity is evicted, and returning it again delivers it again.
  - New envelopes join the `pending` list before delivery. The manager sends each envelope in
    `pending` to `await service._handle_disaster_event(envelope)` with a delivery timeout of one
    minute (`DELIVERY_TIMEOUT_SECONDS = 60`). Each call has its own `try`/`except`, so a stalled
    handler returns the envelope to `pending` instead of blocking the poller. An envelope leaves
    `pending` in place as soon as the host handles it (`handled is not False`). If `stop()`
    cancels the task between two envelopes, every unfinished envelope remains in `pending`.
  - An envelope whose `source_id` does not match the `source_id` of the polling task is removed
    from `pending` and rejected. An extension cannot speak for another source, built-in or
    extension.
  - An envelope whose delivery returns `False`, times out, or raises stays in `pending` for the
    next poll. Retries from `pending` do not check `_seen` again. After the delivery step of a
    poll cycle finishes, `pending` keeps at most the newest 100 envelopes
    (`MAX_PENDING_ENVELOPES`) and drops older ones. If `stop()` cancels the task during
    delivery, `pending` can hold more than 100 envelopes until the next poll finishes its
    delivery step. The drop warning shares the per-poller log throttle
    (`LOG_THROTTLE_SECONDS = 600`) with fetch and delivery warnings. Thus, the dropped count is
    logged only when the throttle allows a warning in that period.
  - If `fetch` raises an exception, the manager logs a throttled warning (at most one per
    `LOG_THROTTLE_SECONDS` period). It then waits with exponential backoff (`2**failures` times
    the interval, up to 10 times the interval). A poller task does not stop on an exception.
  - `pending` and the recorded identities (`_seen`) live in the memory of the manager. They
    survive `stop()` and `start()` on the same manager instance. They also survive a failed
    reload when the extension stays in `enabled_ids` (`_drop_stale_poll_state` drops only
    extensions removed from `enabled_ids`). A plugin reload or a new process creates a new
    manager with empty memory. An extension that wants an event delivered after a reload must
    return it again.
- `stop()` cancels the poller tasks, closes the HTTP session, and unregisters the extension
  sources, parsers, presenters and rules. During shutdown, `DisasterServiceLifecycle` calls
  `weather_agg.flush_all()` **before** `extension_manager.stop()`, so buffered extension weather
  events still resolve their extension sources and presenters.
- `status() -> list[dict]` gives `[{id, name, version, state, error, sources}]`.
  `state` is `"loaded"`, `"error"`, `"not_found"` or `"disabled"`. The admin command `/灾害预警扩展` shows this list.

`host_registry` is a small adapter with the functions of section 3 (`HostRegistry` in
`core/extensions/host_registry.py`, plus `missing_source_parser_names(owner)`). Tests can pass a
fake registry.

## 6. Items that are not in the MVP

- Process sandboxes.
- A marketplace and signatures.
- Installation from zip files or git.
- WebAdmin pages.
- WebSocket handlers from extensions.
- New disaster types.
- Session overrides for extension sources.
- Migration of the built-in sources.
