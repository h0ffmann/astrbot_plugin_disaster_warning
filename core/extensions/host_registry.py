"""
Host registry adapter. It sends the register and unregister requests of an
extension to the host registration functions.

Each method imports its host function late. The module can then load
before the host registries are ready.
"""

from __future__ import annotations


class HostRegistry:
    """The host_registry for production. A test can use a fake with the same methods."""

    def register_source_entry(self, entry, *, owner: str) -> None:
        """Add a source entry to the host source catalog."""
        from ..sources.source_catalog import register_source_entry

        register_source_entry(entry, owner=owner)

    def register_parser(self, parser_name: str, parser_class, *, owner: str) -> None:
        """Add a parser class to the host parser registry."""
        from ..parsers.parser_registry import register_parser

        register_parser(parser_name, parser_class, owner=owner)

    def register_text_presenter(self, key: str, presenter_class, *, owner: str) -> None:
        """Add a text presenter class to the host presenter registry."""
        from ..message.presenters.presenter_registry import register_text_presenter

        register_text_presenter(key, presenter_class, owner=owner)

    def register_rule(self, rule, *, after: str = "local_rule", owner: str) -> None:
        """Add a rule to the host rule chain."""
        from ..rules.rule_chain import register_rule

        register_rule(rule, after=after, owner=owner)

    def missing_source_parser_names(self, owner: str) -> dict[str, str]:
        """Return {source_id: parser_name} of the owner for parsers that the host does not have.

        The manager calls this after setup() of an extension. A source that
        references an unregistered parser would stop the host from starting
        (validate_catalog_parser_names). The manager isolates it instead.
        """
        from ..parsers.parser_registry import missing_catalog_parser_names
        from ..sources.source_catalog import get_source_owner

        return {
            sid: name
            for sid, name in missing_catalog_parser_names().items()
            if get_source_owner(sid) == owner
        }

    def unregister_source_entries(self, owner: str):
        """Remove all source entries of the owner from the host source catalog."""
        from ..sources.source_catalog import unregister_source_entries

        return unregister_source_entries(owner)

    def unregister_parsers(self, owner: str) -> None:
        """Remove all parsers of the owner."""
        from ..parsers.parser_registry import unregister_parsers

        unregister_parsers(owner)

    def unregister_presenters(self, owner: str) -> None:
        """Remove all text presenters of the owner."""
        from ..message.presenters.presenter_registry import unregister_presenters

        unregister_presenters(owner)

    def unregister_rules(self, owner: str) -> None:
        """Remove all rules of the owner from the rule chain."""
        from ..rules.rule_chain import unregister_rules

        unregister_rules(owner)
