"""fleet_redact: secrets out of any text, in linear time, without wrecking ordinary code and prose."""
import json, os, sys, unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "bin"))
import fleet_redact as R  # noqa: E402

# Every secret-shaped literal in this file is split ("prefix" + "body") so secret scanners and
# GitHub push protection don't flag it; keep new fixtures that way.

FAKE = {
    "openai": "sk-proj-" + "Ab3dE6gH9jK2mN5pQ8sT1vW4yZ7",
    "anthropic": "sk-ant-api03-" + "Zx9Yw8Vu7Ts6Rq5Po4Nm3Lk2",
    "github": "ghp_" + "a1B2c3D4e5F6g7H8i9J0k1L2m3N4o5P6q7R8",
    "aws": "AKIA" + "IOSFODNN7EXAMPLE",
    "slack": "xoxb-" + "123456789012-abcdefABCDEF",
    "stripe": "sk_live_" + "4eC39HqLyjWDarjtT1zdp7dc",
    "jwt": "eyJhbGciOiJIUzI1NiJ9." + "eyJzdWIiOiIxMjM0NTY3ODkwIn0." + "dozjgNryP4J3jVmNHl0w5N_XgL0n3I9PlFUP0THsR8U",
    "random": "Q2xhdWRlIGlzIGEgZ3JlYXQgbW9kZWwgZm9yIGNvZGluZw9Xk",
}

SECRETS = [
    f"export OPENAI_API_KEY={FAKE['openai']}",
    f"using {FAKE['anthropic']} for haiku",
    f"git clone https://x:{FAKE['github']}@github.com/o/r",
    f"aws key {FAKE['aws']} found",
    f"token: {FAKE['slack']}",
    f"stripe {FAKE['stripe']}",
    f"Authorization: Bearer {FAKE['jwt']}",
    "DATABASE_URL=postgres://admin:hunter2pass@db.internal:5432/app",
    "-----BEGIN RSA " + "PRIVATE KEY-----\nMIIEowIBAAKCAQEA1\n-----END RSA " + "PRIVATE KEY-----",
    'client_secret = "s3cr3tvalue123"',
    "PASSWORD=correcthorsebattery",
    f"blob {FAKE['random']}",
    "sha256 9f86d081884c7d659a2feaa0c55ad015a3bf4f1b2b0b822cd15d6c15b0f00a08f",
    # from the remote security review: formats that once slipped through
    "creds wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY here",
    '{"SecretAccessKey": "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY"}',
    '+  "password": "hunter2hunter2",',
    "+  'db_password': 'Sup3rS3cret!'",
    "-----BEGIN PGP " + "PRIVATE KEY BLOCK-----\nlQOYBF0secretmaterial\n-----END PGP " + "PRIVATE KEY BLOCK-----",
    "https://hooks.slack.com/services/" + "T0ABCDEF1/B0ABCDEF2/abcdefghijklmnopqrstuvwx",
    "https://discord.com/api/webhooks/" + "123456789/abcdefghijklmnopqrstuvwxyz",
    "xapp-" + "1-A0123456789-1234567890123-abcdef0123456789abcdef",
    "key da39a3ee5e6b4b0d3255bfef95601890afd80709deadbeef",
    "AIza" + "SyD-abcdefghijklmnopqrstuvwxyz12345",
    "npm_" + "abcdefghijklmnopqrstuvwxyz0123456789",
    "Authorization: Basic dXNlcjpTdXAzclMzY3JldA==",
    "curl -u admin:" + "Sup3rS3cret https://x",
    "mysql -uroot -p" + "Sup3rS3cret",
    "apikey_value abcdefghijklmnopqrstuvwxyz0123456789abcd",
    "see glpat-" + "zKX9mQp2vR7wT4yH8sN1 for the bot",
    "DB_PASS=Tr0ub4dor3x",
    "MYSQL_PWD=Tr0ub4dor3x",
    "aws_cred: Tr0ub4dor3x",
    "key d41d8cd98f00b204e9800998ecf8427e in CI",
    "twilio 0123456789abcdef0123456789abcdef",
    "+  dbPassword: Tr0ub4dor3x",
    "password: Tr0ub(4dor3x",
    "sshpass -p 'Tr0ub4dor3x' ssh deploy@host",
    "sshpass -pTr0ub4dor3x ssh deploy@host",
]
# what each of those must not leak
LEAKS = ["wJalrXUtnFEMI", "hunter2hunter2", "Sup3rS3cret", "secretmaterial", "B0ABCDEF2", "abcdefghijklmnopqrstuvwxyz",
         "da39a3ee", "dXNlcjpT", "zKX9mQp2", "Tr0ub4dor3x", "d41d8cd9", "0123456789abcdef0123"]
