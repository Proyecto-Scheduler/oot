class OotError(Exception):
    """An expected failure. The CLI prints its message without a traceback."""


class RepoStateError(OotError):
    pass


class ConfigError(OotError):
    pass


class FetchError(OotError):
    pass


class PatchError(OotError):
    """sync or install cannot proceed with the patch set as it is."""
