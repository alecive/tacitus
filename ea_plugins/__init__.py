"""ea_plugins — shared helpers for personal EA plugins.

Plugins live under ~/.ea/plugins/<name>/ and run via run-plugin.sh. They
get scoped access to a few shared utilities (notably the ledger), and
this package houses those helpers.

See docs/plugin-architecture.md for the contract.
"""
