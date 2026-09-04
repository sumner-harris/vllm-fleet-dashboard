#!/usr/bin/env python3
"""
vLLM Fleet Dashboard — aggregation server.

Polls a node agent (agent/spark_agent.py) on each machine, derives rates and
rolling history, and serves both the JSON API and the single-page dashboard.

    uvicorn server.app:app --host 0.0.0.0 --port 8080

Config: ./config.yaml (override with FLEET_CONFIG).
"""

from __future__ import annotations

import asyncio
import json
import os
import time
from collections import deque
from pathlib import Path
from typing import Any

import httpx
import yaml
from fastapi import FastAPI, Header, HTTPException
from fastapi.responses import FileResponse, JSONResponse

BASE_DIR = Path(__file__).resolve().parent
STATIC_DIR = BASE_DIR / "static"
CONFIG_PATH = Path(os.environ.get("FLEET_CONFIG", BASE_DIR.parent / "config.yaml"))

DEFAULTS = {
    "poll_interval_s": 10,
    "request_timeout_s": 5.0,
    "history_points": 720,          # 720 × 10s = 2 hours
    "agent_port": 9900,
    "title": "vLLM Fleet",
    "history_file": "",             # optional path; survives restarts when set
    "admin_token": "",              # when set, control endpoints require it
    "verify_lan": True,             # probe advertised URLs from the dashboard host
}


# --------------------------------------------------------------------------
# config
# --------------------------------------------------------------------------


def load_config() -> dict:
    if not CONFIG_PATH.exists():
        raise SystemExit(
            f"config not found at {CONFIG_PATH}\n"
            f"Copy config.example.yaml to config.yaml and fill in your 5 machines."
        )
    raw = yaml.safe_load(CONFIG_PATH.read_text()) or {}
    cfg = {**DEFAULTS, **{k: v for k, v in raw.items() if k != "nodes"}}
    nodes = []
    for i, node in enumerate(raw.get("nodes") or []):
        if not node.get("host"):
            raise SystemExit(f"nodes[{i}] is missing 'host'")
        nodes.append(
            {
                "name": node.get("name") or node["host"],
                "host": node["host"],
                "agent_port": int(node.get("agent_port", cfg["agent_port"])),
                "agent_token": node.get("agent_token") or raw.get("agent_token") or "",
                "public_host": node.get("public_host") or node["host"],
                "note": node.get("note") or "",
                # Used when a node has no agent: probe these vLLM ports directly.
                "vllm_ports": [int(p) for p in (node.get("vllm_ports") or [])],
                "agent": node.get("agent", True),
                # False for a machine that intentionally serves nothing right now —
                # it stays "online" instead of being flagged for a missing server.
                "expect_vllm": bool(node.get("expect_vllm", True)),
            }
        )
    if not nodes:
        raise SystemExit("config.yaml has no nodes")
    cfg["nodes"] = nodes
    return cfg


CONFIG = load_config()
ADMIN_TOKEN = os.environ.get("FLEET_ADMIN_TOKEN") or CONFIG.get("admin_token") or ""


def require_admin(token: str | None) -> None:
    """Viewing is open to anyone who can reach the host; changing state is not."""
    if not ADMIN_TOKEN:
        return  # no token configured — controls are open (single-user setup)
    if not token or token != ADMIN_TOKEN:
        raise HTTPException(403, "admin token required")


# --------------------------------------------------------------------------
# state
# --------------------------------------------------------------------------

STATE: dict[str, Any] = {
    "nodes": {},        # name -> latest node snapshot (derived)
    "history": {},      # name -> deque of samples
    "fleet_history": deque(maxlen=CONFIG["history_points"]),
    "prev_counters": {},  # "node|port" -> {ts, prompt, gen, e2e_sum, e2e_count, ...}
    "generated_at": 0.0,
    "poll_ms": 0,
}
for n in CONFIG["nodes"]:
    STATE["history"][n["name"]] = deque(maxlen=CONFIG["history_points"])


