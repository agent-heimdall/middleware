"""Guard against a sixth registry of middleware-managed datasets being grown somewhere else.

Five of them existed before ``middlewared.utils.zfs.managed_datasets``, each with its own membership
list and its own matching algorithm, and they had drifted apart from one another in ways nobody
noticed because nothing tied them together. What lets that happen again is that spelling a dataset
name inline is easy and invisible, so this module makes it visible.

Three scans, in decreasing order of how much they catch:

* every string literal that names a managed dataset,
* every symbol whose name is shaped like a registry,
* every reference to the registry's own internals, which is how a sixth registry would most likely
  be built -- on top of the fifth rather than beside it.

Each scan carries a both-directions allowlist: an unexpected hit fails, and so does an allowlist
entry that no longer matches anything. Every reason is either "owns this dataset" -- the subsystem
that creates and maintains it, which necessarily knows its name -- or a marker for a spelling that
has not been converged onto the registry yet. The second kind is the backlog.
"""

import ast
import pathlib
import re

MIDDLEWARED_ROOT = pathlib.Path(__file__).resolve().parents[4]
"""The ``middlewared`` package directory."""

REGISTRY_MODULE = "utils/zfs/managed_datasets.py"
REGISTRY_TESTS = "pytest/unit/utils/zfs/test_managed_datasets.py"

SKIPPED_TREES = ("pytest/", "alembic/")
"""``pytest/`` holds this file and the registry's own tests, both of which quote dataset names by
design. ``alembic/`` is generated migration history and is excluded from linting everywhere else."""


MANAGED_NAMES = (
    ".system",
    ".truenas_containers",
    "boot-pool",
    "freenas-boot",
    "ix-apps",
    "ix-applications",
)

LITERAL_RE = re.compile("(?:" + "|".join(re.escape(name) for name in MANAGED_NAMES) + r")(?![A-Za-z0-9_])")
"""Matches a managed dataset name anywhere inside a literal -- the old registries spelled these as
``'/ix-applications/'``, ``f'{pool}/ix-apps/'`` and ``r'[^/]+/\\.system($|/)'``, so an equality test
would have missed every one of them. The trailing lookahead is what keeps ``.system`` from matching
identifiers such as ``system.system_dataset`` or ``org.freedesktop.systemd1``."""

SYMBOL_RE = re.compile(
    r"(internal|invalid|excluded?|reserved|hidden|protected|forbidden|banned|skipped?|ignored?)_?(path|dataset)s?",
    re.IGNORECASE,
)
"""Matches registry-shaped symbol names. Catches four of the five originals by name alone:
``INTERNAL_PATHS``, ``has_internal_path``, ``INTERNAL_DATASETS``, ``is_internal_dataset`` and
``INVALID_DATASETS``."""

REGISTRY_INTERNALS = (
    "MANAGED_DATASETS",
    "SHAPE_COMPILERS",
    "SHAPE_OVERRIDES",
    "VIEW_MATCHERS",
    "VIEW_SHAPES",
)
"""The registry's own tables. Callers ask a predicate; anyone reaching for these is deriving a
second membership set from the first, which is how the five originals came to disagree."""


LITERAL_ALLOWLIST = {
    "plugins/boot/__init__.py": "boot: owns the boot pool",
    "plugins/boot/pool_ops.py": "boot: owns the boot pool",
    "plugins/catalog/utils.py": "apps: owns the ix-apps dataset",
    "plugins/cloud_sync/rclone.py": "not converged: rclone filter lines, not a membership test",
    "plugins/docker/backup.py": "apps: owns the ix-apps and ix-applications datasets",
    "plugins/docker/backup_to_pool.py": "apps: owns the ix-apps and ix-applications datasets",
    "plugins/docker/config.py": "apps: owns the ix-apps and ix-applications datasets",
    "plugins/docker/fs_manage.py": "not converged: boot pool prefix test, should use the registry",
    "plugins/docker/state_utils.py": "apps: owns the ix-apps and ix-applications datasets",
    "plugins/docker/utils.py": "apps: owns the ix-apps and ix-applications datasets",
    "plugins/pool_/export.py": "not converged: leftover-directory cleanup, not a membership test",
    "plugins/pool_/import_pool.py": "not converged: names one dataset to skip, should use the registry",
    "plugins/sysdataset.py": "sysdataset: owns the .system dataset",
    "plugins/system_dataset/hierarchy.py": "sysdataset: owns the .system dataset",
    "plugins/system_dataset/mount.py": "sysdataset: owns the .system dataset",
    "plugins/zettarepl.py": "not converged: builds a zettarepl exclusion string, not a membership test",
    "test/integration/utils/docker.py": "apps: owns the ix-apps dataset",
    "utils/boot/pool.py": "boot: owns the boot pool, and defines the names the registry imports",
}

