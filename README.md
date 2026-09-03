# vLLM Fleet Dashboard

One page that shows all five DGX Spark / HP ZGX machines at once: which models are
loaded on which ports, whether each vLLM container is actually serving, GPU memory
and utilization per box, live throughput, and a copy button for every endpoint URL.

```
┌───────────────┐   HTTP :9900   ┌──────────────────────────┐
│  spark-01     │◄───────────────┤                          │
│  spark-02     │◄───────────────┤   dashboard (FastAPI)    │◄── browser :8080
│  spark-03     │◄───────────────┤   polls every 10s,       │
│  zgx-01       │◄───────────────┤   keeps 2h of history    │
│  zgx-02       │◄───────────────┤                          │
└───────────────┘                └──────────────────────────┘
   agent reads nvidia-smi,
   docker, and each vLLM's
   /v1/models + /metrics
```

The agent runs **on each node** because vLLM's own `/metrics` endpoint knows nothing
about the GPU — it reports KV-cache usage and request counts, not memory or SM
utilization. `nvidia-smi` has to be read locally. The agent also finds vLLM servers
bound to `127.0.0.1` and containers on `--network host`, which a remote scan cannot.

## What you get

| Per machine | Per model server |
|---|---|
| Online / degraded / offline, with the reason | Port and model ID (from `/v1/models`) |
| GPU name, memory used / total, compute % | Serving / down, container name, uptime, restart count |
| Temperature, power draw | Requests running and queued, KV-cache % |
| 2 hours of GPU-utilization history | Generation tok/s, end-to-end latency, TTFT (vLLM) |
| Open WebUI links | Resident + available models, VRAM, unload countdown (Ollama) |
| Board model, host uptime | Copy buttons: base URL, model ID, ready-to-run `curl` |

### Ollama and Open WebUI

The agent also finds **Ollama** on each node — whether it runs as a systemd service
or in Docker — by probing `127.0.0.1:11434`. Ollama stays bound to loopback exactly
as intended; nothing needs to be exposed to the network, because the agent is already
on the machine.

For each Ollama server you get:

- **Resident models** (`/api/ps`) — what is loaded in VRAM right now, each with its
  own VRAM cost, parameter size, quantization, and a countdown to its `keep_alive`
  unload. Per-model memory attribution that vLLM can't give you, since vLLM
  preallocates one pool.
- **Available models** (`/api/tags`) — everything pulled onto that box and ready to
  serve, with sizes; expandable, with a copy button per model ID.
- **No throughput, latency, queue depth or KV cache.** Ollama exposes no Prometheus
  endpoint, so those read `–` rather than a made-up zero. The fleet throughput tile
  says "vLLM only" when an Ollama server is present.

An idle Ollama has unloaded everything and reads **ready** in green — that is healthy,
not down. It only goes red when `/api/version` stops answering.

**Open WebUI** containers are detected too, and appear as a link on the node card
(`open-webui → :3000`). They carry `OLLAMA_BASE_URL`, so they are classified before
Ollama and never probed as a model server.

If Ollama listens somewhere other than 11434, set `FLEET_OLLAMA_PORTS=11434,11435`
in that node's unit file.

DGX Spark's GB10 shares one memory pool between CPU and GPU. The agent detects that
and reports the unified pool, labeled "unified memory" on the card, so the number
means what you expect.

---

## Install

### 1. On each of the five machines

Copy the `agent/` folder over and run the installer as root:

```bash
scp -r agent/ user@192.168.1.101:/tmp/fleet-agent
ssh user@192.168.1.101 'sudo bash /tmp/fleet-agent/install-agent.sh 9900'
```

That drops `spark_agent.py` into `/opt/vllm-fleet-agent/`, installs a systemd unit,
and starts it. Check it:

```bash
curl -s localhost:9900/health
curl -s localhost:9900/stats | python3 -m json.tool | head -40
```

The agent is one stdlib-only Python file — no pip install, no container. It needs to
run as a user that can execute `docker ps` and `nvidia-smi` (root by default).

Re-running the installer later upgrades an agent in place — it reinstalls the file
and restarts the service.

Open the port to the dashboard host only:

```bash
sudo ufw allow from <DASHBOARD_IP> to any port 9900 proto tcp
```

