"""Run a candidate solution against a list of assert statements in a separate process.

run_suite(code, asserts, setup) -> (all_passed, results)
results[i] = {"passed": bool, "error": str|None, "actual": repr|None}

Asserts run in order in ONE process, so state kept between calls (e.g. a call
counter) behaves the same way it would under a normal test file.
"""
import json, os, subprocess, sys, tempfile

MARK = "__HARNESS_RESULT__"

DRIVER = r'''
import ast, json, signal, sys, resource
try:
    resource.setrlimit(resource.RLIMIT_AS, (2 * 1024 ** 3, 2 * 1024 ** 3))
except Exception:
    pass
payload = json.load(open(sys.argv[1], encoding="utf-8"))
results = []

class _Timeout(Exception):
    pass

def _alarm(signum, frame):
    raise _Timeout()

signal.signal(signal.SIGALRM, _alarm)
ns = {"__name__": "__main__"}
try:
    signal.setitimer(signal.ITIMER_REAL, payload["per_assert_timeout"] * 2)
    exec(compile(payload["setup"] + "\n" + payload["code"], "solution.py", "exec"), ns)
    signal.setitimer(signal.ITIMER_REAL, 0)
    load_error = None
except BaseException as e:  # syntax errors, import errors, timeouts while loading
    signal.setitimer(signal.ITIMER_REAL, 0)
    load_error = f"{type(e).__name__}: {e}"[:300]

for src in payload["asserts"]:
    if load_error:
        results.append({"passed": False, "error": "solution failed to load: " + load_error, "actual": None})
        continue
    r = {"passed": False, "error": None, "actual": None}
    try:
        signal.setitimer(signal.ITIMER_REAL, payload["per_assert_timeout"])
        exec(src, ns)
        signal.setitimer(signal.ITIMER_REAL, 0)
        r["passed"] = True
    except AssertionError:
        signal.setitimer(signal.ITIMER_REAL, 0)
        r["error"] = "AssertionError"
        try:
            t = ast.parse(src).body[0].test
            if isinstance(t, ast.Compare):
                signal.setitimer(signal.ITIMER_REAL, payload["per_assert_timeout"])
                r["actual"] = repr(eval(ast.unparse(t.left), ns))[:300]
                signal.setitimer(signal.ITIMER_REAL, 0)
        except BaseException:
            signal.setitimer(signal.ITIMER_REAL, 0)
    except _Timeout:
        r["error"] = "Timeout"
    except BaseException as e:
        signal.setitimer(signal.ITIMER_REAL, 0)
        r["error"] = f"{type(e).__name__}: {e}"[:300]
    results.append(r)

sys.stdout.flush()
print("\n''' + MARK + r'''" + json.dumps(results))
'''


def run_suite(code, asserts, setup="", per_assert_timeout=3.0, total_timeout=30.0):
    """Never raises. Any harness problem counts as failing every assert."""
    try:
        with tempfile.TemporaryDirectory() as d:
            drv = os.path.join(d, "driver.py")
            pay = os.path.join(d, "payload.json")
            with open(drv, "w", encoding="utf-8") as f:
                f.write(DRIVER)
            with open(pay, "w", encoding="utf-8") as f:
                json.dump({"code": code or "", "asserts": list(asserts), "setup": setup or "",
                           "per_assert_timeout": per_assert_timeout}, f)
            p = subprocess.run([sys.executable, "-I", drv, pay], cwd=d, capture_output=True,
                               encoding="utf-8", errors="replace", timeout=total_timeout,
                               stdin=subprocess.DEVNULL)
            out = p.stdout
            if MARK not in out:
                err = (p.stderr or "crashed")[-300:]
                res = [{"passed": False, "error": "harness: " + err, "actual": None} for _ in asserts]
            else:
                res = json.loads(out.rsplit(MARK, 1)[1].strip().splitlines()[0])
    except subprocess.TimeoutExpired:
        res = [{"passed": False, "error": "Timeout (whole suite)", "actual": None} for _ in asserts]
    except Exception as e:
        res = [{"passed": False, "error": f"harness: {type(e).__name__}: {e}", "actual": None} for _ in asserts]
    return all(r["passed"] for r in res) and len(res) == len(asserts), res
