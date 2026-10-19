#!/usr/bin/env python3
"""
Load test for the Items CRUD API that demonstrates Kubernetes autoscaling.

The test runs a series of load stages (ramp up -> peak -> ramp down), then stops
sending traffic and keeps watching. While it runs it prints, every few seconds:

  * request rate, error count and latency
  * how many distinct backend pods answered (from the X-Served-By header)
  * the backend Deployment's replica count and the HPA's CPU reading (kubectl)

At the end it prints the replica timeline, checks that the backend scaled up
under load and back down after the load dropped, and writes an HTML report
(--open shows it in your browser).

Example:
    pip install -r requirements.txt
    python load_test.py --url http://127.0.0.1:54321 --open
    python load_test.py --url http://127.0.0.1:54321 --stages 20:5,60:40,60:10
"""

import argparse
import asyncio
import json
import random
import shutil
import sys
import time
import webbrowser
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

import aiohttp

HERE = Path(__file__).resolve().parent


# --------------------------------------------------------------------------- #
# Configuration
# --------------------------------------------------------------------------- #

@dataclass
class Stage:
    duration: int  # seconds
    users: int     # concurrent virtual users


def parse_stages(spec: str) -> list[Stage]:
    stages = []
    for part in spec.split(","):
        try:
            duration, users = part.split(":")
            stages.append(Stage(int(duration), int(users)))
        except ValueError:
            raise argparse.ArgumentTypeError(
                f"bad stage '{part}', expected DURATION:USERS (e.g. 60:50)"
            )
    if not stages:
        raise argparse.ArgumentTypeError("at least one stage is required")
    return stages


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Load test the CRUD API and watch the backend autoscale.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    p.add_argument("--url", default="http://localhost:8080",
                   help="Base URL of the app (frontend nginx; /api is proxied to the backend)")
    p.add_argument("--stages", type=parse_stages,
                   default=parse_stages("20:5,60:30,90:60,45:15,30:3"),
                   help="Comma-separated DURATION_SECONDS:CONCURRENT_USERS load stages")
    p.add_argument("--cooldown", type=int, default=300,
                   help="Max seconds to keep watching (with no load) for scale-down after the stages")
    p.add_argument("--interval", type=int, default=5, help="Seconds between status lines")
    p.add_argument("--namespace", default="crud-app")
    p.add_argument("--deployment", default="backend")
    p.add_argument("--hpa", default="backend")
    p.add_argument("--no-kubectl", action="store_true",
                   help="Don't query kubectl; rely only on X-Served-By to count pods")
    p.add_argument("--timeout", type=float, default=10.0, help="Per-request timeout (s)")
    p.add_argument("--report", type=Path, default=None,
                   help="HTML report path (default: results/report-<timestamp>.html next to this script)")
    p.add_argument("--open", action="store_true", help="Open the HTML report in a browser when done")
    return p.parse_args()


# --------------------------------------------------------------------------- #
# Stats
# --------------------------------------------------------------------------- #

@dataclass
class Window:
    """Stats for one reporting interval."""
    requests: int = 0
    errors: int = 0
    capped: int = 0  # POSTs refused because the server's item cap was reached
    latencies: list = field(default_factory=list)
    pods: set = field(default_factory=set)
    error_types: dict = field(default_factory=dict)

    def record(self, latency: float, ok: bool, pod: str | None, err: str | None = None):
        self.requests += 1
        self.latencies.append(latency)
        if pod:
            self.pods.add(pod)
        if not ok:
            self.errors += 1
            if err:
                self.error_types[err] = self.error_types.get(err, 0) + 1


@dataclass
class Totals:
    requests: int = 0
    errors: int = 0
    capped: int = 0
    pods: set = field(default_factory=set)
    error_types: dict = field(default_factory=dict)


def pct(values: list, q: float) -> float:
    if not values:
        return 0.0
    values = sorted(values)
    return values[min(len(values) - 1, int(q * len(values)))]


# --------------------------------------------------------------------------- #
# Load generation
# --------------------------------------------------------------------------- #

