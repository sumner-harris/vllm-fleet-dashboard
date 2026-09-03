#!/usr/bin/env python3
"""Simulate a 5-machine fleet so the dashboard can be exercised without hardware.

    python3 tools/fake_fleet.py          # agents on 19901..19905 (19903 is dead)

Node 4 has a vLLM container that is down, node 3 never starts (offline).
Counters advance in real time, so tok/s and latency are real derivations.
"""
import json, math, random, threading, time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

T0 = time.time()
COUNTERS = {}

OLLAMA_TAGS = [
    ("llama3.3:70b", 42520, "70.6B", "Q4_K_M"),
    ("qwen2.5-coder:32b", 19850, "32.8B", "Q4_K_M"),
    ("gemma3:27b", 17400, "27.4B", "Q4_K_M"),
    ("mistral-small:24b", 14300, "23.6B", "Q4_K_M"),
    ("nomic-embed-text:latest", 274, "137M", "F16"),
]


def ollama_entry(port, loaded_names, webui=None):
    """An Ollama server as the agent reports it: resident models + catalog."""
    by_name = {t[0]: t for t in OLLAMA_TAGS}
    loaded = []
    for i, name in enumerate(loaded_names):
        _, size_mib, params, quant = by_name[name]
        loaded.append(
            {
                "id": name,
                "vram_mib": round(size_mib * 0.96, 1),
                "size_mib": float(size_mib),
                "expires_in_s": 60 * (4 + 7 * i) - int(time.time() - T0) % 200,
                "parameter_size": params,
                "quantization": quant,
                "family": name.split(":")[0],
            }
        )
    return {
        "port": port,
        "engine": "ollama",
        "engine_version": "0.12.4",
        "container": "ollama" if webui else None,
        "container_id": "0ll4m4c0nt",
        "image": "ollama/ollama:latest" if webui else None,
        "uptime_s": int(time.time() - T0) + 86400,
        "restarts": 0,
        "health": None,
        "reachable": True,
        "error": None,
        "metrics": None,
        "metrics_error": "Ollama exposes no Prometheus metrics",
        "models": [{"id": m["id"], "from": "ollama-ps"} for m in loaded],
        "loaded": loaded,
        "available": [
            {"id": n, "size_mib": float(sz), "parameter_size": pp, "quantization": q,
             "modified_at": "2026-08-02T10:00:00Z"}
            for n, sz, pp, q in OLLAMA_TAGS
        ],
        "loaded_count": len(loaded),
        "available_count": len(OLLAMA_TAGS),
        "vram_mib": round(sum(m["vram_mib"] for m in loaded), 1) or None,
        "next_unload_s": min([m["expires_in_s"] for m in loaded], default=None),
        "implicit": False,
    }


def advance(key, rate):
    """Integrate a varying rate into a monotonically increasing counter."""
    now = time.time()
    prev_t, total = COUNTERS.get(key, (now, 40000.0))
    total += rate * max(0.0, now - prev_t)
    COUNTERS[key] = (now, total)
    return total

FLEET = [
    dict(port=19901, host="spark-01", gpu="NVIDIA GB10", mem=131072, unified=True,
         servers=[(8000, "meta-llama/Llama-3.3-70B-Instruct", "vllm-llama70b", 0),
                  (8001, "Qwen/Qwen2.5-Coder-32B-Instruct", "vllm-qwen-coder", 1)]),
    dict(port=19902, host="spark-02", gpu="NVIDIA GB10", mem=131072, unified=True,
         servers=[(8000, "mistralai/Mistral-Small-3.1-24B-Instruct", "vllm-mistral", 0)]),
    dict(port=19903, host="spark-03", gpu="NVIDIA GB10", mem=131072, unified=True, dead=True,
         servers=[(8000, "deepseek-ai/DeepSeek-R1-Distill-Llama-70B", "vllm-r1", 0)]),
    dict(port=19904, host="zgx-01", gpu="NVIDIA GB10", mem=131072, unified=True,
         servers=[(8000, "google/gemma-3-27b-it", "vllm-gemma", 0),
                  (8100, "BAAI/bge-m3", "vllm-embed", 6)],
         ollama=(11434, ["llama3.3:70b", "qwen2.5-coder:32b"]), webui=3000),
    dict(port=19905, host="zgx-02", gpu="NVIDIA GB10", mem=131072, unified=True,
         servers=[(8000, "Qwen/Qwen3-32B", "vllm-qwen3", 0),
                  (8001, "openai/gpt-oss-20b", "vllm-gptoss", 2)],
         ollama=(11434, []), webui=3000),   # idle Ollama: nothing resident
]


