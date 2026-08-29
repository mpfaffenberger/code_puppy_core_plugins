"""The deliberately tiny continuation classifier agent."""

from code_puppy.agents.base_agent import BaseAgent


class AutoContinueAgent(BaseAgent):
    """Decide whether a completed response merely needs permission to continue."""

    @property
    def name(self) -> str:
        return "auto-continue"

    @property
    def display_name(self) -> str:
        return "Auto Continue "

    @property
    def description(self) -> str:
        return "Approves routine requests to continue an already requested task"

    def get_available_tools(self) -> list[str]:
        return []

    def get_system_prompt(self) -> str:
        return """You classify whether an assistant is merely waiting for permission to
continue routine work the user already requested.

Reply with exactly one of these strings and nothing else:
- yes, go.
- continue
- okay
- NO

Approve only a clear request to continue, proceed, or perform an explicitly
described next step within the existing task. Use NO for destructive or risky
actions, credential or payment requests, ambiguous choices, requests for new
requirements, completed work, or anything needing a real human decision.
Never use tools and never explain your answer."""
