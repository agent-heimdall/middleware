import ast
import sys

import pytest

from middlewared.utils.boot.pool import BOOT_POOL_NAME_VALID
from middlewared.utils.zfs import managed_datasets
from middlewared.utils.zfs.managed_datasets import (
    CONTAINER_DS_NAME,
    MANAGED_DATASETS,
    SHAPE_COMPILERS,
    SHAPE_OVERRIDES,
    VIEW_MATCHERS,
    VIEW_SHAPES,
    ManagedDataset,
    MatchShape,
    View,
    blocked_from_mutation,
    effective_shape,
    excluded_from_replication,
    excluded_from_zfs_events,
    hidden_from_dataset_listing,
    hidden_from_zfs_listing,
    reserved_from_user_creation,
)

# path, ZFS_LISTING, DATASET_LISTING, MUTATION, USER_CREATE, ZFS_EVENTS, REPLICATION
#
# Every value below was derived by running the registries this module replaces against the same
# path, so the table is a record of what middleware does today. Disagreements between the columns
# are real: the views match with different shapes because their callers always did. A column
# changing here means a caller's behaviour changed.
TRUTH_TABLE = [
    # Boot pools: the pool itself and everything under it, in every view.
    ("boot-pool", True, True, True, True, True, True),
    ("boot-pool/ROOT/default", True, True, True, True, True, True),
    ("freenas-boot", True, True, True, True, True, True),
    ("freenas-boot/grub", True, True, True, True, True, True),
    # A pool whose name merely starts with a boot pool name is an ordinary pool.
    ("boot-pool-2", False, False, False, False, False, False),
    ("boot-pool-2/data", False, False, False, False, False, False),
    # Ordinary paths.
    ("tank", False, False, False, False, False, False),
    ("tank/data", False, False, False, False, False, False),
    # The system dataset directly under a pool: matched by every view.
    ("tank/.system", True, True, True, True, True, True),
    ("tank/.system/cores", True, True, True, True, True, True),
    # A snapshot suffix on the matched component defeats the whole-component views, because the
    # component being compared is then ".system@snap". One position deeper it does not, because the
    # component being compared is still ".system". The asymmetry is real; it is pinned, not
    # endorsed.
    ("tank/.system@snap", False, True, False, True, True, False),
    ("tank/.system/cores@snap", True, True, True, True, True, True),
    # Nested deeper than a pool's top level: only the substring views match.
    ("tank/foo/.system", False, True, False, True, True, False),
    # Prefix bleed: the substring views match a name that merely starts with an entry.
    ("tank/.systembackup", False, True, False, True, True, False),
    # The current apps dataset.
    ("tank/ix-apps", True, True, True, True, True, False),
    ("tank/ix-apps/docker", True, True, True, True, True, False),
    ("tank/data/ix-apps", False, True, False, True, True, False),
    ("tank/ix-apps-data", False, True, False, True, True, False),
    ("tank/ix-appsdata", False, True, False, True, True, False),
    # No bleed without the separator: "/ix-apps" does not occur in "tank/myix-apps".
    ("tank/myix-apps", False, False, False, False, False, False),
    # The legacy apps dataset. USER_CREATE and ZFS_EVENTS match only its descendants -- that is
    # SHAPE_OVERRIDES, and it is why the dataset itself is creatable but then invisible.
    ("tank/ix-applications", True, True, True, False, False, False),
    ("tank/ix-applications/releases", True, True, True, True, True, False),
    ("tank/a/b/ix-applications", False, True, False, False, False, False),
    ("tank/ix-applications-old", False, True, False, False, False, False),
    # The container dataset is hidden from the product's dataset listing and nothing else.
    (f"tank/{CONTAINER_DS_NAME}", False, True, False, False, False, False),
    (f"tank/{CONTAINER_DS_NAME}/containers", False, True, False, False, False, False),
    (f"tank/a/{CONTAINER_DS_NAME}", False, True, False, False, False, False),
    # A pool literally named after an entry is not itself managed -- the separator is required.
    ("ix-apps", False, False, False, False, False, False),
    ("ix-apps/child", False, False, False, False, False, False),
    ("ix-apps/ix-apps", True, True, True, True, True, False),
    # Degenerate input must be answered, not raised on.
    ("", False, False, False, False, False, False),
]


@pytest.mark.parametrize("path,zfs_listing,dataset_listing,mutation,user_create,zfs_events,replication", TRUTH_TABLE)
def test_predicates(path, zfs_listing, dataset_listing, mutation, user_create, zfs_events, replication):
    assert hidden_from_zfs_listing(path) is zfs_listing
    assert hidden_from_dataset_listing(path) is dataset_listing
    assert blocked_from_mutation(path) is mutation
    assert reserved_from_user_creation(path) is user_create
    assert excluded_from_zfs_events(path) is zfs_events
    assert excluded_from_replication(path) is replication


def test_leading_slash_diverges_from_the_replication_regex():
    """The one known divergence from the behaviour this registry reproduces.

    The replication view used to be a regular expression anchored with ``[^/]+``, which cannot
    match a path whose first component is empty, so it answered False for "/.system" where
    ROOT_CHILD answers True. This is unreachable -- ZFS rejects a leading slash in a dataset name,
    so no such path can exist -- and True is the more defensible of the two answers. It is pinned
    here so that nobody "corrects" it back on the strength of a diff against the old regex.
    """
    assert excluded_from_replication("/.system") is True


