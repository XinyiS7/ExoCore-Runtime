"""CP3 (B-prime): project rules as a frozen field, identity, and rendering."""

import hashlib

from exocore_runtime.contracts import (
    PROJECT_RULES_MAX_CHARS,
    GenerationSpec,
    generation_identity,
    project_rules_sha256,
)
from exocore_runtime.providers.antigravity.renderer import (
    PROJECT_RULES_HEADING,
    extract_rendered_system_instructions,
    render_agent_markdown,
)

import unittest

AGENT = "exocore-runtime-abc123"


def build_spec(**overrides) -> GenerationSpec:
    payload = {
        "runtime_kind": "antigravity",
        "bootstrap_fingerprint": "fingerprint-1",
        "system_instructions": "system instructions body",
    }
    payload.update(overrides)
    return GenerationSpec(**payload)


class ProjectRulesContractTests(unittest.TestCase):
    def test_absent_empty_and_present_are_distinct_states(self) -> None:
        self.assertIsNone(build_spec().project_rules)
        self.assertEqual(build_spec(project_rules="").project_rules, "")
        self.assertEqual(build_spec(project_rules="rule").project_rules, "rule")

    def test_boundary_length_is_exactly_the_frozen_limit(self) -> None:
        self.assertEqual(PROJECT_RULES_MAX_CHARS, 256_000)
        accepted = build_spec(project_rules="x" * PROJECT_RULES_MAX_CHARS)
        self.assertEqual(len(accepted.project_rules), PROJECT_RULES_MAX_CHARS)
        with self.assertRaises(ValueError):
            build_spec(project_rules="x" * (PROJECT_RULES_MAX_CHARS + 1))

    def test_rules_digest_keeps_presence_apart_from_content(self) -> None:
        digests = {
            project_rules_sha256(None),
            project_rules_sha256(""),
            project_rules_sha256("rule"),
        }
        self.assertEqual(len(digests), 3)
        self.assertEqual(project_rules_sha256("rule"), project_rules_sha256("rule"))

    def test_generation_identity_carries_the_rules_state(self) -> None:
        identities = {
            generation_identity(build_spec()),
            generation_identity(build_spec(project_rules="")),
            generation_identity(build_spec(project_rules="rule")),
        }
        self.assertEqual(len(identities), 3)
        # System-instruction identity semantics stay untouched: the same rules
        # with different instructions still differ, and equal inputs match.
        self.assertEqual(
            generation_identity(build_spec(project_rules="rule")),
            generation_identity(build_spec(project_rules="rule")),
        )
        self.assertNotEqual(
            generation_identity(build_spec(project_rules="rule")),
            generation_identity(
                build_spec(project_rules="rule", system_instructions="other body")
            ),
        )


class ProjectRulesRenderingTests(unittest.TestCase):
    def test_absent_rules_render_no_rules_section(self) -> None:
        markdown = render_agent_markdown(AGENT, "body")
        self.assertNotIn(PROJECT_RULES_HEADING, markdown)
        self.assertEqual(extract_rendered_system_instructions(AGENT, markdown), "body")

    def test_empty_rules_still_render_the_section(self) -> None:
        markdown = render_agent_markdown(AGENT, "body", "")
        self.assertIn(PROJECT_RULES_HEADING, markdown)
        self.assertEqual(
            extract_rendered_system_instructions(AGENT, markdown, project_rules=""),
            "body",
        )

    def test_rules_render_verbatim_and_round_trip(self) -> None:
        rules = "# Rules\n\nAlways answer with RULE-ACK.\n"
        markdown = render_agent_markdown(AGENT, "body", rules)
        self.assertIn(rules, markdown)
        self.assertLess(markdown.index("body"), markdown.index(PROJECT_RULES_HEADING))
        self.assertEqual(
            extract_rendered_system_instructions(
                AGENT, markdown, project_rules=rules
            ),
            "body",
        )

    def test_rendering_is_deterministic(self) -> None:
        rules = "rule body"
        self.assertEqual(
            render_agent_markdown(AGENT, "body", rules),
            render_agent_markdown(AGENT, "body", rules),
        )

    def test_extraction_requires_the_matching_expectation_when_rules_present(self) -> None:
        markdown = render_agent_markdown(AGENT, "body", "rule")
        # R1-02: without a rules expectation there is no marker scan, so the
        # middle is recovered as-is; a rules-present generation is protected by
        # the strict ending check plus the system/full-agent hashes.
        recovered = extract_rendered_system_instructions(AGENT, markdown)
        self.assertIn("## ExoCore Project Rules", recovered)
        with self.assertRaises(ValueError):
            extract_rendered_system_instructions(
                AGENT, markdown, project_rules="other"
            )

    def test_extraction_refuses_tampered_rules_section(self) -> None:
        markdown = render_agent_markdown(AGENT, "body", "rule")
        tampered = markdown.replace("rule", "tampered", 1)
        with self.assertRaises(ValueError):
            extract_rendered_system_instructions(
                AGENT, tampered, project_rules="rule"
            )

    def test_full_markdown_hash_covers_the_rules_section(self) -> None:
        first = render_agent_markdown(AGENT, "body", "rule one")
        second = render_agent_markdown(AGENT, "body", "rule two")
        self.assertNotEqual(
            hashlib.sha256(first.encode("utf-8")).hexdigest(),
            hashlib.sha256(second.encode("utf-8")).hexdigest(),
        )
        # The instructions body hash stays identical: rules never leak into the
        # system-instruction identity.
        self.assertEqual(
            hashlib.sha256(
                extract_rendered_system_instructions(
                    AGENT, first, project_rules="rule one"
                ).encode("utf-8")
            ).hexdigest(),
            hashlib.sha256(
                extract_rendered_system_instructions(
                    AGENT, second, project_rules="rule two"
                ).encode("utf-8")
            ).hexdigest(),
        )