**Want a shared secret?** Pass it as the second argument
(`install-agent.sh 9900 my-token`) and put the same value under `agent_token:` in
`config.yaml`.

### 2. On whichever machine hosts the dashboard

```bash
cp config.example.yaml config.yaml
$EDITOR config.yaml          # put in your 5 IPs, names, and notes
pip install -r requirements.txt
uvicorn server.app:app --host 0.0.0.0 --port 8080
```

Open `http://<that machine>:8080`.

Or with Docker:

```bash
cd docker && docker compose up -d --build
```

To run the dashboard itself under systemd, the same pattern as the agent works —
`ExecStart=/usr/bin/python3 -m uvicorn server.app:app --host 0.0.0.0 --port 8080`
with `WorkingDirectory=` pointing at this folder.

---

## Deploying it for other people

Pick one machine that is always on and can reach all five nodes — one of the Sparks
is fine, or any small always-up host on the same network. Then, from the project
folder on that machine:

```bash
cp config.example.yaml config.yaml
$EDITOR config.yaml                       # your 5 IPs and names
sudo bash deploy/install-dashboard.sh 8080
```

That creates an unprivileged `fleetdash` user, installs into
`/opt/vllm-fleet-dashboard` with its own virtualenv, starts a systemd service that
survives reboots, generates an admin token, and prints two links:

```
Share this with colleagues:     http://fleet-host.your-lab.ornl.gov:8080/
Your admin link (keep private): http://fleet-host.your-lab.ornl.gov:8080/?admin=<token>
```

Send the first one around. Anyone on the network who opens it gets the live
dashboard — no login, nothing to install, and nothing they can change. Open the
second link once yourself: the token is stored in your browser, the URL is scrubbed
from the address bar, an `admin` marker appears in the header, and the Refresh
control becomes available. Opening `?admin=` with an empty value signs that browser
back out.

Then open the port to the lab network and no further:

```bash
sudo ufw allow from 192.168.0.0/16 to any port 8080 proto tcp   # adjust to your subnet
sudo ufw reload
```

The token is read from `FLEET_ADMIN_TOKEN` in the unit file (mode 640, root-owned) so
it never sits in `config.yaml`, which colleagues may end up reading. It's stored at
`/etc/vllm-fleet-dashboard.token` if you need it again:

```bash
sudo cat /etc/vllm-fleet-dashboard.token
```

**What "read-only" covers.** Every viewer can fetch the dashboard and the JSON API.
Only an admin can `POST /api/refresh`, the one call that makes the server act. The
API exposes hostnames, ports, model names and load — no credentials and no way to
reach the vLLM servers through it. Treat the page as visible to anyone on the network
you open the port to.

### Optional: a hostname instead of a port number

`deploy/Caddyfile.example` puts the dashboard behind
`https://fleet.your-lab.ornl.gov` with a certificate. Worth doing if you want the
admin token encrypted in transit — over plain HTTP it travels in a header on the
internal network. If you add the proxy, change the unit's `--host 0.0.0.0` to
`--host 127.0.0.1` so the proxy becomes the only way in.

### Reaching it from off-site

Don't open the port outward. Colleagues off the lab network should come in over the
lab VPN, or tunnel to it over SSH:

```bash
ssh -L 8080:fleet-host:8080 you@lab-gateway    # then open http://localhost:8080
```

Anything beyond that (an institutional reverse proxy, a public hostname) is an ORNL
networking/security decision rather than a configuration change here.

---

## Configuration

`config.yaml` is the whole inventory. The fields that matter:

```yaml
poll_interval_s: 10          # every node, every 10s
history_points: 720          # 2h of sparkline history at that interval
history_file: "./fleet-history.json"   # survives a restart; omit to keep in RAM

nodes:
  - name: spark-01           # what the card is titled
    host: 192.168.1.101      # how the dashboard reaches the agent
    agent_port: 9900
    public_host: spark-01.lan  # optional: what the copy-ready URLs should say
    note: "GB10 · lab bench"
```

A node that serves nothing by design gets `expect_vllm: false` — it reads *online*
with "no model loaded right now" instead of being flagged amber for a missing server.
Delete the line once it starts serving.

