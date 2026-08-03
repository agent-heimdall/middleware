"""The single registry of ZFS datasets that middleware manages on the user's behalf.

Middleware creates and maintains a handful of datasets that are not the user's to touch: the boot
pools, the system dataset, the apps datasets and the container dataset. Several subsystems need to
ask whether a given path is one of those, but each of them asks a *different* question -- hiding a
dataset from a listing is not the same decision as refusing to destroy it -- and historically each
grew its own membership list and its own matching algorithm.

This module models that as four separate concepts, each edited in exactly one place:

* an **entry** (:class:`ManagedDataset`) -- one row per managed dataset name,
* a **view** (:class:`View`) -- one per caller decision, each with a predicate at the bottom of
  this module,
* a **shape** (:class:`MatchShape`) -- how a name is matched against a path, bound *per view* in
  :data:`VIEW_SHAPES` so that a view's matching behaviour is a one-line change,
* an **override** (:data:`SHAPE_OVERRIDES`) -- a single (view, entry) cell whose shape disagrees
  with its view. This is quarantined drift, not a feature.

The module answers questions; it never raises. It deliberately imports nothing beyond the standard
library and the boot pool names, so that the hot ZFS iteration callbacks can import it cheaply.
"""

from collections.abc import Callable
from dataclasses import dataclass
import enum
from typing import TypeAlias

from middlewared.utils.boot.pool import BOOT_POOL_NAME_VALID

__all__ = (
    "CONTAINER_DS_NAME",
    "MANAGED_DATASETS",
    "SHAPE_COMPILERS",
    "SHAPE_OVERRIDES",
    "VIEW_MATCHERS",
    "VIEW_SHAPES",
    "ManagedDataset",
    "MatchShape",
    "View",
    "blocked_from_mutation",
    "effective_shape",
    "excluded_from_replication",
    "excluded_from_zfs_events",
    "hidden_from_dataset_listing",
    "hidden_from_zfs_listing",
    "reserved_from_user_creation",
)


CONTAINER_DS_NAME = ".truenas_containers"
"""Name of the per-pool dataset that holds container storage."""


class MatchShape(enum.Enum):
    """How an entry name is matched against a ZFS path."""

    POOL_ROOT = "POOL_ROOT"
    """The first path component equals the name: the pool itself and everything under it."""

    ROOT_CHILD = "ROOT_CHILD"
    """The second path component equals the name exactly: a top-level child of any pool, plus its
    descendants. The comparison is against a whole component, so a path carrying a snapshot suffix
    on that component (``tank/.system@snap``) does not match, while a snapshot of a descendant
    (``tank/.system/cores@snap``) does."""

    ANY_DEPTH = "ANY_DEPTH"
    """``/<name>`` occurs anywhere in the path, unanchored. This matches at any depth but also
    bleeds into names that merely start with an entry (``tank/ix-apps-data``)."""

    ANY_DEPTH_DESCENDANTS = "ANY_DEPTH_DESCENDANTS"
    """``/<name>/`` occurs anywhere in the path. As :attr:`ANY_DEPTH`, except that the dataset
    itself does not match -- only its descendants do."""


class View(enum.Enum):
    """A caller's decision. Each view has one predicate at the bottom of this module."""

    ZFS_LISTING = "ZFS_LISTING"
    DATASET_LISTING = "DATASET_LISTING"
    MUTATION = "MUTATION"
    USER_CREATE = "USER_CREATE"
    ZFS_EVENTS = "ZFS_EVENTS"
    REPLICATION = "REPLICATION"


VIEW_SHAPES: dict[View, MatchShape] = {
    View.ZFS_LISTING: MatchShape.ROOT_CHILD,
    View.DATASET_LISTING: MatchShape.ANY_DEPTH,
    View.MUTATION: MatchShape.ROOT_CHILD,
    View.USER_CREATE: MatchShape.ANY_DEPTH,
    View.ZFS_EVENTS: MatchShape.ANY_DEPTH,
    View.REPLICATION: MatchShape.ROOT_CHILD,
}
"""The shape each view matches with. Converging two views onto one behaviour is a one-line edit
here, which is the point of keeping shape separate from membership."""


@dataclass(frozen=True, slots=True, kw_only=True)
class ManagedDataset:
    """One managed dataset name and the views it participates in."""

    name: str
    """The dataset name as it appears in a path, without any pool prefix."""

    owner: str
    """The subsystem that creates and maintains it, for whoever reads this table next."""

    views: frozenset[View]
    """The views this entry is a member of."""

    shape: MatchShape | None = None
    """Set only when the shape is a property of the entry rather than of the caller -- a boot pool
    is a pool root no matter who is asking. ``None`` means "whatever the view matches with"."""


_ALL_VIEWS = frozenset(View)
_APPS_VIEWS = frozenset(View) - {View.REPLICATION}

_BOOT_POOL_ENTRIES = tuple(
    ManagedDataset(
        name=name,
        owner="boot",
        views=_ALL_VIEWS,
        shape=MatchShape.POOL_ROOT,
    )
    for name in BOOT_POOL_NAME_VALID
)

