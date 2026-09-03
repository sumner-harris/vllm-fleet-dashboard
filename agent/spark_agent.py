#!/usr/bin/env python3
"""
vllm-fleet-agent — a dependency-free status agent for DGX Spark / HP ZGX nodes
running vLLM in Docker.

Exposes JSON over HTTP:
    GET /health   -> {"ok": true, ...}
    GET /stats    -> full node snapshot (GPU, host, docker, vLLM endpoints)

Only the Python standard library is used, so it runs on a bare node with no
pip install. Optional bearer-token auth via FLEET_AGENT_TOKEN.

Usage:
    python3 spark_agent.py --port 9900
Env:
    FLEET_AGENT_TOKEN   shared secret; if set, requests must send
                        "Authorization: Bearer <token>"
    FLEET_AGENT_BIND    bind address (default 0.0.0.0)
    FLEET_AGENT_PORT    port (default 9900)
    FLEET_VLLM_PORTS    comma-separated extra ports to probe for vLLM
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import re
import shutil
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

AGENT_VERSION = "1.1.0"
CACHE_TTL = 2.0  # seconds; collapses dashboard polls into one collection

# --------------------------------------------------------------------------
# small helpers
# --------------------------------------------------------------------------


def run(cmd: list[str], timeout: float = 6.0) -> tuple[int, str, str]:
    """Run a command, never raise."""
    try:
        p = subprocess.run(
            cmd, capture_output=True, text=True, timeout=timeout, check=False
        )
        return p.returncode, p.stdout, p.stderr
    except FileNotFoundError:
        return 127, "", f"{cmd[0]}: not found"
    except subprocess.TimeoutExpired:
        return 124, "", f"{cmd[0]}: timed out after {timeout}s"
    except Exception as exc:  # pragma: no cover
        return 1, "", f"{cmd[0]}: {exc}"


def to_num(value, default=None):
    if value is None:
        return default
    text = str(value).strip()
    if text in ("", "N/A", "[N/A]", "[Not Supported]", "Insufficient Permissions"):
        return default
    try:
        num = float(text)
    except ValueError:
        return default
    return int(num) if num.is_integer() else round(num, 2)


def http_json(url: str, timeout: float = 2.5):
    req = urllib.request.Request(url, headers={"Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8", "replace"))


# --------------------------------------------------------------------------
# GPU collection
# --------------------------------------------------------------------------

NVSMI_FIELDS = [
    "index",
    "uuid",
    "name",
    "memory.total",
    "memory.used",
    "memory.free",
    "utilization.gpu",
    "utilization.memory",
    "temperature.gpu",
    "power.draw",
    "power.limit",
    "clocks.sm",
    "persistence_mode",
]


def gpus_from_nvidia_smi() -> tuple[list[dict], str | None]:
    if not shutil.which("nvidia-smi"):
        return [], "nvidia-smi not on PATH"
    code, out, err = run(
        [
            "nvidia-smi",
            f"--query-gpu={','.join(NVSMI_FIELDS)}",
            "--format=csv,noheader,nounits",
        ]
    )
    if code != 0:
        return [], (err or out).strip()[:300] or "nvidia-smi failed"

    gpus = []
    for line in out.strip().splitlines():
        cells = [c.strip() for c in line.split(",")]
        if len(cells) < len(NVSMI_FIELDS):
            continue
        row = dict(zip(NVSMI_FIELDS, cells))
        total = to_num(row["memory.total"])
        used = to_num(row["memory.used"])
        gpus.append(
            {
                "index": to_num(row["index"], 0),
                "uuid": row["uuid"],
                "name": row["name"],
                "memory_total_mib": total,
                "memory_used_mib": used,
                "memory_free_mib": to_num(row["memory.free"]),
                "memory_used_pct": round(used / total * 100, 1)
                if total and used is not None
                else None,
                "util_gpu_pct": to_num(row["utilization.gpu"]),
                "util_mem_pct": to_num(row["utilization.memory"]),
                "temp_c": to_num(row["temperature.gpu"]),
                "power_w": to_num(row["power.draw"]),
                "power_limit_w": to_num(row["power.limit"]),
                "clock_sm_mhz": to_num(row["clocks.sm"]),
                "source": "nvidia-smi",
            }
        )
    return gpus, None


def gpus_from_tegra_sysfs() -> tuple[list[dict], str | None]:
    """Fallback for Tegra/GB10-class boards where nvidia-smi is unavailable.

    GPU load lives in sysfs; memory is unified with host RAM, so memory
    figures are filled in later from /proc/meminfo by collect_gpus().
    """
    load_paths = [
        "/sys/devices/platform/gpu.0/load",
        "/sys/devices/gpu.0/load",
        "/sys/class/devfreq/17000000.gpu/device/load",
    ]
    for path in load_paths:
        try:
            with open(path) as fh:
                raw = fh.read().strip()
        except OSError:
            continue
        load = to_num(raw)
        if load is None:
            continue
        # sysfs reports per-mille (0-1000) on Tegra
        util = round(load / 10.0, 1) if load > 100 else float(load)
        name = "Integrated NVIDIA GPU"
        try:
            with open("/proc/device-tree/model") as fh:
                name = fh.read().strip("\x00").strip() or name
        except OSError:
            pass
        return (
            [
                {
                    "index": 0,
                    "uuid": None,
                    "name": name,
                    "util_gpu_pct": util,
                    "source": "tegra-sysfs",
                }
            ],
            None,
        )
    return [], "no NVIDIA GPU interface found (nvidia-smi and sysfs both absent)"


def host_memory() -> dict:
    info = {}
    try:
        with open("/proc/meminfo") as fh:
            for line in fh:
                key, _, rest = line.partition(":")
                kb = to_num(rest.strip().split(" ")[0])
                if kb is not None:
                    info[key] = kb / 1024.0  # MiB
    except OSError:
        return {}
    total = info.get("MemTotal")
    available = info.get("MemAvailable")
    if total is None:
        return {}
    used = total - available if available is not None else None
    return {
        "total_mib": round(total, 1),
        "available_mib": round(available, 1) if available is not None else None,
        "used_mib": round(used, 1) if used is not None else None,
        "used_pct": round(used / total * 100, 1) if used is not None else None,
    }


UNIFIED_HINTS = ("gb10", "spark", "orin", "thor", "grace blackwell", "igpu")


def collect_gpus() -> dict:
    gpus, error = gpus_from_nvidia_smi()
    if not gpus:
        gpus, fallback_error = gpus_from_tegra_sysfs()
        error = error if gpus else (fallback_error or error)

    hmem = host_memory()
    model = board_model()
    unified = any(h in (model or "").lower() for h in UNIFIED_HINTS) or any(
        h in (g.get("name") or "").lower() for h in UNIFIED_HINTS for g in gpus
    )

    for gpu in gpus:
        # On unified-memory boards (DGX Spark GB10 and friends) the GPU shares
        # one physical pool with the CPU. Report the host pool so the number
        # means something, and label it so the dashboard can say so.
        if unified and hmem:
            if not gpu.get("memory_total_mib"):
                gpu["memory_total_mib"] = hmem.get("total_mib")
                gpu["memory_used_mib"] = hmem.get("used_mib")
                gpu["memory_free_mib"] = hmem.get("available_mib")
                gpu["memory_used_pct"] = hmem.get("used_pct")
                gpu["memory_source"] = "host-unified"
            else:
                gpu["memory_source"] = gpu.get("source")
            gpu["unified_memory"] = True
        else:
            gpu["unified_memory"] = False
            gpu["memory_source"] = gpu.get("source")

    return {"gpus": gpus, "gpu_error": error, "host_memory": hmem, "unified": unified}


def board_model() -> str | None:
    for path in ("/proc/device-tree/model", "/sys/class/dmi/id/product_name"):
        try:
            with open(path) as fh:
                text = fh.read().strip("\x00").strip()
            if text:
                return text
        except OSError:
            continue
    return None


# --------------------------------------------------------------------------
# Docker collection
# --------------------------------------------------------------------------

VLLM_HINTS = ("vllm", "openai.api_server", "vllm-openai")
INSPECT_FMT = (
    "{{json (dict "
    '"id" .Id '
    '"name" .Name '
    '"image" .Config.Image '
    '"cmd" .Config.Cmd '
    '"entrypoint" .Config.Entrypoint '
    '"args" .Args '
    '"env" .Config.Env '
    '"labels" .Config.Labels '
    '"ports" .NetworkSettings.Ports '
    '"exposed" .Config.ExposedPorts '
    '"netmode" .HostConfig.NetworkMode '
    '"state" .State.Status '
    '"health" .State.Health '
    '"started" .State.StartedAt '
    '"restarts" .RestartCount '
    ") }}"
)


def docker_available() -> bool:
    return bool(shutil.which("docker"))


def collect_containers() -> tuple[list[dict], str | None]:
    if not docker_available():
        return [], "docker not on PATH"
    code, out, err = run(["docker", "ps", "-a", "--format", "{{.ID}}"], timeout=8.0)
    if code != 0:
        return [], (err or out).strip()[:300] or "docker ps failed"
    ids = [line.strip() for line in out.splitlines() if line.strip()]
    if not ids:
        return [], None

    code, out, err = run(
        ["docker", "inspect", "--format", INSPECT_FMT, *ids], timeout=15.0
    )
    if code != 0:
        return [], (err or out).strip()[:300] or "docker inspect failed"

    containers = []
    for line in out.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            raw = json.loads(line)
        except json.JSONDecodeError:
            continue
        containers.append(normalize_container(raw))
    return containers, None


def normalize_container(raw: dict) -> dict:
    argv = []
    for key in ("entrypoint", "cmd", "args"):
        val = raw.get(key)
        if isinstance(val, list):
            argv.extend(str(v) for v in val)
    blob = " ".join(argv).lower()
    image = (raw.get("image") or "").lower()
    env_list = raw.get("env") or []
    env = {}
    for item in env_list:
        key, _, value = str(item).partition("=")
        env[key] = value

    is_vllm = any(h in blob for h in VLLM_HINTS) or any(h in image for h in VLLM_HINTS)
    if not is_vllm:
        is_vllm = any(k.startswith("VLLM_") for k in env)

    ports = []
    port_map = raw.get("ports") or {}
    if isinstance(port_map, dict):
        for container_port, bindings in port_map.items():
            cport = to_num(str(container_port).split("/")[0])
            for binding in bindings or []:
                hport = to_num(binding.get("HostPort"))
                if hport:
                    ports.append(
                        {
                            "host_ip": binding.get("HostIp") or "0.0.0.0",
                            "host_port": hport,
                            "container_port": cport,
                        }
                    )
    # --network host publishes nothing; recover the port from argv/env.
    arg_port = argv_flag(argv, "--port") or env.get("VLLM_PORT") or env.get("PORT")
    if not ports and to_num(arg_port):
        ports.append(
            {"host_ip": "0.0.0.0", "host_port": to_num(arg_port), "container_port": to_num(arg_port)}
        )
    if not ports and is_vllm and str(raw.get("netmode", "")).startswith("host"):
        ports.append({"host_ip": "0.0.0.0", "host_port": 8000, "container_port": 8000})

    health = raw.get("health") or {}
    started = raw.get("started")
    return {
        "id": (raw.get("id") or "")[:12],
        "name": (raw.get("name") or "").lstrip("/"),
        "image": raw.get("image"),
        "state": raw.get("state"),
        "health": (health or {}).get("Status"),
        "started_at": started,
        "uptime_s": iso_age(started) if raw.get("state") == "running" else None,
        "restarts": to_num(raw.get("restarts"), 0),
        "network_mode": raw.get("netmode"),
        "ports": ports,
        "is_vllm": is_vllm,
        "model_arg": argv_flag(argv, "--model") or env.get("MODEL"),
        "served_model_name": argv_flag(argv, "--served-model-name"),
        "tensor_parallel_size": to_num(argv_flag(argv, "--tensor-parallel-size")),
        "gpu_memory_utilization": to_num(argv_flag(argv, "--gpu-memory-utilization")),
        "max_model_len": to_num(argv_flag(argv, "--max-model-len")),
    }


def argv_flag(argv: list[str], flag: str) -> str | None:
    for i, token in enumerate(argv):
        if token == flag and i + 1 < len(argv):
            return argv[i + 1]
        if token.startswith(flag + "="):
            return token.split("=", 1)[1]
    return None


def iso_age(stamp: str | None) -> int | None:
    if not stamp:
        return None
    text = str(stamp).strip()
    if text.startswith("0001-01-01"):
        return None
    text = re.sub(r"\.(\d{6})\d+", r".\1", text).replace("Z", "+00:00")
    try:
        import datetime as _dt

        started = _dt.datetime.fromisoformat(text)
        now = _dt.datetime.now(_dt.timezone.utc)
        return max(0, int((now - started).total_seconds()))
    except Exception:
        return None


# --------------------------------------------------------------------------
# vLLM endpoint probing (from the node itself, so host-network and
# loopback-bound servers are still found)
# --------------------------------------------------------------------------


def candidate_ports(containers: list[dict]) -> list[tuple[int, dict | None]]:
    seen: dict[int, dict | None] = {}
    for c in containers:
        if not c["is_vllm"] or c["state"] != "running":
            continue
        for p in c["ports"]:
            port = p.get("host_port")
            if port and port not in seen:
                seen[port] = c
    for raw in (os.environ.get("FLEET_VLLM_PORTS") or "").split(","):
        port = to_num(raw.strip())
        if port and port not in seen:
            seen[port] = None
    return sorted(seen.items())


def probe_vllm(port: int, container: dict | None) -> dict:
    base = f"http://127.0.0.1:{port}"
    entry = {
        "port": port,
        "container": container["name"] if container else None,
        "container_id": container["id"] if container else None,
        "image": container["image"] if container else None,
        "uptime_s": container["uptime_s"] if container else None,
        "restarts": container["restarts"] if container else None,
        "health": container["health"] if container else None,
        "tensor_parallel_size": container.get("tensor_parallel_size") if container else None,
        "max_model_len": container.get("max_model_len") if container else None,
        "gpu_memory_utilization": container.get("gpu_memory_utilization") if container else None,
        "reachable": False,
        "models": [],
        "error": None,
    }
    try:
        payload = http_json(f"{base}/v1/models")
        entry["reachable"] = True
        for item in (payload or {}).get("data", []) or []:
            entry["models"].append(
                {
                    "id": item.get("id"),
                    "max_model_len": item.get("max_model_len"),
                    "owned_by": item.get("owned_by"),
                }
            )
    except urllib.error.HTTPError as exc:
        entry["error"] = f"HTTP {exc.code} on /v1/models"
    except Exception as exc:
        entry["error"] = str(exc)[:200]

    if entry["reachable"]:
        metrics, metrics_error = fetch_metrics(port)
        entry["metrics"] = metrics
        entry["metrics_error"] = metrics_error
        if not entry["models"] and metrics and metrics.get("_model_names"):
            for name in metrics["_model_names"]:
                entry["models"].append({"id": name, "from": "metrics"})
    else:
        entry["metrics"] = None
        entry["metrics_error"] = "server unreachable"

    if not entry["models"] and container:
        fallback = container.get("served_model_name") or container.get("model_arg")
        if fallback:
            entry["models"].append({"id": fallback, "from": "container-args"})
    return entry


def collect_vllm(containers: list[dict]) -> list[dict]:
    cands = candidate_ports(containers)
    if not cands:
        return []
    with ThreadPoolExecutor(max_workers=min(8, len(cands))) as pool:
        return list(pool.map(lambda item: probe_vllm(item[0], item[1]), cands))


# --------------------------------------------------------------------------
# Prometheus /metrics scraping (done on the node, so vLLM servers bound to
# 127.0.0.1 are still covered)
# --------------------------------------------------------------------------

WANTED_METRICS = {
    "vllm:num_requests_running",
    "vllm:num_requests_waiting",
    "vllm:num_requests_swapped",
    "vllm:gpu_cache_usage_perc",
    "vllm:kv_cache_usage_perc",
    "vllm:cpu_cache_usage_perc",
    "vllm:prompt_tokens_total",
    "vllm:generation_tokens_total",
    "vllm:num_preemptions_total",
    "vllm:request_success_total",
    "vllm:e2e_request_latency_seconds_sum",
    "vllm:e2e_request_latency_seconds_count",
    "vllm:time_to_first_token_seconds_sum",
    "vllm:time_to_first_token_seconds_count",
    "vllm:time_per_output_token_seconds_sum",
    "vllm:time_per_output_token_seconds_count",
}

_SAMPLE_RE = re.compile(r"^(?P<name>[a-zA-Z_:][a-zA-Z0-9_:]*)(?P<labels>\{[^}]*\})?\s+(?P<value>[^\s]+)")


def parse_prometheus(text: str) -> dict:
    """Sum each wanted metric across label sets; also note the model_name label."""
    totals: dict[str, float] = {}
    model_names: list[str] = []
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        m = _SAMPLE_RE.match(line)
        if not m:
            continue
        name = m.group("name")
        if name not in WANTED_METRICS:
            continue
        try:
            value = float(m.group("value"))
        except ValueError:
            continue
        if value != value:  # NaN
            continue
        totals[name] = totals.get(name, 0.0) + value
        labels = m.group("labels") or ""
        lm = re.search(r'model_name="([^"]*)"', labels)
        if lm and lm.group(1) and lm.group(1) not in model_names:
            model_names.append(lm.group(1))
    if model_names:
        totals["_model_names"] = model_names  # type: ignore[assignment]
    return totals


def fetch_metrics(port: int, timeout: float = 2.5) -> tuple[dict | None, str | None]:
    url = f"http://127.0.0.1:{port}/metrics"
    try:
        req = urllib.request.Request(url, headers={"Accept": "text/plain"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read().decode("utf-8", "replace")
        return parse_prometheus(body), None
    except Exception as exc:
        return None, str(exc)[:200]


# --------------------------------------------------------------------------
# snapshot
# --------------------------------------------------------------------------


def load_avg() -> list[float] | None:
    try:
        return [round(v, 2) for v in os.getloadavg()]
    except OSError:
        return None


def disk_root() -> dict | None:
    try:
        usage = shutil.disk_usage("/")
    except OSError:
        return None
    return {
        "total_gib": round(usage.total / 1024**3, 1),
        "used_gib": round(usage.used / 1024**3, 1),
        "used_pct": round(usage.used / usage.total * 100, 1),
    }


def build_snapshot() -> dict:
    t0 = time.time()
    gpu_info = collect_gpus()
    containers, docker_error = collect_containers()
    vllm = collect_vllm(containers)
    return {
        "agent_version": AGENT_VERSION,
        "collected_at": time.time(),
        "collect_ms": int((time.time() - t0) * 1000),
        "hostname": socket.gethostname(),
        "board_model": board_model(),
        "kernel": platform.release(),
        "arch": platform.machine(),
        "uptime_s": host_uptime(),
        "load_avg": load_avg(),
        "cpu_count": os.cpu_count(),
        "disk_root": disk_root(),
        "host_memory": gpu_info["host_memory"],
        "unified_memory": gpu_info["unified"],
        "gpus": gpu_info["gpus"],
        "gpu_error": gpu_info["gpu_error"],
        "docker_error": docker_error,
        "containers": containers,
        "vllm": vllm,
    }


def host_uptime() -> int | None:
    try:
        with open("/proc/uptime") as fh:
            return int(float(fh.read().split()[0]))
    except (OSError, ValueError):
        return None


_cache: dict = {"at": 0.0, "data": None}


def cached_snapshot() -> dict:
    now = time.time()
    if _cache["data"] is not None and now - _cache["at"] < CACHE_TTL:
        return _cache["data"]
    data = build_snapshot()
    _cache.update(at=now, data=data)
    return data


# --------------------------------------------------------------------------
# HTTP
# --------------------------------------------------------------------------


class Handler(BaseHTTPRequestHandler):
    server_version = f"vllm-fleet-agent/{AGENT_VERSION}"
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):  # keep journald quiet
        if os.environ.get("FLEET_AGENT_VERBOSE"):
            sys.stderr.write("%s - %s\n" % (self.address_string(), fmt % args))

    def _send(self, code: int, payload: dict):
        body = json.dumps(payload, default=str).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "Authorization")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _authorized(self) -> bool:
        token = os.environ.get("FLEET_AGENT_TOKEN")
        if not token:
            return True
        header = self.headers.get("Authorization", "")
        return header.strip() == f"Bearer {token}"

    def do_OPTIONS(self):
        self._send(204, {})

    def do_GET(self):
        path = self.path.split("?", 1)[0].rstrip("/") or "/"
        if path == "/health":
            self._send(200, {"ok": True, "agent_version": AGENT_VERSION,
                             "hostname": socket.gethostname()})
            return
        if not self._authorized():
            self._send(401, {"error": "unauthorized"})
            return
        if path in ("/", "/stats"):
            try:
                self._send(200, cached_snapshot())
            except Exception as exc:
                self._send(500, {"error": f"collection failed: {exc}"})
            return
        self._send(404, {"error": "not found", "paths": ["/health", "/stats"]})


def main() -> int:
    ap = argparse.ArgumentParser(description="vLLM fleet node agent")
    ap.add_argument("--bind", default=os.environ.get("FLEET_AGENT_BIND", "0.0.0.0"))
    ap.add_argument("--port", type=int,
                    default=int(os.environ.get("FLEET_AGENT_PORT", "9900")))
    ap.add_argument("--once", action="store_true",
                    help="print one snapshot as JSON and exit (for testing)")
    args = ap.parse_args()

    if args.once:
        print(json.dumps(build_snapshot(), indent=2, default=str))
        return 0

    httpd = ThreadingHTTPServer((args.bind, args.port), Handler)
    httpd.daemon_threads = True
    sys.stderr.write(
        f"vllm-fleet-agent {AGENT_VERSION} listening on {args.bind}:{args.port} "
        f"(auth: {'on' if os.environ.get('FLEET_AGENT_TOKEN') else 'off'})\n"
    )
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