A node with `agent: false` and `vllm_ports: [8000, 8001]` is polled directly over the
network — you still get models, ports, throughput and KV cache, but no GPU stats.
Useful for a box you can't install on yet.

Adding a sixth machine later is two lines in `config.yaml` plus the installer on that
box; nothing else changes.

## API

The page is a thin client over a plain JSON API, so you can script against it:

| Endpoint | Returns |
|---|---|
| `GET /api/fleet` | Everything: fleet summary, all nodes, all endpoints, history |
| `GET /api/fleet?history=false` | Same without the history arrays (small) |
| `GET /api/node/{name}` | One node plus its history |
| `POST /api/refresh` | Force an immediate poll — **admin token required** (`X-Admin-Token`) |
| `GET /api/whoami` | Whether this caller has admin rights |
| `GET /healthz` | Liveness plus age of the last poll |
| `GET /api/docs` | Interactive OpenAPI docs |

Example — which machine is free right now:

```bash
curl -s localhost:8080/api/fleet?history=false \
  | python3 -c 'import json,sys; d=json.load(sys.stdin); \
    print(sorted(((n["gpu_util_pct"] or 0, n["name"]) for n in d["nodes"] \
    if n["status"]=="online"))[0][1])'
```

## Reading the page

- **Cards / Table** — cards for browsing, table for scanning all endpoints at once
  and copying URLs. Every status also carries an icon and a word, never color alone.
- **Meters** turn amber past 80% and red past 92%; the number beside them always
  says the same thing.
- **Sparklines** show GPU utilization over the retained window; hover for the value
  at a point in time. A gap means the node was unreachable then.
- **Theme** follows your OS by default; the button overrides it.

## Troubleshooting

| Symptom | Cause |
|---|---|
| Node shows *offline* | Agent not running, or port 9900 blocked. `systemctl status vllm-fleet-agent` on that box. |
| Node *degraded*, "vLLM not answering on port N" | The container is up but the server isn't accepting requests — still loading weights, or it crashed. Check `docker logs`. |
| "no vLLM server found on this node" | The agent looks for containers whose image or command mentions vLLM. If yours is named something else, set `FLEET_VLLM_PORTS=8000,8001` in the unit file. |
| `template parsing error: function "dict" not defined` | Agent older than 1.2.0. Upgrade it: re-run `sudo bash agent/install-agent.sh 9900` on that node. |
| Ollama not showing up | Agent older than 1.3.0, or Ollama isn't on 11434 — set `FLEET_OLLAMA_PORTS` in the unit file. |
| Ollama shows "nothing resident" | Normal: `keep_alive` expired and the models unloaded. The next request reloads one. |
| Ollama row has no tok/s | Expected — Ollama publishes no metrics endpoint. |
| GPU stats missing, everything else fine | `nvidia-smi` isn't on PATH for the agent's user, or the agent isn't running as root. |
| tok/s shows `–` | Needs two consecutive polls to derive a rate; it fills in after ~20s. |
| Refresh button missing | That browser has no admin token — open the `?admin=<token>` link once. |
| Colleagues can't load the page | Firewall. Check `sudo ufw status` on the dashboard host and that the service is bound to `0.0.0.0`. |
| Model column empty on a down endpoint | vLLM isn't answering, so the name comes from the container's `--model` argument if it has one. |

## Layout

```
agent/spark_agent.py             the node agent (stdlib only, one file)
agent/install-agent.sh           systemd installer, run on each node
agent/vllm-fleet-agent.service   the unit it installs
server/app.py                    polling, rate derivation, history, JSON API
server/static/index.html         the page
server/static/app.js             rendering, sparklines, copy buttons
config.example.yaml              inventory template
deploy/install-dashboard.sh      installs the dashboard as a systemd service
deploy/vllm-fleet-dashboard.service  the unit it installs
deploy/Caddyfile.example         optional HTTPS + hostname in front of it
docker/                          Dockerfile + compose for the dashboard
tools/fake_fleet.py              simulates 5 machines so you can try it offline
```

To see it without touching hardware:

```bash
python3 tools/fake_fleet.py &                       # 5 fake agents on 199xx
FLEET_CONFIG=tools/sim-config.yaml uvicorn server.app:app --port 8080
```
