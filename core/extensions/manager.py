"""
Extension manager. It finds and loads extensions, isolates them, and runs
their polling tasks.

A failure in one extension sets only that extension to the error state and
removes its registrations. The host and the other extensions do not change.
"""

from __future__ import annotations

import asyncio
import importlib
import importlib.util
import json
import ssl
import sys
import time
from pathlib import Path
from typing import Any

import aiohttp
import yaml

from astrbot.api import logger
from astrbot.api.star import StarTools

from .api import ExtensionAPI, PollerSpec
from .manifest import Manifest, check_host_version, is_valid_extension_id, load_manifest

PLUGIN_DIR = Path(__file__).resolve().parents[2]
PLUGIN_NAME = "astrbot_plugin_disaster_warning"


def _build_ssl_context() -> ssl.SSLContext:
    """Use the certifi root certificates when they are available.

    Some system stores do not have new roots, for example GlobalSign Root R46.
    The result is CERTIFICATE_VERIFY_FAILED.
    """
    try:
        import certifi

        return ssl.create_default_context(cafile=certifi.where())
    except ImportError:
        return ssl.create_default_context()


def _dedupe_key(envelope: Any) -> str | None:
    """Return the stable identity of an envelope, or None if it has none.

    The key is the unique_key of the identity, plus published_at when the
    extension gives one: an unchanged alert snapshot is not sent twice, but an
    update (a new published_at of the same event) is a new key.
    """
    identity = getattr(envelope, "identity", None)
    unique_key = getattr(identity, "unique_key", None)
    if not isinstance(unique_key, str) or not unique_key:
        return None
    published_at = getattr(identity, "published_at", None)
    if published_at is None or published_at == "":
        return unique_key
    return f"{unique_key}@{published_at}"


