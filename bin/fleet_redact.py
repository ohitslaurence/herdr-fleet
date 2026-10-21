"""fleet_redact — secrets out of any text before it's sent anywhere or written to a log.

Agent output, PR text, diffs and CI logs can hold anything (a pasted .env, a curl with a password,
a key in a test fixture), so every string that leaves the machine — to a model, a phone
notification, the remote API — or reaches a log goes through redact() first, and is capped only
after: a token cut in half no longer looks like one. Patterns err on the side of over-redacting:
a lost word costs nothing, a leaked key costs a rotation.

A pattern's `keep` group (a label like `password=` or `https://`) stays readable; the rest of the
match becomes [REDACTED:<kind>].
"""
import re

# Every repeat is bounded and every pattern starts at a fixed token, so the work per character is
# constant: one hostile line (a diff, a CI log, an agent's output) can't stall the process that
# redacts it. Lines past MAX_LINE aren't scanned at all: they're replaced whole.
MAX_LINE = 8192
_W = r"[A-Za-z0-9_.-]{0,64}"  # the rest of a name around a key word (DB_PASSWORD, aws.secret_key)
# key words; a lowercase letter may not run into them (compass, passengers, tokenizer), but
# camelCase may (dbPassword, SecretAccessKey)
_KEY_WORDS = (r"(?-i:(?<![a-z])|(?=[A-Z]))(?:secret|token|pass(?:w(?:or)?d|phrase)?|pwd|cred(?:ential)?s?|api[_-]?key|private[_-]?key|"
              r"access[_-]?key|client[_-]?secret|auth[_-]?token|authorization|session[_-]?key|signing[_-]?key)"
              r"(?-i:(?![a-z]))")
_PATTERNS = [
    ("private-key", r"-----BEGIN [A-Z ]{0,40}PRIVATE KEY(?: BLOCK)?-----[\s\S]*?"
                    r"(?:-----END [A-Z ]{0,40}PRIVATE KEY(?: BLOCK)?-----|\Z)"),
    ("jwt", r"\beyJ[A-Za-z0-9_-]{8,4096}\.[A-Za-z0-9_-]{8,4096}\.[A-Za-z0-9_-]{8,4096}"),
    ("anthropic-key", r"\bsk-ant-[A-Za-z0-9_-]{16,}"),
    ("openai-key", r"\bsk-(?:proj-|svcacct-|admin-)?[A-Za-z0-9_-]{16,}"),
    ("github-token", r"\b(?:gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,})"),
    ("slack-token", r"\bx(?:ox[abposr]|app)-[A-Za-z0-9-]{10,}"),
    ("webhook", r"https://(?:hooks\.slack\.com/(?:services|workflows|triggers)|"
                r"(?:canary\.|ptb\.)?discord(?:app)?\.com/api/webhooks)/[A-Za-z0-9/_-]+"),
    ("google-key", r"\bAIza[0-9A-Za-z_-]{35}"),
    ("npm-token", r"\bnpm_[A-Za-z0-9]{36}"),
    ("gitlab-token", r"\bgl(?:pat|dt|rt|ptt|cbt|oas)-[A-Za-z0-9_-]{20,}"),
    ("aws-key-id", r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b"),
    # AWS secret access keys: exactly 40 base64 characters, mixed case with a digit (checked in _ok)
    ("aws-secret", r"(?<![A-Za-z0-9/+])[A-Za-z0-9/+]{40}(?![A-Za-z0-9/+=])"),
    ("stripe-key", r"\b[rs]k_(?:live|test)_[A-Za-z0-9]{16,}"),
    ("bearer", r"(?i)\bbearer\s{1,8}[A-Za-z0-9._~+/=-]{16,}"),
    ("basic-auth", r"(?i)(?P<keep>\bbasic\s{1,8})[A-Za-z0-9+/]{8,}={0,2}"),
    ("url-credentials", r"(?i)(?P<keep>\b[a-z][a-z0-9+.-]{0,30}://)[^\s/:@]{1,256}:[^\s/@]{1,256}(?=@)"),
    ("cli-password", r"(?P<keep>\b(?:curl|wget|http)\b[^\n]{0,200}?\s(?:-u\s{0,4}|--user[ =])[^\s:]{1,128}:)\S+"
                     r"|(?P<keep2>\b(?:mysql|mysqldump|mariadb|sshpass)\b[^\n]{0,200}?\s-p\s?)['\"]?[^\s`'\"-]\S*"),
    # key = value, key: value, "key": "value", 'key' => 'value'
    ("assignment", rf"(?i)(?P<keep>(?<![A-Za-z0-9_.-]){_W}{_KEY_WORDS}{_W}['\"]?\s{{0,8}}(?:[:=]|=>)\s{{0,8}})"
                   # code that computes a value (sessionKey = build(...)) is redacted too: a password
                   # can contain "(", and a lost line of code costs less than a leaked secret
                   r"['\"]?[^\s'\",;]{6,}"),
    # a long value right after a key word, even lowercase or hex. Bare 33-63 character hex stays
    # readable (commit SHAs are 40): a key of that shape with no label isn't caught.
    ("labelled-secret", rf"(?i)(?P<keep>(?<![A-Za-z0-9])(?:[A-Za-z0-9]{{0,64}}[_-])?(?:key|{_KEY_WORDS}|sid)"
                        r"(?:[_-][A-Za-z0-9_]{0,64})?s?[\s:=\"']{1,3})[A-Za-z0-9_+/-]{32,}"),
    # random-looking tokens: mixed case + digits (checked in _ok: not git SHAs, kebab-case names, paths)
    ("long-secret", r"(?<![A-Za-z0-9+_-])[A-Za-z0-9+_-]{32,}={0,2}"),
    ("hex-secret", r"\b[0-9a-f]{64,}\b"),
    # exactly 32 hex: md5-sized API keys (Twilio, Datadog, ...); commit SHAs are 40 and stay readable
    ("hex-secret", r"(?<![0-9A-Za-z-])[0-9a-f]{32}(?![0-9A-Za-z-])"),
]
_RX = [(kind, re.compile(rx)) for kind, rx in _PATTERNS]
_MIXED = ("aws-secret", "long-secret")  # a match counts only with a digit, a lowercase and an uppercase


def _ok(kind, text):
    return kind not in _MIXED or (any(c.isdigit() for c in text) and any(c.islower() for c in text)
                                  and any(c.isupper() for c in text))


def _sub(kind, rx, text):
    def repl(m):
        if not _ok(kind, m.group(0)):
            return m.group(0)
        keep = m.groupdict().get("keep") or m.groupdict().get("keep2") or ""
        return f"{keep}[REDACTED:{kind}]"
    return rx.sub(repl, text)


def _line(text):
    if len(text) > MAX_LINE:
        return f"[line of {len(text)} characters not shown: too long to check for secrets]"
    for kind, rx in _RX:
        text = _sub(kind, rx, text)
    return text


def redact(obj):
    """obj with secret-looking substrings replaced by [REDACTED:<kind>], recursively."""
    if isinstance(obj, str):
        if len(obj) <= MAX_LINE:
            return _line(obj)
        # line by line: a private key block spans lines, so those go first, over the whole text
        obj = _sub("private-key", _RX[0][1], obj)
        return "\n".join(_line(line) for line in obj.split("\n"))
    if isinstance(obj, dict):
        return {k: redact(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [redact(v) for v in obj]
    return obj
