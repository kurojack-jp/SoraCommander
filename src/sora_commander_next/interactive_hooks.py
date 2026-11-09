"""Release-safe hook slot for the optional DEV interactive-session feature.

The interactive Python stdin feature lives in dev_rdc_compat, which is
DEV_ONLY and never ships in a release candidate. Release-side callers
(JobManager, Host, the Windows Owner controller) therefore never import
that module. Instead they call the dispatchers here, which forward to the
hooks installed on the JobManager instance. With no hooks installed (every
release candidate and every Broker composition) each dispatcher fails
closed with PermissionError, so an interactive request is denied rather
than imported into existence.

Hooks are installed once per JobManager by
dev_rdc_compat.install_interactive_hooks, which the MCP adapter calls only
inside its exact-Host DEV registration block; tests may install them on a
fixture manager the same way. This module imports nothing local.
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class InteractiveHooks:
    """The DEV interactive entry points, as callables (see dev_rdc_compat)."""

    prepare_interactive: object
    approve_interactive_launch: object
    authorize_interactive_local: object
    input_status: object


def install_hooks(manager, hooks):
    """Install a validated DEV interactive hooks bundle on one manager."""
    if type(hooks) is not InteractiveHooks:
        raise ValueError("interactive hooks bundle required")
    if not all(callable(entry) for entry in (
            hooks.prepare_interactive, hooks.approve_interactive_launch,
            hooks.authorize_interactive_local, hooks.input_status)):
        raise ValueError("interactive hooks must be callable")
    manager.interactive_hooks = hooks


def hooks_for(manager):
    """Return the installed InteractiveHooks for a manager, or None."""
    hooks = getattr(manager, "interactive_hooks", None)
    return hooks if type(hooks) is InteractiveHooks else None


def prepare_interactive(host, request, background, decision):
    hooks = hooks_for(host._dev_interactive_jobs)
    if hooks is None:
        raise PermissionError("interactive DEV hooks unavailable")
    return hooks.prepare_interactive(host, request, background, decision)


def approve_interactive_launch(manager, request):
    hooks = hooks_for(manager)
    if hooks is None:
        raise PermissionError("interactive DEV hooks unavailable")
    return hooks.approve_interactive_launch(manager, request)


def authorize_interactive_local(host, request, **review_bounds):
    hooks = hooks_for(host._dev_interactive_jobs)
    if hooks is None:
        raise PermissionError("interactive DEV hooks unavailable")
    return hooks.authorize_interactive_local(host, request, **review_bounds)


def input_status(manager, pending_id, wait_seconds=0):
    hooks = hooks_for(manager)
    if hooks is None:
        raise PermissionError("interactive DEV hooks unavailable")
    return hooks.input_status(manager, pending_id, wait_seconds)
