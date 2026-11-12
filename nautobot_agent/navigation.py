"""Navigation menu items for the Nautobot Agent app."""

from nautobot.apps.ui import NavigationWeightChoices, NavMenuGroup, NavMenuItem, NavMenuTab

menu_items = (
    NavMenuTab(
        name="Nautobot Agent",
        weight=NavigationWeightChoices.APPS - 50,
        groups=(
            NavMenuGroup(
                name="Chat",
                weight=100,
                items=(
                    NavMenuItem(
                        link="plugins:nautobot_agent:agent",
                        name="Agent",
                        weight=100,
                    ),
                    NavMenuItem(
                        link="plugins:nautobot_agent:agentconversation_list",
                        name="Conversation History",
                        weight=150,
                        permissions=["nautobot_agent.view_agentconversation"],
                    ),
                    NavMenuItem(
                        link="plugins:nautobot_agent:agentconversation_all",
                        name="All Conversations",
                        weight=160,
                        permissions=["nautobot_agent.viewall_agentconversation"],
                    ),
                ),
            ),
            NavMenuGroup(
                name="Configuration",
                weight=200,
                items=(
                    NavMenuItem(
                        link="plugins:nautobot_agent:llmconfiguration_list",
                        name="LLM Configurations",
                        weight=100,
                        permissions=["nautobot_agent.view_llmconfiguration"],
                    ),
                ),
            ),
            NavMenuGroup(
                name="Integrations",
                weight=300,
                items=(
                    NavMenuItem(
                        link="plugins:nautobot_agent:skill_list",
                        name="Skills",
                        weight=100,
                        permissions=["nautobot_agent.view_skill"],
                    ),
                    NavMenuItem(
                        link="plugins:nautobot_agent:mcpserver_list",
                        name="MCP Servers",
                        weight=150,
                        permissions=["nautobot_agent.view_mcpserver"],
                    ),
                ),
            ),
            NavMenuGroup(
                name="Observability",
                weight=400,
                items=(
                    NavMenuItem(
                        link="plugins:nautobot_agent:agentexecutiontrace_list",
                        name="Traces",
                        weight=100,
                        permissions=["nautobot_agent.view_agentexecutiontrace"],
                    ),
                ),
            ),
        ),
    ),
)
