"""Refusing mutation of a dataset that middleware manages on the user's behalf.

:mod:`middlewared.utils.zfs.managed_datasets` answers whether a path is one of ours; this module
turns that answer into a refusal. The two are separate on purpose: the registry runs inside libzfs
iteration callbacks and must stay free of any middleware runtime dependency, while raising needs
:mod:`middlewared.service_exception`.

A caller that owns the path -- the apps plugin working on ``<pool>/ix-apps``, the system dataset
plugin working on ``<pool>/.system`` -- says so by passing :attr:`InternalAccess.ALLOW`, which is
also the grep token for auditing every place that privilege is exercised.
"""

import enum
import errno

from middlewared.service_exception import ValidationError
from middlewared.utils.zfs.managed_datasets import blocked_from_mutation

__all__ = (
    "InternalAccess",
    "deny_protected_path",
    "deny_protected_snapshot",
)


class InternalAccess(str, enum.Enum):
    """Whether the caller is the subsystem that owns the path it is about to change.

    Values are strings so that the choice survives the JSON hop taken by ``failover.call_remote``
    on its way to the other HA node.
    """

    DENY = "DENY"
    """The default everywhere: a managed dataset is off limits."""

    ALLOW = "ALLOW"
    """The caller owns this path and is allowed to change it."""


def _resolve(access: InternalAccess | str) -> InternalAccess:
    """Coerce rather than compare by identity.

    A value that arrived over ``failover.call_remote`` is a plain string, so an identity test
    against the enum would silently read as "not ALLOW" -- which is safe -- or, with the comparison
    the other way round, as "not DENY" -- which is not. Coercion also makes anything that is neither
    (``True``, ``None``, a typo) raise here instead of being interpreted, so a caller that got it
    wrong fails closed and loudly.
    """
    return InternalAccess(access)


def deny_protected_path(schema: str, path: str, access: InternalAccess | str = InternalAccess.DENY) -> None:
    """Raise unless `path` may be mutated by this caller.

    `path` is taken as written. A *data* pool root is never protected, even when it hosts a managed
    dataset, so ``tank`` passes while ``tank/.system`` does not. A boot pool is protected up to its
    own name, so ``boot-pool`` is refused as well.
    """
    if _resolve(access) is not InternalAccess.ALLOW and blocked_from_mutation(path):
        raise ValidationError(schema, f"{path!r} is a protected path.", errno.EACCES)


def deny_protected_snapshot(schema: str, path: str, access: InternalAccess | str = InternalAccess.DENY) -> None:
    """Raise unless the dataset `path` names or is a snapshot of may be mutated by this caller.

    `path` may be either a dataset or a ``<dataset>@<snapshot>`` name; the decision is about the
    dataset either way, so the snapshot suffix is dropped before asking. The message still quotes
    what the caller passed. Callers that are not about to touch a snapshot want
    :func:`deny_protected_path`, which does not strip -- ``zfs.resource.destroy`` has to keep
    telling the caller to use ``zfs.resource.snapshot.destroy`` instead.
    """
    if _resolve(access) is not InternalAccess.ALLOW and blocked_from_mutation(path.split("@", 1)[0]):
        raise ValidationError(schema, f"{path!r} is a protected path.", errno.EACCES)