def snapshot(node):
    t = time.time() - T0
    seed = sum(ord(c) for c in node["host"])
    rnd = random.Random(int(t / 7) + seed)
    util = max(0.0, min(100.0, 42 + 34 * math.sin(t / 47 + seed) + rnd.uniform(-7, 7)))
    mem_used = node["mem"] * (0.55 + 0.16 * math.sin(t / 90 + seed / 3))

    vllm, containers = [], []
    for i, (port, model, cname, mode) in enumerate(node["servers"]):
        down = mode == 1
        containers.append(dict(
            id=f"{seed:04x}{i}abcd12", name=cname, image="vllm/vllm-openai:v0.10.1",
            state="exited" if down else "running", health=None if down else "healthy",
            started_at=None, uptime_s=None if down else int(t + 3600 * (6 + i)),
            restarts=4 if down else (1 if mode == 2 else 0), network_mode="bridge",
            ports=[{"host_ip": "0.0.0.0", "host_port": port, "container_port": port}],
            is_vllm=True, model_arg=model, served_model_name=model,
            tensor_parallel_size=1, gpu_memory_utilization=0.9, max_model_len=32768))
        if down:
            vllm.append(dict(engine="vllm", port=port, container=cname, container_id=f"{seed:04x}{i}abcd12",
                             image="vllm/vllm-openai:v0.10.1", uptime_s=None, restarts=4,
                             health=None, reachable=False, models=[],
                             error="Connection refused", metrics=None,
                             metrics_error="server unreachable",
                             tensor_parallel_size=1, max_model_len=32768,
                             gpu_memory_utilization=0.9))
            continue
        rate = 0.0 if mode == 6 else 120 + 90 * abs(math.sin(t / 31 + i + seed))
        vllm.append(dict(
            engine="vllm", port=port, container=cname, container_id=f"{seed:04x}{i}abcd12",
            image="vllm/vllm-openai:v0.10.1", uptime_s=int(t + 3600 * (6 + i)),
            restarts=1 if mode == 2 else 0, health="healthy", reachable=True,
            models=[{"id": model, "max_model_len": 32768, "owned_by": "vllm"}],
            error=None, metrics_error=None,
            tensor_parallel_size=1, max_model_len=32768, gpu_memory_utilization=0.9,
            metrics={
                "vllm:num_requests_running": float(rnd.randint(0, 5) if mode != 6 else 0),
                "vllm:num_requests_waiting": float(rnd.randint(0, 2) if mode != 6 else 0),
                "vllm:gpu_cache_usage_perc": min(0.97, 0.12 + 0.5 * abs(math.sin(t / 53 + i))),
                "vllm:prompt_tokens_total": advance(f"{node['host']}:{port}:p", rate * 4),
                "vllm:generation_tokens_total": advance(f"{node['host']}:{port}:g", rate),
                "vllm:e2e_request_latency_seconds_sum": 3.1 * (t / 4 + 100),
                "vllm:e2e_request_latency_seconds_count": (t / 4 + 100),
                "vllm:time_to_first_token_seconds_sum": 0.28 * (t / 4 + 100),
                "vllm:time_to_first_token_seconds_count": (t / 4 + 100),
            }))
    if node.get("ollama"):
        oport, oloaded = node["ollama"]
        vllm.append(ollama_entry(oport, oloaded, webui=node.get("webui")))
    web_uis = ([{"name": "open-webui", "image": "ghcr.io/open-webui/open-webui:main",
                 "port": node["webui"], "uptime_s": int(t) + 86400, "restarts": 0}]
               if node.get("webui") else [])

    return dict(
        agent_version="1.3.0", collected_at=time.time(), collect_ms=42,
        hostname=node["host"], board_model="NVIDIA DGX Spark", kernel="6.11.0-nv",
        arch="aarch64", uptime_s=int(t + 86400 * 3.4), load_avg=[2.1, 1.8, 1.6],
        cpu_count=20, disk_root={"total_gib": 3720.0, "used_gib": 812.4, "used_pct": 21.8},
        host_memory={"total_mib": node["mem"], "available_mib": node["mem"] - mem_used,
                     "used_mib": mem_used, "used_pct": round(mem_used / node["mem"] * 100, 1)},
        unified_memory=node["unified"],
        gpus=[dict(index=0, uuid="GPU-fake", name=node["gpu"],
                   memory_total_mib=node["mem"], memory_used_mib=round(mem_used),
                   memory_free_mib=round(node["mem"] - mem_used),
                   memory_used_pct=round(mem_used / node["mem"] * 100, 1),
                   util_gpu_pct=round(util, 1), util_mem_pct=round(util * 0.7, 1),
                   temp_c=round(48 + util * 0.28, 1), power_w=round(60 + util * 1.5, 1),
                   power_limit_w=240, clock_sm_mhz=1400,
                   source="nvidia-smi", unified_memory=True, memory_source="host-unified")],
        gpu_error=None, docker_error=None, containers=containers,
        servers=vllm, vllm=vllm, web_uis=web_uis)


def make_handler(node):
    class H(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *a):
            pass

        def do_GET(self):
            body = json.dumps(snapshot(node)).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
    return H


def main():
    for node in FLEET:
        if node.get("dead"):
            print(f"  {node['host']}: intentionally offline (port {node['port']} closed)")
            continue
        srv = ThreadingHTTPServer(("127.0.0.1", node["port"]), make_handler(node))
        srv.daemon_threads = True
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        print(f"  {node['host']}: agent on 127.0.0.1:{node['port']}")
    print("fake fleet running — ctrl-c to stop")
    while True:
        time.sleep(3600)


if __name__ == "__main__":
    main()
