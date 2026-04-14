"""
axor-claude — Claude adapter for axor-core.

Quick start:

    import axor_claude
    session = axor_claude.make_session()
    result = await session.run("refactor the auth module")
"""

from axor_claude.executor import ClaudeCodeExecutor
from axor_claude.tools.read import ReadHandler
from axor_claude.tools.write import WriteHandler
from axor_claude.tools.bash import BashHandler
from axor_claude.tools.search import SearchHandler
from axor_claude.tools.glob import GlobHandler
from axor_claude.extensions.skill_loader import ClaudeSkillLoader
from axor_claude.extensions.plugin_loader import ClaudePluginLoader
from axor_claude.tool_definitions import register_tool_definition
from axor_claude import normalizer

__version__ = "0.1.0"

__all__ = [
    "ClaudeCodeExecutor",
    "ReadHandler", "WriteHandler", "BashHandler",
    "SearchHandler", "GlobHandler",
    "ClaudeSkillLoader", "ClaudePluginLoader",
    "register_tool_definition",
    "normalizer",
    "make_session",
]


def make_session(
    api_key=None,
    *,
    tools=("read", "write", "bash", "search", "glob"),
    load_skills=True,
    load_plugins=True,
    model=None,
    system_prompt=None,
    **session_kwargs,
):
    """
    Convenience factory. Creates GovernedSession with all standard handlers.

    Example:
        session = axor_claude.make_session(soft_token_limit=100_000)
        result = await session.run("refactor auth module")
    """
    from axor_core import GovernedSession, CapabilityExecutor

    _handlers = {
        "read": ReadHandler, "write": WriteHandler, "bash": BashHandler,
        "search": SearchHandler, "glob": GlobHandler,
    }

    cap = CapabilityExecutor()
    for name in tools:
        if cls := _handlers.get(name):
            cap.register(cls())

    loaders = []
    if load_skills:
        loaders.append(ClaudeSkillLoader())
    if load_plugins:
        loaders.append(ClaudePluginLoader())

    executor_kwargs = {}
    if model:
        executor_kwargs["model"] = model
    if system_prompt:
        executor_kwargs["system_prompt"] = system_prompt

    return GovernedSession(
        executor=ClaudeCodeExecutor(api_key=api_key, **executor_kwargs),
        capability_executor=cap,
        extension_loaders=loaders,
        **session_kwargs,
    )
