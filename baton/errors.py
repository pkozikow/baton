"""Errors raised by Baton commands."""




class ProjectError(Exception):
    pass


class NoProject(ProjectError):
    pass


class NativeOperationPending(ProjectError):
    """A gh stack rebase/modify/migration journal exists; only native stack members are affected."""
