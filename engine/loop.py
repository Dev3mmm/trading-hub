"""Long-running hosted worker (one GitHub Actions job, ~5.4 h, then it re-dispatches itself).
 - starts the news sniper (live EUR/USD quotes around releases) as a background process
 - every 10 min: full update (trackers, scanner, trends, bots, scalp book, forex) + publish
 - inside a release window (-3 min .. +25 min) or when the sniper logs a finished session: forex-only refresh + publish every ~75 s
GitHub's own cron is unreliable (it fired ~every 2-3 h), so this loop keeps the site fresh and the cron is only a restart safety net."""
import os, sys, json, time, shutil, subprocess

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PUB = os.environ.get("PUBLISH_DIR", os.path.join(ROOT, "publish"))
FX = os.path.join(ROOT, "engine", "forex")
PUSH = os.environ.get("PUSH_URL", "")
LIFETIME = float(os.environ.get("LOOP_HOURS", "5.4")) * 3600
FULL_EVERY = 600
env = dict(os.environ, HOSTED="1", PUBLISH_DIR=PUB)


def sh(args, cwd=None, check=True):
    return subprocess.run(args, cwd=cwd, env=env, check=check, capture_output=True, text=True)


def publish(msg):
    if not PUSH:
        print("no PUSH_URL, skipping publish")
        return
    shutil.rmtree(os.path.join(PUB, ".git"), ignore_errors=True)
    sh(["git", "init", "-q", "-b", "gh-pages"], cwd=PUB)
    sh(["git", "config", "user.name", "hub-bot"], cwd=PUB)
    sh(["git", "config", "user.email", "hub-bot@users.noreply.github.com"], cwd=PUB)
    sh(["git", "add", "-A"], cwd=PUB)
    sh(["git", "commit", "-q", "-m", msg], cwd=PUB)
    for i in range(3):
        r = sh(["git", "push", "-q", "--force", PUSH, "gh-pages"], cwd=PUB, check=False)
        if r.returncode == 0:
            return
        time.sleep(5)
    print("push failed:", r.stderr[-300:])


def run(fast):
    r = subprocess.run([sys.executable, os.path.join(ROOT, "engine", "run_all.py")] + (["--fast"] if fast else []), env=env, capture_output=True, text=True)
    print(("FAST" if fast else "FULL"), "rc", r.returncode, (r.stdout[-300:] if r.returncode else ""), (r.stderr[-400:] if r.returncode else ""), flush=True)
    return r.returncode == 0


def near_release():
    try:
        d = json.load(open(os.path.join(PUB, "data", "state.json")))
    except Exception:
        return False
    now = time.time() * 1000
    return any(-3 * 60000 <= e["T"] - now <= 25 * 60000 for e in d.get("events", []) if e.get("cur") in ("USD", "EUR", "GBP", "JPY", "AUD", "CAD", "CHF", "NZD"))


def snipmtime():
    try:
        return os.path.getmtime(os.path.join(FX, "sniper_log.json"))
    except OSError:
        return 0


def main():
    t_end = time.time() + LIFETIME
    os.makedirs(PUB, exist_ok=True)
    if not os.path.exists(os.path.join(PUB, "data")):  # first ever run: nothing to restore
        pass
    # full run first: it restores state (so the sniper log/ticks exist before the sniper starts appending)
    ok = run(False)
    publish("full update")
    sniper = subprocess.Popen([sys.executable, os.path.join(FX, "sniper.py")], cwd=FX, env=env, stdout=open(os.path.join(FX, "sniper.out"), "w"), stderr=subprocess.STDOUT)
    last_full, last_fast, last_snip = time.time(), 0, snipmtime()
    while time.time() < t_end:
        time.sleep(15)
        now = time.time()
        if sniper.poll() is not None:  # sniper died: restart it
            sniper = subprocess.Popen([sys.executable, os.path.join(FX, "sniper.py")], cwd=FX, env=env, stdout=open(os.path.join(FX, "sniper.out"), "a"), stderr=subprocess.STDOUT)
        if now - last_full >= FULL_EVERY:
            run(False)
            publish("full update")
            last_full = last_fast = time.time()
            continue
        sm = snipmtime()
        if (near_release() and now - last_fast >= 75) or sm != last_snip:
            run(True)
            publish("forex refresh")
            last_fast, last_snip = time.time(), sm
    sniper.terminate()
    publish("final")


if __name__ == "__main__":
    main()