def restore_history() -> None:
    path = CONFIG.get("history_file")
    if not path:
        return
    f = Path(path)
    if not f.exists():
        return
    try:
        blob = json.loads(f.read_text())
    except Exception:
        return
    for name, points in (blob.get("nodes") or {}).items():
        if name in STATE["history"]:
            STATE["history"][name].extend(points[-CONFIG["history_points"]:])
    STATE["fleet_history"].extend((blob.get("fleet") or [])[-CONFIG["history_points"]:])


def persist_history() -> None:
    path = CONFIG.get("history_file")
    if not path:
        return
    try:
        Path(path).write_text(
            json.dumps(
                {
                    "nodes": {k: list(v) for k, v in STATE["history"].items()},
                    "fleet": list(STATE["fleet_history"]),
                }
            )
        )
    except Exception:
        pass


# --------------------------------------------------------------------------
# polling
# --------------------------------------------------------------------------


async def fetch_agent(client: httpx.AsyncClient, node: dict) -> dict:
    url = f"http://{node['host']}:{node['agent_port']}/stats"
    headers = {}
    if node["agent_token"]:
        headers["Authorization"] = f"Bearer {node['agent_token']}"
    resp = await client.get(url, headers=headers, timeout=CONFIG["request_timeout_s"])
    resp.raise_for_status()
    return resp.json()


async def fetch_direct(client: httpx.AsyncClient, node: dict) -> dict:
    """Agent-free fallback: talk to the configured vLLM ports over the network."""
    entries = []
    for port in node["vllm_ports"]:
        base = f"http://{node['host']}:{port}"
        entry = {"port": port, "reachable": False, "models": [], "error": None,
                 "container": None, "metrics": None, "metrics_error": None}
        try:
            r = await client.get(f"{base}/v1/models", timeout=CONFIG["request_timeout_s"])
            r.raise_for_status()
            entry["reachable"] = True
            for item in (r.json() or {}).get("data", []) or []:
                entry["models"].append({"id": item.get("id"),
                                        "max_model_len": item.get("max_model_len")})
        except Exception as exc:
            entry["error"] = str(exc)[:200]
        if entry["reachable"]:
            try:
                m = await client.get(f"{base}/metrics", timeout=CONFIG["request_timeout_s"])
                m.raise_for_status()
                import sys

                sys.path.insert(0, str(BASE_DIR.parent / "agent"))
                from spark_agent import parse_prometheus  # type: ignore

                entry["metrics"] = parse_prometheus(m.text)
            except Exception as exc:
                entry["metrics_error"] = str(exc)[:200]
        entries.append(entry)
    return {
        "hostname": node["name"],
        "gpus": [],
        "gpu_error": "no agent on this node — GPU stats unavailable",
        "containers": [],
        "docker_error": "no agent on this node",
        "vllm": entries,
        "agent_version": None,
        "collected_at": time.time(),
        "no_agent": True,
    }