def test_pool_roots_are_not_blocked_from_mutation():
    """A *data* pool root must stay mutable even when it hosts the system dataset.

    Boot pool roots are deliberately the other way round; TRUTH_TABLE pins that.
    """
    for path in ("tank", "tank/"):
        assert blocked_from_mutation(path) is False


def test_every_view_has_a_shape():
    assert set(VIEW_SHAPES) == set(View)


def test_every_shape_has_a_compiler():
    assert set(SHAPE_COMPILERS) == set(MatchShape)


def test_every_view_has_a_matcher():
    assert set(VIEW_MATCHERS) == set(View)


def test_entry_names_are_unique():
    names = [entry.name for entry in MANAGED_DATASETS]
    assert len(names) == len(set(names))


def test_every_entry_belongs_to_at_least_one_view():
    for entry in MANAGED_DATASETS:
        assert entry.views, entry.name


@pytest.mark.parametrize("view", list(View))
def test_every_view_has_at_least_one_member(view):
    assert [entry for entry in MANAGED_DATASETS if view in entry.views]


def test_boot_pools_are_members_of_every_view():
    """No caller may treat a boot pool as an ordinary dataset."""
    for name in BOOT_POOL_NAME_VALID:
        entry = next(e for e in MANAGED_DATASETS if e.name == name)
        assert entry.views == frozenset(View)
        assert entry.shape is MatchShape.POOL_ROOT


def test_shape_overrides_are_exactly_the_two_known_cells():
    """SHAPE_OVERRIDES is drift that is meant to shrink. Adding to it is a deliberate act."""
    assert SHAPE_OVERRIDES == {
        (View.USER_CREATE, "ix-applications"): MatchShape.ANY_DEPTH_DESCENDANTS,
        (View.ZFS_EVENTS, "ix-applications"): MatchShape.ANY_DEPTH_DESCENDANTS,
    }


def test_shape_overrides_reference_real_cells():
    """An override naming an entry that is not in that view would silently do nothing."""
    entries = {entry.name: entry for entry in MANAGED_DATASETS}
    for view, name in SHAPE_OVERRIDES:
        assert name in entries, name
        assert view in entries[name].views, (view, name)


def test_shape_overrides_actually_change_their_cell():
    entries = {entry.name: entry for entry in MANAGED_DATASETS}
    for (view, name), shape in SHAPE_OVERRIDES.items():
        entry = entries[name]
        assert effective_shape(view, entry) is shape
        assert shape is not (entry.shape if entry.shape is not None else VIEW_SHAPES[view])


def test_entry_shape_wins_over_the_view_shape():
    boot = ManagedDataset(name="x", owner="t", views=frozenset(View), shape=MatchShape.POOL_ROOT)
    assert effective_shape(View.DATASET_LISTING, boot) is MatchShape.POOL_ROOT
    plain = ManagedDataset(name="x", owner="t", views=frozenset(View))
    assert effective_shape(View.DATASET_LISTING, plain) is VIEW_SHAPES[View.DATASET_LISTING]


def test_container_dataset_participates_in_dataset_listing_only():
    """A deliberate asymmetry: the container dataset is hidden from the UI but is otherwise an
    ordinary dataset. It is not protected from mutation and not reserved from creation."""
    entry = next(e for e in MANAGED_DATASETS if e.name == CONTAINER_DS_NAME)
    assert entry.views == frozenset({View.DATASET_LISTING})


def test_apps_datasets_are_offered_to_replication():
    """Replication is the supported way to back the apps datasets up, so they must stay listed."""
    for name in ("ix-apps", "ix-applications"):
        entry = next(e for e in MANAGED_DATASETS if e.name == name)
        assert View.REPLICATION not in entry.views
        assert excluded_from_replication(f"tank/{name}") is False


@pytest.mark.parametrize("pool", ["tank", "dozer", "ix-apps"])
@pytest.mark.parametrize("name", ["ix-apps", "ix-applications"])
@pytest.mark.parametrize("suffix", ["", "/", "/child", "/a/b"])
def test_dataset_listing_subsumes_the_apps_alert_skip(pool, name, suffix):
    """The unencrypted-datasets alert skips the apps datasets by hand while iterating children of
    an already-filtered dataset listing. Every path that skip covers is one the dataset listing has
    already removed, so the skip can never fire -- which is what makes it safe to delete rather
    than convert.
    """
    assert hidden_from_dataset_listing(f"{pool}/{name}{suffix}") is True


def test_registry_imports_only_stdlib_and_the_boot_pool_names():
    """The registry is imported from inside libzfs iteration callbacks and must stay cheap. It also
    answers questions rather than raising, so it must not reach for service exceptions."""
    with open(managed_datasets.__file__) as f:
        tree = ast.parse(f.read())

    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            assert node.level == 0, "relative imports hide the dependency"
            assert node.module is not None
            imported.add(node.module)

    for name in sorted(imported):
        assert name.split(".")[0] in sys.stdlib_module_names or name == "middlewared.utils.boot.pool", name
