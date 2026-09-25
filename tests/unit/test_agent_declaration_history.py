"""Frozen declaration-history invariants and byte-exact shape recognition.

Frozen authority: ``Plan/Archived/Subscription_Runtime_AGY_Legacy_Tool_Declaration_Upgrade_Plan.md``
section 5 (discipline guard) and section 4 (the two historical templates). This
file is the reason a future tool-set edit cannot silently brick live
generations or leave a running process on a stale declaration.
"""

import unittest

from exocore_runtime.providers.antigravity.capabilities import SECURITY_POLICY_REVISION
from exocore_runtime.providers.antigravity.renderer import (
    AGENT_TOOLS,
    AGENT_TOOLSET_HISTORY,
    LEGACY_AGENT_TOOLSETS,
    extract_rendered_system_instructions,
    render_agent_markdown,
)


AGENT_NAME = "exocore-runtime-history"
DESCRIPTION_LINE = "description: ExoCore generation-private subscription runtime agent."
BODY = "system instructions body"
RULES = "## frozen project rules"

# The frozen historical declarations, literal by design (plan section 4).
L3_FRONTMATTER = (
    "---\n"
    f"name: {AGENT_NAME}\n"
    f"{DESCRIPTION_LINE}\n"
    "tools:\n"
    "  - view_file\n"
    "  - write_to_file\n"
    "  - run_command\n"
    "---\n"
)
L0_FRONTMATTER = (
    "---\n"
    f"name: {AGENT_NAME}\n"
    f"{DESCRIPTION_LINE}\n"
    "---\n"
)

# Frozen policy revision -> declared tool set. Every shipped policy is listed in
# order; the last entry must match the live policy, and a tool-set change must
# always carry a new revision so a live process reacquires the declaration.
FROZEN_POLICY_HISTORY = (
    ("agy-seven-deny-v1", None),
    ("agy-tool-perm-v2", ("view_file", "write_to_file", "run_command")),
    ("agy-tool-perm-v3", ("view_file", "write_to_file", "run_command")),
    ("agy-tool-perm-v4", ("view_file", "write_to_file", "run_command", "search_web")),
    # v5 widens the Runtime-bound Memory MCP tool surface (enabledTools + eager)
    # without touching the declared AGY toolset, so the tool tuple repeats.
    ("agy-tool-perm-v5", ("view_file", "write_to_file", "run_command", "search_web")),
)

L3_TOOLSET = ("view_file", "write_to_file", "run_command")


def body_of(rendered_markdown: str) -> str:
    return rendered_markdown.split("---\n", 2)[2]


class DeclarationHistoryTests(unittest.TestCase):
    def test_history_tail_is_the_live_declaration(self) -> None:
        self.assertEqual(AGENT_TOOLSET_HISTORY[-1], AGENT_TOOLS)
        self.assertEqual(LEGACY_AGENT_TOOLSETS, AGENT_TOOLSET_HISTORY[:-1])
        self.assertEqual(LEGACY_AGENT_TOOLSETS[-1], L3_TOOLSET)

    def test_toolset_changes_always_carry_a_new_policy_revision(self) -> None:
        self.assertEqual(FROZEN_POLICY_HISTORY[-1], (SECURITY_POLICY_REVISION, AGENT_TOOLS))
        # The declaration history lists distinct tool sets in order, so two
        # neighbouring entries never repeat the same set; a permission-only
        # revision (v2 -> v3) keeps its slot in the policy history instead.
        for previous, current in zip(AGENT_TOOLSET_HISTORY, AGENT_TOOLSET_HISTORY[1:]):
            with self.subTest(tools=current):
                self.assertNotEqual(previous, current)
        distinct_toolsets: list[tuple[str, ...] | None] = []
        for _, tools in FROZEN_POLICY_HISTORY:
            if not distinct_toolsets or distinct_toolsets[-1] != tools:
                distinct_toolsets.append(tools)
        self.assertEqual(distinct_toolsets, list(AGENT_TOOLSET_HISTORY))
        for (previous_revision, previous_tools), (revision, tools) in zip(
            FROZEN_POLICY_HISTORY, FROZEN_POLICY_HISTORY[1:]
        ):
            with self.subTest(revision=revision):
                if previous_tools != tools:
                    # Without a revision bump an already-live process would keep
                    # serving the previous declaration until an unrelated respawn.
                    self.assertNotEqual(previous_revision, revision)
        self.assertEqual(distinct_toolsets[:-1], list(LEGACY_AGENT_TOOLSETS))

    def test_recognizer_accepts_every_historical_shape_literally(self) -> None:
        cases = (
            (L3_FRONTMATTER, L3_TOOLSET, None, BODY),
            (L3_FRONTMATTER, L3_TOOLSET, RULES, BODY),
            (L0_FRONTMATTER, None, None, BODY),
        )
        for frontmatter, tools, project_rules, expected in cases:
            with self.subTest(tools=tools, rules=project_rules is not None):
                # The body comes from the production renderer (instructions,
                # optional rules section, transport trailer); only the
                # frontmatter is the frozen historical literal.
                reference = render_agent_markdown(AGENT_NAME, expected, project_rules)
                legacy = frontmatter + body_of(reference)
                self.assertEqual(
                    extract_rendered_system_instructions(
                        AGENT_NAME,
                        legacy,
                        project_rules=project_rules,
                        tools=tools,
                    ),
                    expected,
                )
                with self.assertRaises(ValueError):
                    # The current policy never recognizes a historical shape.
                    extract_rendered_system_instructions(
                        AGENT_NAME,
                        legacy,
                        project_rules=project_rules,
                    )

    def test_recognizer_rejects_structural_near_misses(self) -> None:
        body = body_of(render_agent_markdown(AGENT_NAME, BODY))
        near_misses = {
            "empty-tools-key": (
                "---\n"
                f"name: {AGENT_NAME}\n"
                f"{DESCRIPTION_LINE}\n"
                "tools:\n"
                "---\n"
            ),
            "reordered-tools": (
                "---\n"
                f"name: {AGENT_NAME}\n"
                f"{DESCRIPTION_LINE}\n"
                "tools:\n"
                "  - run_command\n"
                "  - view_file\n"
                "  - write_to_file\n"
                "---\n"
            ),
            "extra-key": (
                "---\n"
                f"name: {AGENT_NAME}\n"
                f"{DESCRIPTION_LINE}\n"
                "model: gemini\n"
                "---\n"
            ),
            "rewritten-description": (
                "---\n"
                f"name: {AGENT_NAME}\n"
                "description: something else\n"
                "---\n"
            ),
        }
        for label, frontmatter in near_misses.items():
            with self.subTest(shape=label):
                markdown = frontmatter + body
                for tools in (None, L3_TOOLSET):
                    with self.assertRaises(ValueError):
                        extract_rendered_system_instructions(
                            AGENT_NAME,
                            markdown,
                            tools=tools,
                        )

    def test_empty_tool_declaration_is_not_a_renderable_shape(self) -> None:
        markdown = render_agent_markdown(AGENT_NAME, BODY)
        with self.assertRaises(ValueError):
            extract_rendered_system_instructions(AGENT_NAME, markdown, tools=())


if __name__ == "__main__":
    unittest.main()
