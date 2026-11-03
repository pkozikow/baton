"""The project manager: one stateful object assembled from per-area mixins."""

from .lifecycle import LifecycleMixin
from .artifacts import ArtifactsMixin
from .members import MembersMixin
from .rename import RenameMixin
from .cleanup import CleanupMixin
from .abandon import AbandonMixin
from .follow_up import FollowUpMixin
from .status import StatusMixin
from .retirement import RetirementMixin
from .sessions import SessionsMixin
from .search import SearchMixin


# The mixins define disjoint methods over ManagerCore, so their order does not change behavior.
class Manager(LifecycleMixin, ArtifactsMixin, MembersMixin, RenameMixin, CleanupMixin, AbandonMixin, FollowUpMixin, StatusMixin, RetirementMixin, SessionsMixin, SearchMixin):
    pass