BENIGN = [
    "Fixed the null check in src/billing/invoice-service.ts",
    "commit 2e6d7f8b9a0c1d2e3f405162738495a6b7c8d9e0 merged into main",
    "branch api__export-daily-count-discrepancy-50-vs-209",
    "Want me to run the integration suite too?",
    "PR https://github.com/acme/api/pull/982 is merged",
    "the token bucket refills every 10s",
    "uuid 3f1c2b9e-7a4d-4e1b-9c2a-5d6e7f8a9b0c",
    "author=claude, authored by the agent",  # "auth" inside a word is not a credential
    "src/components/OrderSummary/useOrderSummaryTotals.ts",
    "-p 8080:8080 and mkdir -p build/out",
    "the session key rotation is documented in docs/keys.md",
    "the monkey inside src/components/OrderSummary/useOrderSummaryTotals.ts",
    # from the second review: code that looks like a credential to a careless pattern
    "+  passengers: 12345678",
    "+  compass_heading=north12",
    "+  tokenizer: bert-base-uncased",
    "+    docker run -u 1000:1000 image",
    "use `mysql -p` and type the password when asked",
]


class Redaction(unittest.TestCase):
    def test_hostile_input_takes_linear_time(self):
        import time
        lines = ["a-" * 4000, "a." * 4000, "a_" * 4000, "aA1/" * 2000, "token" * 1600, "key:" * 2000,
                 "a://" + "b:" * 4000, "mysql " + "x " * 4000, "basic " * 1300, "-----BEGIN PRIVATE KEY-----" * 300]
        text = "\n".join(lines * 16)  # about 1 MB
        start = time.perf_counter()
        R.redact(text)
        self.assertLess(time.perf_counter() - start, 2.0)

    def test_overlong_lines_are_replaced_not_scanned(self):
        out = R.redact("ok line\n" + "x" * 10_000 + f" {FAKE['github']}\nlast")
        self.assertEqual(out.split("\n")[0], "ok line")
        self.assertIn("not shown", out.split("\n")[1])
        self.assertNotIn(FAKE["github"], out)

    def test_secrets_are_removed(self):
        for text in SECRETS:
            out = R.redact(text)
            self.assertIn("[REDACTED:", out, text)
        for value in FAKE.values():
            self.assertNotIn(value, R.redact(" ".join(SECRETS)))
        self.assertNotIn("hunter2pass", R.redact(SECRETS[7]))
        self.assertNotIn("s3cr3tvalue123", R.redact(SECRETS[9]))
        everything = R.redact("\n".join(SECRETS))
        for leak in LEAKS:
            self.assertNotIn(leak, everything)

    def test_normal_text_survives(self):
        for text in BENIGN:
            self.assertEqual(R.redact(text), text)

    def test_nested(self):
        out = R.redact({"a": [f"key {FAKE['openai']}"], "b": {"c": "fine"}, "n": 3})
        self.assertNotIn(FAKE["openai"], json.dumps(out))
        self.assertEqual(out["b"]["c"], "fine")
        self.assertEqual(out["n"], 3)


if __name__ == "__main__":
    unittest.main()