def derive_endpoint(node: dict, entry: dict, now: float) -> dict:
    """Turn one vLLM endpoint's raw metrics into display-ready numbers."""
    metrics = entry.get("metrics") or {}
    key = f"{node['name']}|{entry['port']}"
    prev = STATE["prev_counters"].get(key)

    prompt = metrics.get("vllm:prompt_tokens_total")
    gen = metrics.get("vllm:generation_tokens_total")
    e2e_sum = metrics.get("vllm:e2e_request_latency_seconds_sum")
    e2e_count = metrics.get("vllm:e2e_request_latency_seconds_count")
    ttft_sum = metrics.get("vllm:time_to_first_token_seconds_sum")
    ttft_count = metrics.get("vllm:time_to_first_token_seconds_count")

    gen_tps = prompt_tps = None
    avg_latency_s = ttft_s = None
    if prev and now > prev["ts"]:
        dt = now - prev["ts"]
        if gen is not None and prev.get("gen") is not None and gen >= prev["gen"]:
            gen_tps = round((gen - prev["gen"]) / dt, 1)
        if prompt is not None and prev.get("prompt") is not None and prompt >= prev["prompt"]:
            prompt_tps = round((prompt - prev["prompt"]) / dt, 1)
        if (
            e2e_count is not None
            and prev.get("e2e_count") is not None
            and e2e_count > prev["e2e_count"]
        ):
            avg_latency_s = round(
                (e2e_sum - prev["e2e_sum"]) / (e2e_count - prev["e2e_count"]), 2
            )
        if (
            ttft_count is not None
            and prev.get("ttft_count") is not None
            and ttft_count > prev["ttft_count"]
        ):
            ttft_s = round((ttft_sum - prev["ttft_sum"]) / (ttft_count - prev["ttft_count"]), 3)

    if metrics:
        STATE["prev_counters"][key] = {
            "ts": now, "prompt": prompt, "gen": gen,
            "e2e_sum": e2e_sum, "e2e_count": e2e_count,
            "ttft_sum": ttft_sum, "ttft_count": ttft_count,
        }

    cache = metrics.get("vllm:gpu_cache_usage_perc")
    if cache is None:
        cache = metrics.get("vllm:kv_cache_usage_perc")

    models = [m.get("id") for m in (entry.get("models") or []) if m.get("id")]
    engine = entry.get("engine") or "vllm"
    port = entry["port"]

    # How you actually reach this server depends on what it is bound to, not on
    # which engine it is. A loopback-only server (Ollama under an IT policy that
    # forbids 0.0.0.0, for instance) is reached through a tunnel, so that — not a
    # LAN URL that would never work — is what the dashboard hands you.
    scope = entry.get("bind_scope") or "unknown"
    on_network = scope in ("all", "specific")
    host = node["public_host"]
    lan_url = f"http://{host}:{port}/v1" if on_network else None
    local_url = f"http://localhost:{port}/v1"
    base_url = lan_url or local_url
    tunnel_cmd = f"ssh -N -L {port}:localhost:{port} {host}"
    return {
        "port": entry["port"],
        "engine": engine,
        "engine_version": entry.get("engine_version"),
        "bind_scope": scope,
        "listen_addrs": entry.get("listen_addrs") or [],
        "on_network": on_network,
        "lan_url": lan_url,
        "local_url": local_url,
        "tunnel_cmd": tunnel_cmd,
        "sync_hint": f"NVIDIA Sync → Custom App → localhost:{port}",
        "lan_verified": None,
        # Ollama only: what is resident in VRAM vs pulled and ready on disk
        "loaded": entry.get("loaded") or [],
        "available": entry.get("available") or [],
        "loaded_count": entry.get("loaded_count"),
        "available_count": entry.get("available_count"),
        "vram_mib": entry.get("vram_mib"),
        "next_unload_s": entry.get("next_unload_s"),
        "reachable": bool(entry.get("reachable")),
        "error": entry.get("error"),
        "models": models,
        "model_detail": entry.get("models") or [],
        "base_url": base_url,
        "curl": (
            f"curl {base_url}/chat/completions -H 'Content-Type: application/json' "
            f"-d '{{\"model\":\"{models[0] if models else 'MODEL'}\","
            f"\"messages\":[{{\"role\":\"user\",\"content\":\"hi\"}}]}}'"
        ),
        "container": entry.get("container"),
        "container_id": entry.get("container_id"),
        "image": entry.get("image"),
        "health": entry.get("health"),
        "uptime_s": entry.get("uptime_s"),
        "restarts": entry.get("restarts"),
        "max_model_len": entry.get("max_model_len"),
        "tensor_parallel_size": entry.get("tensor_parallel_size"),
        "gpu_memory_utilization": entry.get("gpu_memory_utilization"),
        "requests_running": metrics.get("vllm:num_requests_running"),
        "requests_waiting": metrics.get("vllm:num_requests_waiting"),
        "kv_cache_pct": round(cache * 100, 1) if cache is not None else None,
        "preemptions": metrics.get("vllm:num_preemptions_total"),
        "gen_tps": gen_tps,
        "prompt_tps": prompt_tps,
        "avg_latency_s": avg_latency_s,
        "ttft_s": ttft_s,
        "metrics_error": entry.get("metrics_error"),
        "has_metrics": bool(metrics),
    }