if __name__ == "__main__":
    unittest.main()


import unittest as _unittest

from exocore_runtime.contracts import (
    GenerationSpec as _GenerationSpec,
    generation_identity as _generation_identity,
    generation_identity_parts as _generation_identity_parts,
    project_rules_absent_digest as _project_rules_absent_digest,
    project_rules_identity_digest as _project_rules_identity_digest,
    project_rules_sha256 as _project_rules_sha256,
    system_instructions_sha256 as _system_instructions_sha256,
)
from exocore_runtime.providers.antigravity.renderer import (
    extract_rendered_system_instructions as _extract,
    render_agent_markdown as _render,
)


class ProjectRulesIdentityCompatibilityTests(_unittest.TestCase):
    """R1-03: a rules-free generation keeps the exact pre-CP3 identity."""

    # Golden values produced by the committed CP2 module
    # (git show 40d79f2:src/exocore_runtime/contracts.py) for this exact spec.
    CP2_GOLDEN_IDENTITY = "aff5ef0b8773161dc3d39acdedbcf061af9a595c5e10004aafbccd914d6c40dc"
    CP2_GOLDEN_SYSTEM_DIGEST = "7dba7179ec15487a17f707b5b11c28db48aceb874a0cedaf1be4d5aea34fe244"

    def legacy_spec(self, **overrides) -> _GenerationSpec:
        values = {
            "runtime_kind": "antigravity",
            "bootstrap_fingerprint": "cp2-legacy-fingerprint",
            "system_instructions": "Legacy system instructions.",
        }
        values.update(overrides)
        return _GenerationSpec(**values)

    def test_rules_free_identity_matches_the_committed_cp2_payload(self) -> None:
        self.assertEqual(
            _system_instructions_sha256("Legacy system instructions."),
            self.CP2_GOLDEN_SYSTEM_DIGEST,
        )
        self.assertEqual(_generation_identity(self.legacy_spec()), self.CP2_GOLDEN_IDENTITY)
        self.assertIsNone(_project_rules_identity_digest(None))
        self.assertEqual(
            _generation_identity_parts(
                runtime_kind="antigravity",
                bootstrap_fingerprint="cp2-legacy-fingerprint",
                system_instructions_digest=self.CP2_GOLDEN_SYSTEM_DIGEST,
                project_rules_digest=None,
            ),
            self.CP2_GOLDEN_IDENTITY,
        )

    def test_present_rules_rotate_the_identity_and_stay_distinct(self) -> None:
        absent = _generation_identity(self.legacy_spec())
        empty = _generation_identity(self.legacy_spec(project_rules=""))
        body = _generation_identity(self.legacy_spec(project_rules="rule"))
        self.assertEqual(len({absent, empty, body}), 3)
        self.assertNotEqual(_project_rules_identity_digest(""), None)
        self.assertEqual(
            _project_rules_sha256(None), _project_rules_absent_digest()
        )


class ProjectRulesRenderingRoundTripTests(_unittest.TestCase):
    """R1-02: system text may legitimately contain the rules heading."""

    AGENT = "exocore-rt-test-agent"

    def test_rules_free_markdown_round_trips_system_text_with_the_heading(self) -> None:
        body = (
            "Intro line.\n\n"
            "## ExoCore Project Rules\n\n"
            "This heading is part of my own system text.\n\n"
            "Closing line."
        )
        markdown = _render(self.AGENT, body)
        self.assertIn("## ExoCore Project Rules", markdown)
        self.assertEqual(
            _extract(self.AGENT, markdown, project_rules=None),
            body,
        )
        self.assertEqual(markdown, _render(self.AGENT, body))

    def test_rules_present_markdown_still_rejects_tampering(self) -> None:
        markdown = _render(self.AGENT, "System body.", "Rule body.")
        self.assertEqual(
            _extract(self.AGENT, markdown, project_rules="Rule body."),
            "System body.",
        )
        with self.assertRaises(ValueError):
            _extract(
                self.AGENT,
                markdown.replace("Rule body.", "Edited rules."),
                project_rules="Rule body.",
            )