class ExtensionManager:
    """See section 5 of docs/extensions/design.md."""

    MAX_BACKOFF_FACTOR = 10  # Maximum backoff, as a multiple of the polling interval
    LOG_THROTTLE_SECONDS = 600  # Minimum seconds between error logs of one polling task
    MAX_PENDING_ENVELOPES = 100  # Maximum failed events to keep for each polling task
    MAX_SEEN_EVENTS = 1024  # Identities of delivered events that one polling task keeps
    DELIVERY_TIMEOUT_SECONDS = (
        60  # Maximum seconds for one event in the host event pipeline
    )

    def __init__(
        self,
        service,
        host_registry,
        *,
        roots: list[Path] | None = None,
        host_version: str | None = None,
        data_dir: Path | None = None,
    ):
        self.service = service
        self.host_registry = host_registry
        self._data_dir = data_dir
        self._roots = roots
        self.host_version = host_version or self._read_host_version()
        self._records: list[dict[str, Any]] = []  # Data for status()
        self._apis: dict[str, ExtensionAPI] = {}  # Extensions that loaded
        self._tasks: list[asyncio.Task] = []
        self._session: aiohttp.ClientSession | None = None
        self._loaded_once = False  # A start() after a stop() loads again
        # Serializes start() and stop(). The service locks of the host
        # (_start_lock, _stop_lock) do not exclude each other, so the manager
        # guards its own session, pollers and registrations with this one.
        self._lifecycle_lock = asyncio.Lock()
        # State of the polling tasks. It survives a stop(): the pollers of a
        # restart continue where they stopped.
        self._pending: dict[str, list] = {}  # label -> failed envelopes
        self._seen: dict[str, dict[str, None]] = {}  # label -> delivered identities

    @staticmethod
    def _read_host_version() -> str:
        try:
            meta = yaml.safe_load((PLUGIN_DIR / "metadata.yaml").read_text("utf-8"))
            return str(meta.get("version") or "0")
        except Exception:
            return "0"

    def _base_data_dir(self) -> Path:
        return self._data_dir or StarTools.get_data_dir(PLUGIN_NAME)

    def _get_roots(self) -> list[Path]:
        if self._roots is not None:
            return list(self._roots)
        return [PLUGIN_DIR / "extensions", self._base_data_dir() / "extensions"]

    def _extensions_config(self) -> dict:
        """Return the extensions configuration. A value that is not a mapping gives {}.

        A malformed setting must not abort the start of the host.
        """
        try:
            cfg = self.service.config.get("extensions")
        except AttributeError:
            cfg = None
        if cfg is None:
            return {}
        if not isinstance(cfg, dict):
            logger.warning(
                f"[灾害预警] extensions 配置必须是键值映射，已忽略: {type(cfg).__name__}"
            )
            return {}
        return cfg

    @staticmethod
    def _enabled_ids(cfg: dict) -> list[str]:
        """Return the enabled extension ids. Invalid values do not stop the load.

        A string is one id, not one character per id. A value that is not a
        list, a tuple, a set or a string is ignored.
        """
        raw = cfg.get("enabled_ids")
        if raw is None:
            return []
        if isinstance(raw, str):
            return [raw]
        if isinstance(raw, (list, tuple, set)):
            return [str(i) for i in raw]
        logger.warning(
            f"[灾害预警] enabled_ids 必须是列表，已忽略: {type(raw).__name__}"
        )
        return []

    def _settings(self, cfg: dict) -> dict:
        """Return the settings dict. The value is JSON text or a dict. Return {} if it is not valid."""
        raw = cfg.get("settings") or {}
        if isinstance(raw, str):
            try:
                raw = json.loads(raw or "{}")
            except ValueError as e:
                logger.warning(f"[灾害预警] 扩展 settings 不是合法 JSON，已忽略: {e}")
                return {}
        return raw if isinstance(raw, dict) else {}

    @staticmethod
    def _is_inside_root(root: Path, ext_id: str) -> bool:
        """Check that the extension directory of ext_id is a child of the root.

        A path traversal or a symlink in the id must not take the load outside
        of the configured extension roots.
        """
        try:
            return (root / ext_id).resolve().parent == root.resolve()
        except OSError:
            return False

    def _record(
        self, ext_id: str, state: str, error: str = "", *, name: str = "", version=""
    ) -> dict:
        rec = {
            "id": ext_id,
            "name": name or ext_id,
            "version": version,
            "state": state,
            "error": error,
            "sources": [],
        }
        self._records.append(rec)
        return rec

    def _unregister_all(self, ext_id: str) -> None:
        """Remove all registrations of an extension. A failed item does not stop the others."""
        reg = self.host_registry
        for name in (
            "unregister_source_entries",
            "unregister_parsers",
            "unregister_presenters",
            "unregister_rules",
        ):
            try:
                getattr(reg, name)(owner=ext_id)
            except Exception as e:
                logger.warning(f"[灾害预警] 扩展 {ext_id} 回收 {name} 失败: {e}")

    def load_all(self) -> None:
        """Load the extensions in enabled_ids one by one. A failure does not stop the others.

        An extension that names dependencies loads after them. An extension
        whose dependency is not enabled, or that sits in a dependency cycle,
        gets the error state and does not load.
        """
        self._loaded_once = True
        self._records.clear()
        self._apis.clear()
        cfg = self._extensions_config()
        enabled = self._enabled_ids(cfg)
        settings = self._settings(cfg)
        found: dict[str, Path] = {}
        for ext_id in dict.fromkeys(enabled):
            if not is_valid_extension_id(ext_id):
                self._record(
                    ext_id, "error", f"非法的扩展 ID（需匹配 [a-z0-9_]+）: {ext_id!r}"
                )
                logger.warning(f"[灾害预警] 扩展 ID 非法，已忽略: {ext_id!r}")
                continue
            roots = self._find_manifest_dirs(ext_id)
            if not roots:
                self._record(ext_id, "not_found", "未在任何扩展根目录中找到")
                logger.warning(f"[灾害预警] 扩展 {ext_id} 未找到")
                continue
            for dup in roots[1:]:
                self._record(ext_id, "error", f"重复的扩展 id，已忽略: {dup}")
                logger.warning(f"[灾害预警] 扩展 {ext_id} 重复，已忽略 {dup}")
            found[ext_id] = roots[0]
        order, manifests = self._plan_load_order(found)
        for ext_id in order:
            manifest = manifests.get(ext_id)
            unmet = [
                dep
                for dep in (manifest.dependencies if manifest is not None else ())
                if dep not in self._apis
            ]
            if unmet:
                self._record(
                    ext_id,
                    "error",
                    f"依赖的扩展未加载: {', '.join(unmet)}",
                    name=manifest.name if manifest is not None else "",
                    version=manifest.version if manifest is not None else "",
                )
                logger.warning(
                    f"[灾害预警] 扩展 {ext_id} 依赖未加载: {', '.join(unmet)}"
                )
                continue
            self._load_one(ext_id, found[ext_id], settings.get(ext_id))
        self._record_disabled(set(enabled))
        self._drop_stale_poll_state(enabled)

    def _plan_load_order(
        self, found: dict[str, Path]
    ) -> tuple[list[str], dict[str, Manifest]]:
        """Order the ids so that each extension loads after its dependencies.

        An extension whose dependency is not in enabled_ids, or that sits in a
        dependency cycle, gets the error state here and does not load. An
        extension whose manifest is invalid loads in its own load step: it
        reports the error of its manifest there.
        """
        manifests: dict[str, Manifest] = {}
        for ext_id, ext_dir in found.items():
            try:
                manifests[ext_id] = load_manifest(ext_dir)
            except Exception:
                continue  # _load_one records the error of this manifest
        order: list[str] = []
        loaded: set[str] = set()
        blocked: set[str] = set()

        def record_blocked(ext_id: str, msg: str) -> None:
            manifest = manifests.get(ext_id)
            self._record(
                ext_id,
                "error",
                msg,
                name=manifest.name if manifest is not None else "",
                version=manifest.version if manifest is not None else "",
            )
            logger.warning(f"[灾害预警] 扩展 {ext_id} 无法加载: {msg}")

        def visit(ext_id: str, path: tuple[str, ...]) -> bool:
            """Add ext_id after its dependencies. Return False when it does not load."""
            if ext_id in loaded:
                return True
            if ext_id in blocked:
                return False
            if ext_id in path:
                cycle = path[path.index(ext_id) :] + (ext_id,)
                for member in cycle:
                    record_blocked(member, f"循环依赖: {' -> '.join(cycle)}")
                    blocked.add(member)
                return False
            manifest = manifests.get(ext_id)
            for dep in manifest.dependencies if manifest is not None else ():
                if dep not in found:
                    record_blocked(ext_id, f"依赖的扩展未启用: {dep}")
                    blocked.add(ext_id)
                    return False
                if not visit(dep, path + (ext_id,)):
                    if ext_id not in blocked:
                        record_blocked(ext_id, f"依赖的扩展无法加载: {dep}")
                        blocked.add(ext_id)
                    return False
            order.append(ext_id)
            loaded.add(ext_id)
            return True

        for ext_id in found:
            visit(ext_id, ())
        return order, manifests

    def _find_manifest_dirs(self, ext_id: str) -> list[Path]:
        """Return the extension directories of ext_id that carry a manifest.

        A root that the host cannot read gives no directory, not an exception:
        the discovery failure of one root is isolated, and the other roots
        still give their extensions.
        """
        found: list[Path] = []
        for root in self._get_roots():
            if not self._is_inside_root(root, ext_id):
                continue
            try:
                if (root / ext_id / "extension.yaml").is_file():
                    found.append(root / ext_id)
            except OSError as e:
                logger.warning(
                    f"[灾害预警] 无法在扩展根目录 {root} 中读取 {ext_id}: {e}"
                )
        return found

    def _record_disabled(self, known: set[str]) -> None:
        """Record each extension on disk that is not in enabled_ids. Do not import it."""
        for root in self._get_roots():
            try:
                if not root.is_dir():
                    continue
                ext_dirs = sorted(root.iterdir())
            except OSError as e:
                logger.warning(f"[灾害预警] 无法列出扩展根目录 {root}: {e}")
                continue
            for ext_dir in ext_dirs:
                ext_id = ext_dir.name
                if ext_id in known:
                    continue
                try:
                    if not (ext_dir / "extension.yaml").is_file():
                        continue
                except OSError as e:
                    logger.warning(f"[灾害预警] 无法检查扩展 {ext_dir}: {e}")
                    continue
                known.add(ext_id)  # The first root wins, as it does for a load.
                try:
                    manifest = load_manifest(ext_dir)
                except Exception as e:
                    self._record(ext_id, "disabled", f"{type(e).__name__}: {e}")
                    continue
                self._record(
                    ext_id, "disabled", name=manifest.name, version=manifest.version
                )

    def _drop_stale_poll_state(self, enabled: list[str]) -> None:
        """Drop the poll state of extensions that are no longer enabled.

        An extension that stays enabled keeps its state even when its load
        fails: its pollers resume with the pending envelopes when it loads
        again in a later restart.
        """
        enabled_ids = set(enabled)
        for label in [
            key for key in self._pending if key.split("/", 1)[0] not in enabled_ids
        ]:
            del self._pending[label]
        for label in [
            key for key in self._seen if key.split("/", 1)[0] not in enabled_ids
        ]:
            del self._seen[label]

    @staticmethod
    def _purge_modules(pkg: str) -> None:
        for name in [n for n in sys.modules if n == pkg or n.startswith(pkg + ".")]:
            del sys.modules[name]

    def _load_one(self, ext_id: str, ext_dir: Path, ext_settings: Any) -> None:
        rec = None
        pkg = f"_dw_ext_{ext_id}"
        # Remove the modules of an earlier load. A plugin reload then uses the new code.
        self._purge_modules(pkg)
        # Remove the registrations of an earlier load. A new load then does not fail.
        self._unregister_all(ext_id)
        try:
            manifest = load_manifest(ext_dir)
            rec = self._record(
                ext_id, "error", name=manifest.name, version=manifest.version
            )
            check_host_version(manifest, self.host_version)
            api = ExtensionAPI(
                manifest,
                self.host_registry,
                config=ext_settings if isinstance(ext_settings, dict) else {},
                host_version=self.host_version,
                data_dir=self._base_data_dir() / "extensions_data" / ext_id,
            )
            # Import with a private package name. The extension can use relative imports
            # and the global namespace stays clean.
            spec = importlib.util.spec_from_file_location(
                pkg,
                ext_dir / "__init__.py",
                submodule_search_locations=[str(ext_dir)],
            )
            if spec is None or spec.loader is None:
                raise ImportError("扩展缺少 __init__.py")
            module = importlib.util.module_from_spec(spec)
            sys.modules[pkg] = module
            spec.loader.exec_module(module)
            mod_name, _, func_name = manifest.entry.partition(":")
            entry_mod = importlib.import_module(f"{pkg}.{mod_name}")
            getattr(entry_mod, func_name)(api)
            # A source that references a parser_name without a registered parser
            # would stop the host at validate_catalog_parser_names(). Isolate it here.
            missing = self.host_registry.missing_source_parser_names(ext_id)
            if missing:
                raise ValueError(
                    "源引用了未注册的解析器: "
                    + ", ".join(
                        f"{sid}={name}" for sid, name in sorted(missing.items())
                    )
                )
        except Exception as e:
            self._purge_modules(pkg)
            self._unregister_all(ext_id)
            msg = f"{type(e).__name__}: {e}"
            if rec is None:
                rec = self._record(ext_id, "error")
            rec.update(state="error", error=msg)
            logger.warning(f"[灾害预警] 扩展 {ext_id} 加载失败: {msg}")
            return
        self._apis[ext_id] = api
        rec.update(state="loaded", error="", sources=list(api.sources))
        logger.info(f"[灾害预警] 扩展 {ext_id} v{manifest.version} 已加载")

    async def start(self) -> None:
        """Start one asyncio task for each registered polling task.

        After a stop() on the same manager, the extensions are no longer
        loaded. Load them again, so a restart of the service restores the
        sources, the pollers and the envelopes that still wait.

        The lock of the manager serializes start() and stop(): the service
        locks of the host (_start_lock, _stop_lock) do not exclude each
        other, so a start that runs while a stop is in flight waits for it.
        """
        async with self._lifecycle_lock:
            if self._loaded_once and not self._apis and not self._tasks:
                self.load_all()
            specs = [(i, p) for i, api in self._apis.items() for p in api.pollers]
            if not specs or self._tasks:
                return
            self._session = aiohttp.ClientSession(
                connector=aiohttp.TCPConnector(ssl=_build_ssl_context())
            )
            for ext_id, spec in specs:
                label = f"{ext_id}/{spec.source_id}"
                self._pending.setdefault(label, [])
                self._seen.setdefault(label, {})
                self._tasks.append(
                    asyncio.create_task(
                        self._run_poller(ext_id, spec),
                        name=f"ext-poller-{ext_id}-{spec.source_id}",
                    )
                )

    async def stop(self) -> None:
        """Cancel the polling tasks, close the session and remove the registrations.

        The host makes a new service after this. An extension that is not enabled in
        the new configuration then leaves no source in the shared registries.
        The failed envelopes and the delivered identities of the polling tasks
        stay in the manager: a start() of this manager (a service restart)
        continues where the pollers stopped.
        """
        async with self._lifecycle_lock:
            tasks, self._tasks = self._tasks, []
            for t in tasks:
                t.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            if self._session is not None:
                await self._session.close()
                self._session = None
            for ext_id in list(self._apis):
                self._unregister_all(ext_id)
            self._apis.clear()

    async def _run_poller(self, ext_id: str, spec: PollerSpec) -> None:
        """Poll in a loop. Log each exception except CancelledError, then retry with backoff.

        An envelope that the host did not handle stays in the pending list,
        and the next poll gives it to the host again. An envelope whose
        identity the host already delivered is not delivered again: an
        extension that returns the current alert snapshot does not push an
        unchanged alert twice. The pending list and the delivered identities
        live in the manager: they survive a stop() and the restart of the
        pollers.
        """
        failures = 0
        last_log = float("-inf")
        label = f"{ext_id}/{spec.source_id}"
        seen = self._seen.setdefault(label, {})
        # `pending` is the list object in self._pending: _deliver() changes
        # the list in place, so every change is at once visible to a restart.
        pending = self._pending.setdefault(label, [])

        def log_throttled(msg: str) -> None:
            nonlocal last_log
            now = time.monotonic()
            if now - last_log >= self.LOG_THROTTLE_SECONDS:
                last_log = now
                logger.warning(f"[灾害预警] 扩展轮询 {label} {msg}", exc_info=True)

        while True:
            try:
                if pending:
                    # Retry the envelopes that the host refused. A retry that
                    # the host handles leaves the list inside _deliver().
                    await self._deliver(
                        pending, log_throttled, source_id=spec.source_id
                    )
                fetched = (
                    await asyncio.wait_for(
                        spec.fetch(self._session), timeout=spec.timeout_seconds
                    )
                    or []
                )
                fresh = [env for env in fetched if self._mark_seen(seen, env)]
                if fresh:
                    # The fresh envelopes join the recoverable list before
                    # the first delivery: a stop during the delivery keeps
                    # every envelope that did not finish.
                    pending += fresh
                    await self._deliver(
                        pending, log_throttled, source_id=spec.source_id
                    )
                if len(pending) > self.MAX_PENDING_ENVELOPES:
                    # Too many failed events in one cycle: keep the newest
                    # and say what was dropped. The host does not drop them in silence.
                    dropped = len(pending) - self.MAX_PENDING_ENVELOPES
                    log_throttled(
                        f"待重试事件超过 {self.MAX_PENDING_ENVELOPES} 个，丢弃最早的 {dropped} 个"
                    )
                    pending = pending[-self.MAX_PENDING_ENVELOPES :]
                    self._pending[label] = pending
                failures = 0
            except asyncio.CancelledError:
                # `pending` is the list object in self._pending (the only
                # exception is the truncation above, which syncs at once).
                raise
            except Exception as e:
                failures += 1
                log_throttled(f"拉取失败: {type(e).__name__}: {e}")
            factor = min(2**failures, self.MAX_BACKOFF_FACTOR) if failures else 1
            await asyncio.sleep(spec.interval_seconds * factor)

    def _mark_seen(self, seen: dict[str, None], envelope: Any) -> bool:
        """Mark the identity of an envelope as seen. Return False when the host already delivered it.

        The host marks the identity at the fetch: an envelope that stays in
        pending is still delivered by the retry, so an interrupted delivery
        loses nothing. An envelope without an identity takes no part in the
        deduplication.
        """
        key = _dedupe_key(envelope)
        if key is None:
            return True
        if key in seen:
            return False
        seen[key] = None
        while len(seen) > self.MAX_SEEN_EVENTS:
            seen.pop(next(iter(seen)))  # Forget the oldest identities first
        return True

    async def _deliver(
        self, batch: list, log_throttled, *, source_id: str = ""
    ) -> None:
        """Give each envelope of the batch to the host, and change the batch in place.

        An envelope leaves the batch when the host handles it. A delivery
        that the host refuses, that times out, or that raises keeps the
        envelope in the batch: the next poll gives it to the host again. A
        cancellation between two envelopes keeps every envelope that did not
        finish, so the pending list of the polling task loses nothing.

        An envelope whose source_id does not match the source of the polling
        task is rejected: an extension must not speak for another source.
        """
        index = 0
        while index < len(batch):
            env = batch[index]
            env_source = getattr(env, "source_id", None)
            if env_source is not None and source_id and env_source != source_id:
                batch.pop(index)
                log_throttled(f"拒绝信封：source_id 不属于本轮询的源: {env_source!r}")
                continue
            try:
                handled = await asyncio.wait_for(
                    self.service._handle_disaster_event(env),
                    timeout=self.DELIVERY_TIMEOUT_SECONDS,
                )
                if handled is False:
                    index += 1
                    log_throttled("处理事件失败，下次轮询重试")
                    continue
            except asyncio.TimeoutError:
                index += 1
                log_throttled(
                    f"事件处理超时（>{self.DELIVERY_TIMEOUT_SECONDS}s），下次轮询重试"
                )
                continue
            except Exception as e:
                index += 1
                log_throttled(f"处理事件失败: {type(e).__name__}: {e}")
                continue
            # The host handled the envelope: it leaves the batch.
            batch.pop(index)

    def status(self) -> list[dict]:
        """Return the load state of each extension for the admin command."""
        return [dict(r, sources=list(r["sources"])) for r in self._records]