class LoadGenerator:
    def __init__(self, base_url: str, timeout: float):
        self.api = base_url.rstrip("/") + "/api/items"
        self.timeout = aiohttp.ClientTimeout(total=timeout)
        self.target_users = 0
        self.window = Window()
        self.totals = Totals()
        self.stopping = False
        self._workers: list[asyncio.Task] = []
        self.session: aiohttp.ClientSession | None = None

    async def __aenter__(self):
        self.session = aiohttp.ClientSession(
            timeout=self.timeout, connector=aiohttp.TCPConnector(limit=0)
        )
        return self

    async def __aexit__(self, *exc):
        # Let workers leave their loops and delete what they created, then
        # cancel any that are still busy after the grace period.
        self.stopping = True
        if self._workers:
            _, pending = await asyncio.wait(self._workers, timeout=60)
            for w in pending:
                w.cancel()
            await asyncio.gather(*self._workers, return_exceptions=True)
        await self.session.close()

    def set_users(self, n: int):
        """Change the number of active virtual users, spawning workers as needed."""
        self.target_users = n
        while len(self._workers) < n:
            self._workers.append(asyncio.create_task(self._worker(len(self._workers))))

    def take_window(self) -> Window:
        w, self.window = self.window, Window()
        self.totals.requests += w.requests
        self.totals.errors += w.errors
        self.totals.capped += w.capped
        self.totals.pods |= w.pods
        for k, v in w.error_types.items():
            self.totals.error_types[k] = self.totals.error_types.get(k, 0) + v
        return w

    async def _call(self, method: str, url: str, expect: tuple, body=None):
        start = time.perf_counter()
        pod, ok, err, data = None, False, None, None
        try:
            async with self.session.request(method, url, json=body) as resp:
                pod = resp.headers.get("X-Served-By")
                if resp.status == 507:  # item cap reached: expected, not a failure
                    self.window.capped += 1
                    ok = True
                    await resp.read()
                else:
                    if resp.status != 204:
                        text = await resp.text()
                        if resp.content_type == "application/json" and text:
                            data = json.loads(text)
                    ok = resp.status in expect
                    if not ok:
                        err = f"{method} HTTP {resp.status}"
        except asyncio.CancelledError:
            raise
        except Exception as e:  # timeouts, connection resets, ...
            err = f"{method} {type(e).__name__}"
        self.window.record(time.perf_counter() - start, ok, pod, err)
        return ok, data

    async def _worker(self, idx: int):
        my_ids: list[str] = []
        rnd = random.Random(idx)
        while not self.stopping:
            if idx >= self.target_users:
                await asyncio.sleep(0.2)  # parked: this user isn't active now
                continue

            # Mix: 50% list, 15% create, 10% get, 10% update, 15% delete.
            # Creates and deletes are balanced so the collection stays small.
            r = rnd.random()
            if r < 0.50 or (not my_ids and r < 0.65):
                await self._call("GET", f"{self.api}?limit=20", (200,))
            elif r < 0.65 or not my_ids:
                ok, data = await self._call(
                    "POST", self.api, (201,),
                    {"name": f"load-{idx}-{rnd.randint(0, 1_000_000)}",
                     "description": "created by load test",
                     "quantity": rnd.randint(0, 100)},
                )
                if ok and data and "_id" in data:
                    my_ids.append(data["_id"])
            elif r < 0.75:
                await self._call("GET", f"{self.api}/{rnd.choice(my_ids)}", (200,))
            elif r < 0.85:
                await self._call("PUT", f"{self.api}/{rnd.choice(my_ids)}", (200,),
                                 {"quantity": rnd.randint(0, 100)})
            else:
                item_id = my_ids.pop(rnd.randrange(len(my_ids)))
                await self._call("DELETE", f"{self.api}/{item_id}", (204,))

        # Clean up whatever this user created (best effort).
        for item_id in my_ids:
            try:
                async with self.session.delete(f"{self.api}/{item_id}"):
                    pass
            except Exception:
                pass


# --------------------------------------------------------------------------- #
# Kubernetes observation
# --------------------------------------------------------------------------- #

class KubeWatcher:
    def __init__(self, namespace: str, deployment: str, hpa: str):
        self.ns, self.deployment, self.hpa = namespace, deployment, hpa
        self.min_replicas: int | None = None
        self.max_replicas: int | None = None

    async def _kubectl(self, *args: str) -> str | None:
        try:
            proc = await asyncio.create_subprocess_exec(
                "kubectl", "-n", self.ns, *args,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            )
            out, _ = await asyncio.wait_for(proc.communicate(), timeout=10)
            return out.decode().strip() if proc.returncode == 0 else None
        except Exception:
            return None

    async def snapshot(self) -> dict:
        deploy_raw, hpa_raw = await asyncio.gather(
            self._kubectl("get", "deployment", self.deployment, "-o", "json"),
            self._kubectl("get", "hpa", self.hpa, "-o", "json"),
        )
        snap = {"replicas": None, "ready": None, "desired": None, "cpu": None, "target": None}
        if deploy_raw:
            status = json.loads(deploy_raw).get("status", {})
            snap["replicas"] = status.get("replicas", 0)
            snap["ready"] = status.get("readyReplicas", 0)
        if hpa_raw:
            hpa = json.loads(hpa_raw)
            spec, status = hpa.get("spec", {}), hpa.get("status", {})
            self.min_replicas = spec.get("minReplicas", 1)
            self.max_replicas = spec.get("maxReplicas")
            snap["desired"] = status.get("desiredReplicas") or None
            for m in status.get("currentMetrics") or []:
                if m.get("type") == "Resource" and m["resource"]["name"] == "cpu":
                    snap["cpu"] = m["resource"]["current"].get("averageUtilization")
            for m in spec.get("metrics") or []:
                if m.get("type") == "Resource" and m["resource"]["name"] == "cpu":
                    snap["target"] = m["resource"]["target"].get("averageUtilization")
        return snap


