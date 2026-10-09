"""Frozen v2 hash-parity vectors for continuity delta requests.

These exact SHA-256 hex digests are shared with ExoCore's
``bridge/tests/test_runtime_coverage_contracts.py`` so both repositories
prove the same canonical encoder byte-for-byte (AC-02). Change any wire
field, ordering rule, or format here and both sides must be updated
together — the digests are the parity proof.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
import unittest

from pydantic import ValidationError

from exocore_runtime.contracts import (
    ContinuityDeltaTurn,
    TurnRequest,
    canonical_turn_request_hash,
)

EMPTY_DELTA_REQUEST_ID = "11111111-2222-3333-4444-555555555555"
UNICODE_DELTA_REQUEST_ID = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"

# Frozen cross-repository parity vectors (computed once and committed).
# WARNING: the two full-request hashes embed the shared manifest fixture rows
# below, so a manifest fixture change regenerates them in the same lockstep
# checkpoint (the delta-fingerprint vector covers the delta payload only).
EMPTY_DELTA_SHA256 = "aa75ba833418bd30699c15cc978c2e314de3af95aec05929bebdc1941f22419c"
UNICODE_DELTA_SHA256 = "9c7dad5d03900a62a529069aaa6d0f4694ca772cce4d1e92cfc1eefcebb8a840"
DELTA_FINGERPRINT_SHA256 = "58c79aea31d3db029fbe7dd5f322f6d44f97175d50e2296f245106000812729a"
_RUNTIME_MCP_TOOLS = tuple(
    json.loads(
        (
            Path(__file__).resolve().parents[1]
            / "fixtures"
            / "runtime_mcp_manifest.json"
        ).read_text(encoding="utf-8")
    )
)

# The vectors above were computed from the canonical encoder by this
# repository's own implementation; ExoCore's suite asserts the same
# constants against its independent implementation (AC-02 parity).


def unicode_delta_request() -> TurnRequest:
    delta = (
        ContinuityDeltaTurn(
            role="user",
            content="第一行\n第二行 ünïcödé 🎉",
            timestamp="2026-08-29T12:00:00.123456Z",
        ),
        ContinuityDeltaTurn(role="assistant", content="ok", timestamp=None),
    )
    return TurnRequest(
        schema_version="v2",
        request_id=UNICODE_DELTA_REQUEST_ID,
        user_message="当前消息",
        requested_model_id="gemini-3.1-pro-preview",
        requested_thinking_level="auto",
        runtime_mcp_tools=_RUNTIME_MCP_TOOLS,
        bootstrap_context=None,
        continuity_delta=delta,
        ephemeral_current=None,
    )


def empty_delta_request() -> TurnRequest:
    return TurnRequest(
        schema_version="v2",
        request_id=EMPTY_DELTA_REQUEST_ID,
        user_message="hello",
        requested_model_id="gemini-3.1-pro-preview",
        requested_thinking_level="auto",
        runtime_mcp_tools=_RUNTIME_MCP_TOOLS,
        bootstrap_context=None,
        continuity_delta=(),
        ephemeral_current=None,
    )


class ContinuityDeltaContractTests(unittest.TestCase):
    def test_role_timestamp_matrix_is_exact(self) -> None:
        user = ContinuityDeltaTurn(
            role="user",
            content="",
            timestamp="2026-08-29T12:00:00.123456Z",
        )
        self.assertEqual(user.content, "")
        assistant = ContinuityDeltaTurn(
            role="assistant",
            content="ok",
            timestamp=None,
        )
        self.assertIsNone(assistant.timestamp)

    def test_invalid_role_timestamp_pairs_fail_closed(self) -> None:
        cases = (
            {"role": "user", "content": "x", "timestamp": None},
            {"role": "assistant", "content": "x", "timestamp": "2026-08-29T12:00:00.123456Z"},
            {"role": "user", "content": "x", "timestamp": "2026-08-29T12:00:00Z"},
            {"role": "user", "content": "x", "timestamp": "2026-08-29T12:00:00.123456"},
            {"role": "system", "content": "x", "timestamp": None},
        )
        for values in cases:
            with self.subTest(values=values), self.assertRaises(ValidationError):
                ContinuityDeltaTurn(**values)

    def test_turn_request_delta_must_be_an_array_and_never_null(self) -> None:
        request = empty_delta_request()
        self.assertEqual(request.continuity_delta, ())
        with self.assertRaises(ValidationError):
            TurnRequest.model_validate(
                {
                    **request.model_dump(mode="json"),
                    "continuity_delta": None,
                }
            )
        with self.assertRaises(ValidationError):
            TurnRequest.model_validate(
                {
                    **request.model_dump(mode="json"),
                    "continuity_delta": {"role": "user", "content": "x"},
                }
            )

    def test_repr_never_projects_delta_content(self) -> None:
        request = unicode_delta_request()
        self.assertNotIn("第一行", repr(request))
        self.assertNotIn("当前消息", repr(request))
        serialized = request.model_dump(mode="json")
        self.assertNotIn("第一行", str(serialized.get("user_message")))
        delta = serialized["continuity_delta"]
        self.assertEqual(delta[0]["content"], "第一行\n第二行 ünïcödé 🎉")

    def test_delta_byte_budget_is_not_truncated_or_summarized(self) -> None:
        request = empty_delta_request()
        with self.assertRaises(ValidationError):
            TurnRequest.model_validate(
                {
                    **request.model_dump(mode="json"),
                    "continuity_delta": [
                        {"role": "user", "content": "x" * 1_000_001, "timestamp": None},
                    ],
                }
            )


class ContinuityHashParityTests(unittest.TestCase):
    """Both repositories must compute these exact digests (AC-02)."""

    def test_unicode_multiline_delta_full_request_hash(self) -> None:
        request = unicode_delta_request()
        self.assertEqual(canonical_turn_request_hash(request), UNICODE_DELTA_SHA256)

    def test_empty_delta_full_request_hash(self) -> None:
        request = empty_delta_request()
        self.assertEqual(canonical_turn_request_hash(request), EMPTY_DELTA_SHA256)

    def test_any_field_change_changes_the_hash(self) -> None:
        base = unicode_delta_request()
        base_hash = canonical_turn_request_hash(base)
        mutations = (
            {
                "continuity_delta": (
                    ContinuityDeltaTurn(
                        role="user",
                        content="第一行\n第二行 ünïcödé 🎉",
                        timestamp="2026-08-29T12:00:00.123456Z",
                    ),
                    ContinuityDeltaTurn(
                        role="assistant",
                        content="ok-changed",
                        timestamp=None,
                    ),
                )
            },
            {
                "continuity_delta": (
                    ContinuityDeltaTurn(
                        role="assistant",
                        content="ok",
                        timestamp=None,
                    ),
                    ContinuityDeltaTurn(
                        role="user",
                        content="第一行\n第二行 ünïcödé 🎉",
                        timestamp="2026-08-29T12:00:00.123456Z",
                    ),
                )
            },
        )
        for mutation in mutations:
            with self.subTest(mutation=mutation):
                changed = TurnRequest(
                    **{**base.model_dump(mode="json"), **mutation}
                )
                self.assertNotEqual(canonical_turn_request_hash(changed), base_hash)

    def test_delta_fingerprint_wire_fields_match_exocore_audit(self) -> None:
        request = unicode_delta_request()
        payload = {
            "algorithm": "continuity-delta-v1",
            "turns": [
                turn.model_dump(mode="json")
                for turn in request.continuity_delta
            ],
        }
        canonical = json.dumps(
            payload,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        ).encode("utf-8")
        self.assertEqual(
            hashlib.sha256(canonical).hexdigest(),
            DELTA_FINGERPRINT_SHA256,
        )


if __name__ == "__main__":
    unittest.main()