"""AgentRegistry: discovers and registers agent types, generates orchestrator tool definitions."""
from agents.base import BaseAgent


class AgentRegistry:
    """Registry of specialist agents. Auto-generates orchestrator tool definitions."""

    def __init__(self) -> None:
        self._agents: dict[str, BaseAgent] = {}

    def register(self, agent: BaseAgent) -> None:
        self._agents[agent.name] = agent

    def get(self, name: str) -> BaseAgent:
        return self._agents[name]

    def all_agents(self) -> dict[str, BaseAgent]:
        return dict(self._agents)

    def orchestrator_tools(self) -> list[dict]:
        """Generate tool definitions for the orchestrator from registered agents."""
        tools = []
        for agent in self._agents.values():
            tools.append({
                "name": agent.name,
                "description": agent.description,
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "question": {
                            "type": "string",
                            "description": "The refined question to answer or the request to fulfill.",
                        },
                    },
                    "required": ["question"],
                },
            })
        return tools
