"""Bounded native local runtime. No daemon, login service or shared-process kill."""
import argparse
import os
from pathlib import Path
import signal
import socket
import subprocess
import sys
import time

import httpx


class NativeSession:
    def __init__(self, run_dir, evidence_root):
        self.run_dir, self.evidence_root = Path(run_dir).resolve(), Path(evidence_root).resolve()
        self.children, self.logs = {}, []

    def start(self):
        for port in (8080, 9070, 9080, 5122):
            with socket.socket() as probe:
                if probe.connect_ex(("127.0.0.1", port)) == 0:
                    raise RuntimeError(f"Port {port} is in use; no existing service was stopped.")
        binary = os.environ.get("RESTATE_SERVER")
        if not binary:
            matches = list(Path(".tools").glob("restate-server-*/restate-server"))
            if not matches:
                raise RuntimeError("Run scripts/bootstrap_restate.py or set RESTATE_SERVER to pinned 1.7.13.")
            binary = str(matches[0].resolve())
        self.binary = binary
        self.version = subprocess.check_output([binary, "--version"], text=True).strip()
        if "1.7.13" not in self.version:
            raise RuntimeError("Recovery evidence requires pinned Restate 1.7.13.")
        self.run_dir.mkdir(parents=True, exist_ok=True)
        self.start_runtime()
        self.start_worker()
        self.wait(lambda: httpx.get("http://127.0.0.1:9080/discover", headers={"Accept": "application/vnd.restate.endpointmanifest.v3+json"}, timeout=1).status_code == 200)
        response = httpx.post("http://127.0.0.1:9070/deployments", json={"uri": "http://127.0.0.1:9080"}, timeout=30)
        response.raise_for_status()
        return self

    def launch(self, name, command, cwd):
        log = (self.run_dir / f"{name}-{len(self.logs)}.log").open("a")
        self.logs.append(log)
        self.children[name] = subprocess.Popen(command, cwd=cwd, stdout=log, stderr=subprocess.STDOUT, start_new_session=True,
            env={**os.environ, "SGR_PAPER_ROOT": str(self.evidence_root), "DO_NOT_TRACK": "1"})

    def start_runtime(self):
        state = self.run_dir / "runtime"
        state.mkdir(exist_ok=True)
        self.launch("runtime", [self.binary, "--bind-ip", "127.0.0.1", "--no-logo"], state)
        self.wait(lambda: httpx.get("http://127.0.0.1:9070/health", timeout=1).status_code == 200)

    def start_worker(self):
        self.launch("worker", [sys.executable, "-m", "hypercorn", "sgr.paper.workflows:app", "--bind", "127.0.0.1:9080"], Path.cwd())

    @staticmethod
    def wait(predicate, timeout=30):
        deadline = time.monotonic()+timeout
        while time.monotonic() < deadline:
            try:
                value = predicate()
                if value:
                    return value
            except (httpx.HTTPError, KeyError, OSError):
                pass
            time.sleep(.1)
        raise TimeoutError("Bounded runtime readiness wait expired; inspect ignored logs.")

    def stop(self, name, *, kill=False):
        child = self.children.get(name)
        if child and child.poll() is None:
            os.killpg(child.pid, signal.SIGKILL if kill else signal.SIGTERM)
            try:
                child.wait(timeout=5)
            except subprocess.TimeoutExpired:
                os.killpg(child.pid, signal.SIGKILL)
                child.wait(timeout=5)

    def close(self):
        self.stop("worker")
        self.stop("runtime")
        for log in self.logs:
            log.close()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--seconds", type=int, default=120)
    parser.add_argument("--state", default=".runs/paper-runtime")
    parser.add_argument("--data", default="data/polymarket")
    args = parser.parse_args()
    if not 1 <= args.seconds <= 3600:
        parser.error("Use a bounded 1..3600-second session.")
    session = NativeSession(args.state, args.data)
    try:
        session.start()
        print(f"Bounded paper runtime ready for {args.seconds}s; ingress 127.0.0.1:8080", flush=True)
        deadline = time.monotonic()+args.seconds
        while time.monotonic() < deadline and all(p.poll() is None for p in session.children.values()):
            time.sleep(.2)
    except KeyboardInterrupt:
        pass
    finally:
        session.close()


if __name__ == "__main__":
    main()