def derive_node(node: dict, snapshot: dict | None, error: str | None, now: float) -> dict:
    if snapshot is None:
        return {
            "name": node["name"],
            "host": node["host"],
            "public_host": node["public_host"],
            "note": node["note"],
            "status": "offline",
            "status_reason": error or "agent unreachable",
            "endpoints": [],
            "gpus": [],
            "last_seen": STATE["nodes"].get(node["name"], {}).get("last_seen"),
        }

    endpoints = [
        derive_endpoint(node, e, now)
        for e in (snapshot.get("servers") or snapshot.get("vllm") or [])
    ]
    web_uis = [
        {
            "name": w.get("name"),
            "port": w.get("port"),
            "url": f"http://{node['public_host']}:{w.get('port')}",
            "uptime_s": w.get("uptime_s"),
            "restarts": w.get("restarts"),
        }
        for w in (snapshot.get("web_uis") or [])
        if w.get("port")
    ]
    gpus = snapshot.get("gpus") or []

    mem_total = sum(g.get("memory_total_mib") or 0 for g in gpus)
    mem_used = sum(g.get("memory_used_mib") or 0 for g in gpus)
    utils = [g.get("util_gpu_pct") for g in gpus if g.get("util_gpu_pct") is not None]

    no_agent = bool(snapshot.get("no_agent"))
    problems = []
    if snapshot.get("gpu_error") and not no_agent:
        problems.append(snapshot["gpu_error"])
    if snapshot.get("docker_error") and not no_agent:
        problems.append(snapshot["docker_error"])
    down = [e for e in endpoints if not e["reachable"]]
    if down:
        problems.append(
            "vLLM not answering on port " + ", ".join(str(e["port"]) for e in down)
        )
    if not endpoints and node["expect_vllm"]:
        problems.append("no vLLM server found on this node")

    status = "online"
    if (not endpoints and node["expect_vllm"]) or down or (
        snapshot.get("gpu_error") and not no_agent
    ):
        status = "degraded"
    if endpoints and all(not e["reachable"] for e in endpoints):
        status = "degraded"

    return {
        "name": node["name"],
        "host": node["host"],
        "public_host": node["public_host"],
        "note": node["note"],
        "idle_ok": not node["expect_vllm"],
        "status": status,
        "status_reason": "; ".join(problems) if problems else "",
        "hostname": snapshot.get("hostname"),
        "board_model": snapshot.get("board_model"),
        "arch": snapshot.get("arch"),
        "kernel": snapshot.get("kernel"),
        "agent_version": snapshot.get("agent_version"),
        "no_agent": snapshot.get("no_agent", False),
        "uptime_s": snapshot.get("uptime_s"),
        "load_avg": snapshot.get("load_avg"),
        "cpu_count": snapshot.get("cpu_count"),
        "disk_root": snapshot.get("disk_root"),
        "host_memory": snapshot.get("host_memory"),
        "unified_memory": snapshot.get("unified_memory", False),
        "gpus": gpus,
        "gpu_mem_total_mib": mem_total or None,
        "gpu_mem_used_mib": mem_used or None,
        "gpu_mem_used_pct": round(mem_used / mem_total * 100, 1) if mem_total else None,
        "gpu_util_pct": round(sum(utils) / len(utils), 1) if utils else None,
        "containers": snapshot.get("containers") or [],
        "endpoints": endpoints,
        "web_uis": web_uis,
        "gen_tps": round(
            sum(e["gen_tps"] or 0 for e in endpoints if e["engine"] == "vllm"), 1
        ) if endpoints else 0.0,
        "engines": sorted({e["engine"] for e in endpoints}),
        "requests_running": sum(e["requests_running"] or 0 for e in endpoints),
        "requests_waiting": sum(e["requests_waiting"] or 0 for e in endpoints),
        "collected_at": snapshot.get("collected_at"),
        "last_seen": now,
    }