SYMBOL_ALLOWLIST = {
    "exclude_internal_datasets": "flag on pool.dataset.query, selects the dataset-listing view",
    "exclude_internal_paths": "flag on zfs.resource.query, selects the ZFS-listing view",
    "__should_exclude_internal_paths": (
        "the auto-opt-out: naming a managed path explicitly turns the filter off for that query"
    ),
    "deny_protected_path": "the mutation guard, which asks the registry rather than answering itself",
}
"""Symbols that consume a view rather than define one. These are flags and guards, not registries."""


def _iter_source_files() -> list[tuple[str, ast.Module]]:
    """Every parsed module under ``middlewared/`` that a registry could hide in."""
    found = []
    for path in sorted(MIDDLEWARED_ROOT.rglob("*.py")):
        relative = path.relative_to(MIDDLEWARED_ROOT).as_posix()
        if relative.startswith(SKIPPED_TREES):
            continue
        found.append((relative, ast.parse(path.read_text())))
    return found


SOURCE_FILES = _iter_source_files()


def _docstring_nodes(tree: ast.Module) -> set[int]:
    """Identity of every docstring constant, so prose does not count as a spelling."""
    nodes = set()
    for node in ast.walk(tree):
        body = getattr(node, "body", None)
        if not isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)) or not body:
            continue
        first = body[0]
        if isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant) and isinstance(first.value.value, str):
            nodes.add(id(first.value))
    return nodes


def _string_literals(tree: ast.Module) -> list[str]:
    """Every non-docstring string constant, including the pieces of an f-string.

    ``ast.walk`` descends into :class:`ast.JoinedStr`, so the literal halves of ``f"{pool}/ix-apps"``
    arrive here as ordinary constants -- which is how a registry written with f-strings gets caught.
    """
    docstrings = _docstring_nodes(tree)
    return [
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant) and isinstance(node.value, str) and id(node) not in docstrings
    ]


def _symbol_names(tree: ast.Module) -> list[str]:
    """Every name a module defines or refers to."""
    names = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.append(node.name)
        elif isinstance(node, ast.Name):
            names.append(node.id)
        elif isinstance(node, ast.Attribute):
            names.append(node.attr)
        elif isinstance(node, ast.arg):
            names.append(node.arg)
        elif isinstance(node, ast.keyword) and node.arg is not None:
            names.append(node.arg)
    return names


def test_managed_dataset_names_are_only_spelled_by_their_owners():
    """No module outside the registry names a managed dataset unless it owns it."""
    offenders = {}
    for relative, tree in SOURCE_FILES:
        if relative == REGISTRY_MODULE:
            continue
        hits = sorted({literal for literal in _string_literals(tree) if LITERAL_RE.search(literal)})
        if hits:
            offenders[relative] = hits

    unexpected = {path: hits for path, hits in offenders.items() if path not in LITERAL_ALLOWLIST}
    assert not unexpected, (
        "these modules spell a managed dataset name inline; use a predicate from "
        f"middlewared.utils.zfs.managed_datasets, or add the module to LITERAL_ALLOWLIST with the "
        f"reason it owns the dataset: {unexpected}"
    )

    stale = sorted(set(LITERAL_ALLOWLIST) - set(offenders))
    assert not stale, f"LITERAL_ALLOWLIST entries no longer match anything and should be dropped: {stale}"


def test_no_module_defines_a_registry_shaped_symbol():
    """No module grows a symbol named like an internal-path list."""
    offenders = {}
    for relative, tree in SOURCE_FILES:
        if relative == REGISTRY_MODULE:
            continue
        hits = sorted({name for name in _symbol_names(tree) if SYMBOL_RE.search(name) and name not in SYMBOL_ALLOWLIST})
        if hits:
            offenders[relative] = hits

    assert not offenders, (
        "these symbols look like a second registry of managed datasets; ask "
        f"middlewared.utils.zfs.managed_datasets instead: {offenders}"
    )

    seen = {name for _, tree in SOURCE_FILES for name in _symbol_names(tree)}
    stale = sorted(set(SYMBOL_ALLOWLIST) - seen)
    assert not stale, f"SYMBOL_ALLOWLIST entries no longer match anything and should be dropped: {stale}"


def test_registry_internals_stay_inside_the_registry():
    """Nobody derives their own membership set from the registry's tables."""
    offenders = {}
    for path in sorted(MIDDLEWARED_ROOT.rglob("*.py")):
        relative = path.relative_to(MIDDLEWARED_ROOT).as_posix()
        if relative in (REGISTRY_MODULE, REGISTRY_TESTS):
            continue
        names = set(_symbol_names(ast.parse(path.read_text())))
        hits = sorted(names.intersection(REGISTRY_INTERNALS))
        if hits:
            offenders[relative] = hits

    assert not offenders, (
        "these modules reach into the registry's tables rather than calling one of its predicates, "
        f"which is how a second registry gets built on top of the first: {offenders}"
    )