# --------------------------------------------------------------------------- #
# Report
# --------------------------------------------------------------------------- #

def write_report(path: Path, data: dict) -> Path:
    template = (HERE / "report_template.html").read_text()
    # "</" is escaped so no string in the data can close the <script> tag.
    payload = json.dumps(data).replace("</", "<\\/")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(template.replace("/*__DATA__*/null", payload))
    return path


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #

def fmt(v, suffix=""):
    return "-" if v is None else f"{v}{suffix}"


async def run(args) -> int:
    use_kubectl = not args.no_kubectl
    if use_kubectl and not shutil.which("kubectl"):
        print("kubectl not found on PATH; continuing with --no-kubectl behaviour.")
        use_kubectl = False
    kube = KubeWatcher(args.namespace, args.deployment, args.hpa) if use_kubectl else None

    # Sanity check that the API is reachable before generating load.
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=5)) as s:
            async with s.get(args.url.rstrip("/") + "/api/items?limit=1") as r:
                if r.status != 200:
                    print(f"API check failed: HTTP {r.status} from {args.url}")
                    return 2
    except Exception as e:
        print(f"Cannot reach {args.url}: {e}")
        return 2

    started = datetime.now()
    initial = await kube.snapshot() if kube else {}
    initial_replicas = initial.get("replicas")
    cpu_target = initial.get("target")

    total_load = sum(s.duration for s in args.stages)
    print(f"Target: {args.url}")
    print("Stages: " + ", ".join(f"{s.users} users for {s.duration}s" for s in args.stages)
          + f"  (+ up to {args.cooldown}s idle cooldown)")
    if kube:
        print(f"Backend replicas at start: {fmt(initial_replicas)}, HPA min={fmt(kube.min_replicas)} "
              f"max={fmt(kube.max_replicas)} target CPU={fmt(cpu_target, '%')}")
    print()
    header = (f"{'time':>6} {'phase':<10} {'users':>5} {'req/s':>7} {'err':>5} {'capped':>6} "
              f"{'p50 ms':>7} {'p95 ms':>7} {'pods hit':>8}"
              + (f" {'replicas':>9} {'desired':>7} {'cpu':>9}" if kube else ""))
    print(header)
    print("-" * len(header))

    stage_bounds = []
    acc = 0
    for i, s in enumerate(args.stages):
        stage_bounds.append((acc, acc + s.duration, i, s))
        acc += s.duration

    def current_stage(elapsed):
        for start, end, i, s in stage_bounds:
            if start <= elapsed < end:
                return i, s
        return None, None

    rows = []  # one dict per reporting interval
    t0 = time.monotonic()
    async with LoadGenerator(args.url, args.timeout) as gen:
        last_stage = -1
        next_report = t0 + args.interval
        while True:
            elapsed = time.monotonic() - t0
            idx, stage = current_stage(elapsed)
            in_cooldown = stage is None
            if stage and idx != last_stage:
                gen.set_users(stage.users)
                last_stage = idx
            elif in_cooldown and gen.target_users:
                gen.set_users(0)

            now = time.monotonic()
            if now >= next_report:
                window = gen.take_window()
                span = now - (next_report - args.interval)
                next_report += args.interval
                snap = await kube.snapshot() if kube else {}
                row = {
                    "t": int(elapsed),
                    "phase": "cooldown" if in_cooldown else f"stage {idx + 1}",
                    "users": gen.target_users,
                    "rps": round(window.requests / span, 1) if span > 0 else 0,
                    "errors": window.errors,
                    "capped": window.capped,
                    "p50": round(pct(window.latencies, 0.50) * 1000),
                    "p95": round(pct(window.latencies, 0.95) * 1000),
                    "pods": len(window.pods),
                    **{k: snap.get(k) for k in ("replicas", "ready", "desired", "cpu")},
                }
                rows.append(row)
                line = (f"{row['t']:>5}s {row['phase']:<10} {row['users']:>5} {row['rps']:>7.1f} "
                        f"{row['errors']:>5} {row['capped']:>6} {row['p50']:>7} {row['p95']:>7} "
                        f"{row['pods']:>8}")
                if kube:
                    cpu = f"{row['cpu']}%/{snap['target']}%" if row["cpu"] is not None else "-"
                    line += (f" {fmt(row['ready'])}/{fmt(row['replicas']):<3}"
                             f"{'':>3} {fmt(row['desired']):>7} {cpu:>9}")
                print(line, flush=True)

                # End the cooldown early once we're back at the minimum.
                if in_cooldown:
                    back_to_min = (kube and row["replicas"] is not None
                                   and kube.min_replicas is not None
                                   and row["replicas"] <= kube.min_replicas)
                    if back_to_min or elapsed >= total_load + args.cooldown:
                        break
            await asyncio.sleep(0.1)

        print("\nStopping users and deleting the items they created...")
    totals = gen.totals
    duration = time.monotonic() - t0

    # ------------------------------------------------------------------ summary
    print()
    print("=" * 60)
    print("Summary")
    print("=" * 60)
    err_rate = 100 * totals.errors / totals.requests if totals.requests else 0
    print(f"Requests: {totals.requests}   errors: {totals.errors} ({err_rate:.2f}%)   "
          f"refused by item cap: {totals.capped}")
    print(f"Distinct backend pods that served requests: {len(totals.pods)}")
    if totals.error_types:
        print("Errors by type:")
        for k, v in sorted(totals.error_types.items(), key=lambda kv: -kv[1]):
            print(f"  {k}: {v}")

    replicas = [r["replicas"] for r in rows if r["replicas"] is not None]
    peak = max(replicas) if replicas else None
    final = replicas[-1] if replicas else None
    scaled_up = bool(replicas) and initial_replicas is not None and peak > initial_replicas
    scaled_down = bool(replicas) and final < peak

    changes = []
    prev = initial_replicas
    for r in rows:
        if r["replicas"] is not None and prev is not None and r["replicas"] != prev:
            changes.append({"t": r["t"], "from": prev, "to": r["replicas"], "phase": r["phase"],
                            "users": r["users"], "rps": r["rps"], "cpu": r["cpu"]})
            prev = r["replicas"]

    exit_code = 0
    if not kube:
        print("\n(kubectl disabled - judge scaling by the 'pods hit' column above)")
    elif not replicas or initial_replicas is None:
        print("\nCould not read replica counts from kubectl; check your kube context/namespace.")
        exit_code = 2
    else:
        print(f"\nBackend replicas: start={initial_replicas}  peak={peak}  end={final}")
        print("\nReplica timeline:")
        print(f"  {0:>5}s  {initial_replicas} replica(s)")
        for c in changes:
            arrow = "scaled UP" if c["to"] > c["from"] else "scaled DOWN"
            print(f"  {c['t']:>5}s  {c['from']} -> {c['to']}  {arrow:<11} ({c['phase']}, "
                  f"{c['users']} users, {c['rps']:.0f} req/s, cpu {fmt(c['cpu'], '%')})")
        print()
        print(f"[{'PASS' if scaled_up else 'FAIL'}] scaled up under load ({initial_replicas} -> {peak})")
        print(f"[{'PASS' if scaled_down else 'FAIL'}] scaled down when load dropped ({peak} -> {final})")
        if not scaled_up:
            print("  Hint: check `kubectl top pods -n crud-app` works (metrics-server), "
                  "or increase users in --stages.")
        elif not scaled_down:
            print("  Hint: scale-down waits for the HPA stabilization window; try a longer --cooldown.")
        exit_code = 0 if scaled_up and scaled_down else 1

    report_path = args.report or HERE / "results" / f"report-{started:%Y%m%d-%H%M%S}.html"
    write_report(report_path, {
        "meta": {
            "url": args.url, "started": f"{started:%Y-%m-%d %H:%M}",
            "stages": [{"duration": s.duration, "users": s.users} for s in args.stages],
            "min_replicas": kube.min_replicas if kube else None,
            "max_replicas": kube.max_replicas if kube else None,
            "cpu_target": cpu_target,
        },
        "rows": rows,
        "changes": changes,
        "summary": {
            "requests": totals.requests, "errors": totals.errors, "capped": totals.capped,
            "error_rate": err_rate, "error_types": totals.error_types,
            "avg_rps": totals.requests / duration if duration else 0,
            "initial": initial_replicas, "peak": peak, "final": final,
            "scaled_up": scaled_up, "scaled_down": scaled_down,
        },
    })
    print(f"\nReport: {report_path}")
    if args.open:
        webbrowser.open(report_path.resolve().as_uri())
    return exit_code


def main():
    args = parse_args()
    try:
        sys.exit(asyncio.run(run(args)))
    except KeyboardInterrupt:
        print("\nInterrupted.")
        sys.exit(130)


if __name__ == "__main__":
    main()