async def verify_lan(client: httpx.AsyncClient, nodes: list[dict]) -> None:
    """Bind scope says what the process intends; this says what actually works
    from where the dashboard sits, so a firewall shows up too."""

    async def probe(host: str, e: dict) -> None:
        path = "/api/version" if e["engine"] == "ollama" else "/v1/models"
        try:
            r = await client.get(f"http://{host}:{e['port']}{path}", timeout=2.5)
            e["lan_verified"] = r.status_code < 500
        except Exception:
            e["lan_verified"] = False

    tasks = []
    for n in nodes:
        for e in n["endpoints"]:
            if not e["reachable"]:
                continue
            if not e["on_network"]:
                e["lan_verified"] = False   # loopback: known, no probe needed
                continue
            tasks.append(probe(n["public_host"], e))
    if tasks:
        await asyncio.gather(*tasks)


async def poll_once() -> None:
    now = time.time()
    t0 = time.perf_counter()
    async with httpx.AsyncClient(follow_redirects=False) as client:

        async def one(node: dict):
            try:
                if node["agent"]:
                    return node, await fetch_agent(client, node), None
                return node, await fetch_direct(client, node), None
            except Exception as exc:
                if node["vllm_ports"]:
                    try:  # agent down, but we still know the ports — degrade gracefully
                        snap = await fetch_direct(client, node)
                        snap["gpu_error"] = f"agent unreachable ({type(exc).__name__})"
                        return node, snap, None
                    except Exception:
                        pass
                return node, None, f"{type(exc).__name__}: {exc}"[:200]

        results = await asyncio.gather(*(one(n) for n in CONFIG["nodes"]))

        derived_nodes = [derive_node(node, snap, err, now) for node, snap, err in results]
        if CONFIG["verify_lan"]:
            try:
                await verify_lan(client, derived_nodes)
            except Exception as exc:
                print(f"[poll] lan verification skipped: {exc}")

    fleet_tps = 0.0
    for node, derived in zip(CONFIG["nodes"], derived_nodes):
        STATE["nodes"][node["name"]] = derived
        fleet_tps += derived.get("gen_tps") or 0.0
        STATE["history"][node["name"]].append(
            {
                "t": int(now),
                "u": derived.get("gpu_util_pct"),
                "m": derived.get("gpu_mem_used_pct"),
                "g": derived.get("gen_tps"),
                "r": derived.get("requests_running"),
                "ok": derived["status"] != "offline",
            }
        )

    online = [n for n in STATE["nodes"].values() if n["status"] != "offline"]
    utils = [n["gpu_util_pct"] for n in online if n.get("gpu_util_pct") is not None]
    STATE["fleet_history"].append(
        {
            "t": int(now),
            "u": round(sum(utils) / len(utils), 1) if utils else None,
            "g": round(fleet_tps, 1),
            "n": len(online),
        }
    )
    STATE["generated_at"] = now
    STATE["poll_ms"] = int((time.perf_counter() - t0) * 1000)