MANAGED_DATASETS: tuple[ManagedDataset, ...] = (
    *_BOOT_POOL_ENTRIES,
    ManagedDataset(name=".system", owner="sysdataset", views=_ALL_VIEWS),
    ManagedDataset(name="ix-apps", owner="apps", views=_APPS_VIEWS),
    ManagedDataset(name="ix-applications", owner="apps", views=_APPS_VIEWS),
    ManagedDataset(name=CONTAINER_DS_NAME, owner="container", views=frozenset({View.DATASET_LISTING})),
)
"""Every dataset middleware manages, and which decisions it takes part in.

The apps datasets are absent from :attr:`View.REPLICATION` because replication is how users back
them up. The container dataset takes part in dataset listing only: it is hidden from the UI but is
an ordinary dataset as far as every other view is concerned."""


SHAPE_OVERRIDES: dict[tuple[View, str], MatchShape] = {
    (View.USER_CREATE, "ix-applications"): MatchShape.ANY_DEPTH_DESCENDANTS,
    (View.ZFS_EVENTS, "ix-applications"): MatchShape.ANY_DEPTH_DESCENDANTS,
}
"""Cells whose shape disagrees with their view.

Both entries here say the same thing: ``<pool>/ix-applications`` itself is creatable, only its
descendants are reserved. That is drift, not intent -- every other view treats the dataset and its
descendants alike -- but changing it would change behaviour, so it is recorded rather than quietly
converged. This mapping exists so that such drift has a home; it is meant to shrink."""


Matcher: TypeAlias = Callable[[str], bool]


def _compile_pool_root(names: frozenset[str]) -> Matcher:
    def matcher(path: str) -> bool:
        return path.split("/", 1)[0] in names

    return matcher


def _compile_root_child(names: frozenset[str]) -> Matcher:
    def matcher(path: str) -> bool:
        components = path.split("/")
        return len(components) > 1 and components[1] in names

    return matcher


def _compile_any_depth(names: frozenset[str]) -> Matcher:
    needles = tuple(f"/{name}" for name in sorted(names))

    def matcher(path: str) -> bool:
        return any(needle in path for needle in needles)

    return matcher


def _compile_any_depth_descendants(names: frozenset[str]) -> Matcher:
    needles = tuple(f"/{name}/" for name in sorted(names))

    def matcher(path: str) -> bool:
        return any(needle in path for needle in needles)

    return matcher


SHAPE_COMPILERS: dict[MatchShape, Callable[[frozenset[str]], Matcher]] = {
    MatchShape.POOL_ROOT: _compile_pool_root,
    MatchShape.ROOT_CHILD: _compile_root_child,
    MatchShape.ANY_DEPTH: _compile_any_depth,
    MatchShape.ANY_DEPTH_DESCENDANTS: _compile_any_depth_descendants,
}


def effective_shape(view: View, entry: ManagedDataset) -> MatchShape:
    """Return the shape one cell of the table is matched with.

    A per-cell override wins over an entry that pins its own shape, which in turn wins over the
    view's default.
    """
    override = SHAPE_OVERRIDES.get((view, entry.name))
    if override is not None:
        return override
    return entry.shape if entry.shape is not None else VIEW_SHAPES[view]


def _compile_view(view: View) -> Matcher:
    """Build one matcher for a view, grouping its members by the shape they are matched with.

    These predicates run inside libzfs iteration callbacks, once per dataset on a system that can
    have tens of thousands of them, so the per-shape name sets and their string needles are built
    here at import time. A call then costs one pass per distinct shape in the view -- at most four,
    in practice two -- rather than one per table entry.
    """
    by_shape: dict[MatchShape, set[str]] = {}
    for entry in MANAGED_DATASETS:
        if view in entry.views:
            by_shape.setdefault(effective_shape(view, entry), set()).add(entry.name)

    matchers = tuple(SHAPE_COMPILERS[shape](frozenset(names)) for shape, names in by_shape.items())

    def matcher(path: str) -> bool:
        return any(match(path) for match in matchers)

    return matcher


VIEW_MATCHERS: dict[View, Matcher] = {view: _compile_view(view) for view in View}


def hidden_from_zfs_listing(path: str) -> bool:
    """Whether `path` is omitted from ``zfs.resource`` query results by default."""
    return VIEW_MATCHERS[View.ZFS_LISTING](path)


def hidden_from_dataset_listing(path: str) -> bool:
    """Whether `path` is omitted from ``pool.dataset`` query results by default."""
    return VIEW_MATCHERS[View.DATASET_LISTING](path)


def blocked_from_mutation(path: str) -> bool:
    """Whether `path` may be created, destroyed or changed only by the subsystem that owns it.

    A *data* pool root is never blocked, even when it hosts the system dataset -- users have to be
    able to lock and destroy their own pools. A boot pool is the exception: it is matched by
    :attr:`MatchShape.POOL_ROOT`, so the bare pool name is blocked along with everything under it.
    """
    return VIEW_MATCHERS[View.MUTATION](path)


def reserved_from_user_creation(path: str) -> bool:
    """Whether `path` names a location a user may not create a dataset at or under."""
    return VIEW_MATCHERS[View.USER_CREATE](path)


def excluded_from_zfs_events(path: str) -> bool:
    """Whether changes to `path` seen on the ZFS event channel are dropped rather than published as
    middleware events."""
    return VIEW_MATCHERS[View.ZFS_EVENTS](path)


def excluded_from_replication(path: str) -> bool:
    """Whether `path` is withheld from the dataset list offered when configuring replication.

    The apps datasets are deliberately offered: replication is the supported way to back them up.
    """
    return VIEW_MATCHERS[View.REPLICATION](path)
