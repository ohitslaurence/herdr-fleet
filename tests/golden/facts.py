"""Recorded stand-in for ship's LiveFacts: the same methods, answered from a fixture."""
import importlib.machinery, importlib.util, os, tempfile

BIN = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "bin")


def load_ship():
    loader = importlib.machinery.SourceFileLoader("ship_golden", os.path.join(BIN, "ship"))
    spec = importlib.util.spec_from_loader("ship_golden", loader)
    m = importlib.util.module_from_spec(spec)
    loader.exec_module(m)
    m.STATE_DIR = tempfile.mkdtemp()
    return m


class RecordedFacts:
    """Fixture keys: pr, policy, calls{files, prev, required, installed:<p>, requests:<p>,
    lines_since:<sha>:<head>, posted, free_tries, reviewer_available, deploy:<sha>}.
    A call the fixture lacks is an error: decide() asked something new, re-record."""

    def __init__(self, fx):
        self.pr, self.pol, self.calls = fx["pr"], fx["policy"], fx["calls"]
        o, r, _, n = self.pr["url"].split("/")[3:7]
        self.o, self.r, self.n = o, r, int(n)

    def _get(self, key):
        if key not in self.calls:
            raise KeyError(f"fixture has no answer for {key}: re-record it")
        return self.calls[key]

    def files(self):
        return self._get("files")

    def prev(self):
        return self._get("prev")

    def required(self):
        v = self._get("required")
        return set(v) if v is not None else None

    def deploy(self, sha):
        v = self._get(f"deploy:{sha}")
        if v[0] == "error":
            raise RuntimeError(v[1])
        return tuple(v)

    def installed(self, P):
        return self._get(f"installed:{P.name}")

    def requests(self, P):
        return self._get(f"requests:{P.name}")

    def lines_since(self, sha, head):
        return self._get(f"lines_since:{sha}:{head}")

    def posted(self):
        return set(self._get("posted"))

    def free_tries(self):
        return self._get("free_tries")

    def reviewer_available(self, s):
        return self._get("reviewer_available")

    def craft(self):
        return self._get("craft")

    @classmethod
    def capture(cls, live):
        """Wrap LiveFacts: answer from live, remember every answer under its fixture key."""
        calls = {}

        class Capture:
            pr, pol, o, r, n = live.pr, live.pol, live.o, live.r, live.n

            def files(self):
                calls["files"] = live.files()
                return calls["files"]

            def prev(self):
                calls["prev"] = live.prev()
                return calls["prev"]

            def required(self):
                v = live.required()
                calls["required"] = sorted(v) if v is not None else None
                return v

            def deploy(self, sha):
                try:
                    v = live.deploy(sha)
                except RuntimeError as e:
                    calls[f"deploy:{sha}"] = ["error", str(e)]
                    raise
                calls[f"deploy:{sha}"] = list(v)
                return v

            def installed(self, P):
                calls[f"installed:{P.name}"] = live.installed(P)
                return calls[f"installed:{P.name}"]

            def requests(self, P):
                calls[f"requests:{P.name}"] = live.requests(P)
                return calls[f"requests:{P.name}"]

            def lines_since(self, sha, head):
                calls[f"lines_since:{sha}:{head}"] = live.lines_since(sha, head)
                return calls[f"lines_since:{sha}:{head}"]

            def posted(self):
                v = live.posted()
                calls["posted"] = sorted(v)
                return v

            def free_tries(self):
                calls["free_tries"] = live.free_tries()
                return calls["free_tries"]

            def reviewer_available(self, s):
                calls["reviewer_available"] = live.reviewer_available(s)
                return calls["reviewer_available"]

            def craft(self):
                v = live.craft()
                # decide() reads structure and verdicts only: the reviewer's text quotes private code
                calls["craft"] = {"reviews": [
                    {**{k: r[k] for k in ("head", "kind", "failed") if k in r},
                     "findings": [{"id": x["id"], "verdict": x.get("verdict")} for x in r["findings"]]}
                    for r in v["reviews"]]}
                return v

        c = Capture()
        c.calls = calls
        return c