def fleet_summary() -> dict:
    nodes = list(STATE["nodes"].values())
    online = [n for n in nodes if n["status"] != "offline"]
    gpus = [g for n in online for g in n.get("gpus") or []]
    endpoints = [e for n in online for e in n["endpoints"]]
    live_endpoints = [e for e in endpoints if e["reachable"]]
    models = sorted({m for e in live_endpoints for m in e["models"]})
    available = sorted({
        m.get("id") for e in live_endpoints for m in (e.get("available") or []) if m.get("id")
    })
    ollama_eps = [e for e in live_endpoints if e["engine"] == "ollama"]
    mem_total = sum(g.get("memory_total_mib") or 0 for g in gpus)
    mem_used = sum(g.get("memory_used_mib") or 0 for g in gpus)
    utils = [g.get("util_gpu_pct") for g in gpus if g.get("util_gpu_pct") is not None]
    return {
        "nodes_total": len(nodes),
        "nodes_online": len(online),
        "nodes_degraded": len([n for n in nodes if n["status"] == "degraded"]),
        "nodes_offline": len([n for n in nodes if n["status"] == "offline"]),
        "gpus": len(gpus),
        "endpoints_total": len(endpoints),
        "endpoints_live": len(live_endpoints),
        "endpoints_networked": len([e for e in live_endpoints if e["on_network"]]),
        "endpoints_local_only": len([e for e in live_endpoints if not e["on_network"]]),
        "endpoints_unverified": len(
            [e for e in live_endpoints if e["on_network"] and e["lan_verified"] is False]
        ),
        "models_loaded": len(models),
        "model_list": models,
        "models_available": len(available),
        "available_list": available,
        "ollama_endpoints": len(ollama_eps),
        "ollama_vram_mib": round(sum(e.get("vram_mib") or 0 for e in ollama_eps), 1) or None,
        "web_uis": [w for n in online for w in (n.get("web_uis") or [])],
        "gpu_mem_total_mib": mem_total or None,
        "gpu_mem_used_mib": mem_used or None,
        "gpu_mem_used_pct": round(mem_used / mem_total * 100, 1) if mem_total else None,
        "gpu_util_pct": round(sum(utils) / len(utils), 1) if utils else None,
        "gen_tps": round(sum(n.get("gen_tps") or 0 for n in online), 1),
        "requests_running": sum(n.get("requests_running") or 0 for n in online),
        "requests_waiting": sum(n.get("requests_waiting") or 0 for n in online),
    }


# --------------------------------------------------------------------------
# app
# --------------------------------------------------------------------------

app = FastAPI(title="vLLM Fleet Dashboard", docs_url="/api/docs", redoc_url=None)
_task: asyncio.Task | None = None


async def poll_loop() -> None:
    counter = 0
    while True:
        try:
            await poll_once()
        except Exception as exc:  # never let the loop die
            print(f"[poll] error: {exc}")
        counter += 1
        if counter % 6 == 0:
            persist_history()
        await asyncio.sleep(CONFIG["poll_interval_s"])


@app.on_event("startup")
async def startup() -> None:
    global _task
    restore_history()
    _task = asyncio.create_task(poll_loop())


@app.on_event("shutdown")
async def shutdown() -> None:
    if _task:
        _task.cancel()
    persist_history()


@app.get("/api/fleet")
async def api_fleet(history: bool = True):
    order = [n["name"] for n in CONFIG["nodes"]]
    payload = {
        "title": CONFIG["title"],
        "generated_at": STATE["generated_at"],
        "poll_interval_s": CONFIG["poll_interval_s"],
        "poll_ms": STATE["poll_ms"],
        "fleet": fleet_summary(),
        "nodes": [STATE["nodes"][n] for n in order if n in STATE["nodes"]],
    }
    if history:
        payload["history"] = {k: list(v) for k, v in STATE["history"].items()}
        payload["fleet_history"] = list(STATE["fleet_history"])
    return JSONResponse(payload)


@app.get("/api/node/{name}")
async def api_node(name: str):
    if name not in STATE["nodes"]:
        raise HTTPException(404, f"no node named {name!r}")
    return JSONResponse(
        {"node": STATE["nodes"][name], "history": list(STATE["history"].get(name, []))}
    )


@app.post("/api/refresh")
async def api_refresh(x_admin_token: str | None = Header(default=None)):
    require_admin(x_admin_token)
    await poll_once()
    return {"ok": True, "generated_at": STATE["generated_at"]}


@app.get("/api/whoami")
async def api_whoami(x_admin_token: str | None = Header(default=None)):
    """Lets the page decide whether to show admin controls. Never leaks the token."""
    return {
        "admin_required": bool(ADMIN_TOKEN),
        "admin": (not ADMIN_TOKEN) or x_admin_token == ADMIN_TOKEN,
    }


@app.get("/healthz")
async def healthz():
    stale = time.time() - STATE["generated_at"] if STATE["generated_at"] else None
    return {"ok": True, "last_poll_age_s": round(stale, 1) if stale else None}


@app.get("/app.js")
async def appjs():
    return FileResponse(STATIC_DIR / "app.js", media_type="application/javascript")


@app.get("/")
async def index():
    return FileResponse(STATIC_DIR / "index.html", media_type="text/html")
