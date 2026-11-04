"""Offline SSM parameter and pinned-content regressions; no AWS engine claims."""

import copy
import json
from pathlib import Path
import re
import unittest

from test_actions import SSMFake, config, envelope, release
from ssm_transport import dispatch

DOCUMENT = Path(__file__).resolve().parents[1] / "ssm-document.json"


def oversized_bounded_repeats(pattern):
    """Guard this document's simple regex subset against Go/RE2's 1000-repeat cap.

    This is a static regression check, not a general parser or SSM emulator.
    See https://pkg.go.dev/regexp/syntax (implementation restriction).
    """
    return [
        match.group()
        for match in re.finditer(r"\{([0-9]+)(?:,([0-9]*))?\}", pattern)
        if any(int(bound) > 1000 for bound in match.groups() if bound)
    ]


class SSMDocumentTests(unittest.TestCase):
    def setUp(self):
        self.document = json.loads(DOCUMENT.read_text())
        self.parameter = self.document["parameters"]["ReleaseManifest"]

    def test_parameter_avoids_ssm_repeat_limit_and_keeps_explicit_length(self):
        self.assertEqual(self.parameter["allowedPattern"], r"^[A-Za-z0-9+/=]+$")
        self.assertIs(type(self.parameter["maxChars"]), int)
        self.assertEqual(self.parameter["maxChars"], 4096)
        self.assertEqual(oversized_bounded_repeats(self.parameter["allowedPattern"]), [])
        # Python accepts the old expression; the documented Go/RE2 cap does not.
        old = r"^[A-Za-z0-9+/=]{1,4096}$"
        re.compile(old)
        self.assertEqual(oversized_bounded_repeats(old), ["{1,4096}"])
        self.assertEqual(oversized_bounded_repeats("a{1001}b{1,}c{1,1000}"), ["{1001}"])

    def test_manifest_alphabet_empty_and_length_boundaries(self):
        # Python checks values only; the separate test guards engine restrictions.
        def accepted(value):
            return (
                len(value) <= self.parameter["maxChars"]
                and re.fullmatch(self.parameter["allowedPattern"], value) is not None
            )

        for value in ("aA09+/=", "A" * 4096):
            with self.subTest(valid_length=len(value)):
                self.assertTrue(accepted(value))
        for value in ("", "A" * 4097, "a-", "a_", "a ", "a\n", "$(id)", "é"):
            with self.subTest(invalid=value[:20], length=len(value)):
                self.assertFalse(accepted(value))

    def dispatch(self, fake):
        return dispatch(fake, config(), envelope(release(), "e" * 64), self.document)

    def test_get_document_formatting_is_semantic_and_hash_is_aws_reported(self):
        fake = SSMFake()
        call = fake.call

        def reformatted(operation, arguments):
            response = call(operation, arguments)
            if operation == "get-document":
                response["Content"] = json.dumps(fake.document, indent=4, sort_keys=True) + "\n"
            return response

        fake.call = reformatted
        # Fixture hash is deliberately not the hash of either JSON serialization.
        self.dispatch(fake)
        self.assertEqual(fake.calls.count("send-command"), 1)

    def test_get_document_tampering_rejected_before_send(self):
        for field, value in (("allowedPattern", "^.*$"), ("maxChars", 4097)):
            with self.subTest(field=field):
                fake = SSMFake()
                fake.document["parameters"]["ReleaseManifest"][field] = value
                with self.assertRaisesRegex(ValueError, "pinned_document_content"):
                    self.dispatch(fake)
                self.assertNotIn("send-command", fake.calls)
        fake = SSMFake()
        fake.document = copy.deepcopy(self.document)
        fake.document["mainSteps"][0]["inputs"]["runCommand"] = ["echo tampered"]
        with self.assertRaisesRegex(ValueError, "pinned_document_content"):
            self.dispatch(fake)
        self.assertNotIn("send-command", fake.calls)

    def test_description_hash_type_and_version_still_pinned(self):
        for field, value in (("HashType", "Sha1"), ("Hash", "0" * 64), ("DocumentVersion", "2")):
            with self.subTest(field=field):
                fake = SSMFake()
                fake.description[field] = value
                with self.assertRaisesRegex(ValueError, "pinned_document_description"):
                    self.dispatch(fake)
                self.assertNotIn("send-command", fake.calls)
