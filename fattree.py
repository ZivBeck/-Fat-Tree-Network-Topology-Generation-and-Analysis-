import argparse
import json
import math
import random
import sys
from collections import Counter, deque

import networkx as nx


HOST, EDGE, AGG, CORE = "host", "edge", "agg", "core"


def validate_k(k):
    if not isinstance(k, int) or k < 2 or k % 2 != 0:
        raise ValueError(f"k must be an even integer >= 2, got {k!r}")


def build_fat_tree(k):
    validate_k(k)
    half = k // 2
    G = nx.Graph(k=k)

    for j in range(half):
        for i in range(half):
            G.add_node(f"c_{j}_{i}", layer=CORE, ip=f"10.{k}.{j + 1}.{i + 1}")

    for pod in range(k):
        for idx in range(half):
            G.add_node(f"e_{pod}_{idx}", layer=EDGE, pod=pod, ip=f"10.{pod}.{idx}.1")
            G.add_node(f"a_{pod}_{idx}", layer=AGG, pod=pod, ip=f"10.{pod}.{idx + half}.1")

        for e in range(half):
            edge = f"e_{pod}_{e}"
            for h in range(half):
                host = f"h_{pod}_{e}_{h}"
                G.add_node(host, layer=HOST, pod=pod, ip=f"10.{pod}.{e}.{h + 2}")
                G.add_edge(host, edge)
            for a in range(half):
                G.add_edge(edge, f"a_{pod}_{a}")

        for a in range(half):
            for i in range(half):
                G.add_edge(f"a_{pod}_{a}", f"c_{a}_{i}")

    return G


def nodes_by_layer(G, layer):
    return [n for n, d in G.nodes(data=True) if d["layer"] == layer]


def expected_counts(k):
    validate_k(k)
    return {
        "hosts": k**3 // 4,
        "edge": k * k // 2,
        "agg": k * k // 2,
        "core": (k // 2) ** 2,
        "switches": 5 * k * k // 4,
        "links": 3 * k**3 // 4,
    }


def summarize(G):
    return {
        "hosts": len(nodes_by_layer(G, HOST)),
        "edge": len(nodes_by_layer(G, EDGE)),
        "agg": len(nodes_by_layer(G, AGG)),
        "core": len(nodes_by_layer(G, CORE)),
        "switches": G.number_of_nodes() - len(nodes_by_layer(G, HOST)),
        "links": G.number_of_edges(),
    }


def failable_links(G, include_host_links=False):
    links = [
        (u, v) for u, v in G.edges()
        if include_host_links or HOST not in (G.nodes[u]["layer"], G.nodes[v]["layer"])
    ]
    return sorted(tuple(sorted(e)) for e in links)


def num_failures(num_links, fail_pct):
    if not 0 <= fail_pct <= 100:
        raise ValueError(f"fail_pct must be in [0, 100], got {fail_pct}")
    return round(fail_pct / 100 * num_links)


def fail_links(G, fail_pct, seed=None, include_host_links=False):
    candidates = failable_links(G, include_host_links)
    failed = random.Random(seed).sample(candidates, num_failures(len(candidates), fail_pct))
    H = G.copy()
    H.remove_edges_from(failed)
    H.graph["fail_pct"] = fail_pct
    H.graph["failed_links"] = failed
    return H, failed


def bfs_with_path_counts(G, source):
    dist, sigma = {source: 0}, {source: 1}
    queue = deque([source])
    while queue:
        u = queue.popleft()
        for v in G.neighbors(u):
            if v not in dist:
                dist[v] = dist[u] + 1
                sigma[v] = 0
                queue.append(v)
            if dist[v] == dist[u] + 1:
                sigma[v] += sigma[u]
    return dist, sigma


def baseline_hops(G, e1, e2):
    if e1 == e2:
        return 2
    return 4 if G.nodes[e1]["pod"] == G.nodes[e2]["pod"] else 6


def path_metrics(G):
    k = G.graph["k"]
    full_ecmp = (k // 2) ** 2
    switches = G.subgraph(n for n, d in G.nodes(data=True) if d["layer"] != HOST)
    edges = nodes_by_layer(G, EDGE)
    attached = {e: sum(G.nodes[n]["layer"] == HOST for n in G.neighbors(e)) for e in edges}
    num_hosts = len(nodes_by_layer(G, HOST))
    total_pairs = num_hosts * (num_hosts - 1) // 2

    hosts_per_pod = k * k // 4
    inter_pairs = num_hosts * (num_hosts - hosts_per_pod) // 2

    connected = hops = stretched = 0
    ecmp_sum = 0.0
    for i, e1 in enumerate(edges):
        n1 = attached[e1]
        same = n1 * (n1 - 1) // 2
        connected += same
        hops += 2 * same
        if n1 == 0:
            continue
        dist, sigma = bfs_with_path_counts(switches, e1)
        for e2 in edges[i + 1:]:
            w = n1 * attached[e2]
            if w == 0:
                continue
            base = baseline_hops(G, e1, e2)
            inter_pod = base == 6
            if e2 not in dist:
                continue
            h = dist[e2] + 2
            connected += w
            hops += w * h
            if h > base:
                stretched += w
            elif inter_pod:
                ecmp_sum += w * sigma[e2] / full_ecmp

    return {
        "disconnected_fraction": 1 - connected / total_pairs if total_pairs else 0.0,
        "avg_hops": hops / connected if connected else math.nan,
        "stretched_fraction": stretched / connected if connected else math.nan,
        "ecmp_survival": ecmp_sum / inter_pairs if inter_pairs else math.nan,
    }


def failures_per_switch(G, failed):
    c = Counter()
    for u, v in failed:
        for n in (u, v):
            if G.nodes[n]["layer"] != HOST:
                c[n] += 1
    return c


def count_multi_failure_switches(G, failed, threshold=2):
    return sum(1 for n in failures_per_switch(G, failed).values() if n >= threshold)


def _log_comb(n, r):
    if r < 0 or r > n:
        return -math.inf
    return math.lgamma(n + 1) - math.lgamma(r + 1) - math.lgamma(n - r + 1)


def expected_multi_failure_switches(k, num_failed):
    L = k**3 // 2
    m = num_failed

    def p_at_least_two(d):
        denom = _log_comb(L, m)
        p0 = math.exp(_log_comb(L - d, m) - denom)
        p1 = d * math.exp(_log_comb(L - d, m - 1) - denom)
        return max(0.0, 1 - p0 - p1)

    num = {EDGE: k * k // 2, AGG: k * k // 2, CORE: (k // 2) ** 2}
    return num[EDGE] * p_at_least_two(k // 2) + (num[AGG] + num[CORE]) * p_at_least_two(k)


DEFAULTS = {
    "k": 4,
    "fail_pct": 0.0,
    "seed": None,
    "include_host_links": False,
    "output": None,
}


def parse_args(argv=None):
    p = argparse.ArgumentParser(prog="fattree", description="Fat-tree topology generator")
    p.add_argument("--config", help="JSON file with any of: " + ", ".join(DEFAULTS))
    p.add_argument("--k", type=int, help="switch port count (even, >= 2). Default 4")
    p.add_argument("--fail-pct", type=float, help="percentage of failable links to fail, 0-100. Default 0")
    p.add_argument("--seed", type=int, help="random seed for reproducible failures")
    p.add_argument("--include-host-links", action=argparse.BooleanOptionalAction, default=None,
                   help="allow host-edge links to fail too (default: switch links only)")
    p.add_argument("--output", help="write the resulting topology as node-link JSON to this path")
    return p.parse_args(argv)


def resolve_config(args):
    cfg = dict(DEFAULTS)
    if args.config:
        with open(args.config) as f:
            file_cfg = json.load(f)
        unknown = set(file_cfg) - set(DEFAULTS)
        if unknown:
            raise ValueError(f"unknown config keys: {sorted(unknown)}")
        cfg.update(file_cfg)
    for key in DEFAULTS:
        value = getattr(args, key)
        if value is not None:
            cfg[key] = value
    return cfg


def host_reachability(G):
    hosts = set(nodes_by_layer(G, HOST))
    n = len(hosts)
    if n < 2:
        return 1.0
    reachable = 0
    for comp in nx.connected_components(G):
        h = len(hosts & comp)
        reachable += h * (h - 1)
    return reachable / (n * (n - 1))


def run(cfg):
    G = build_fat_tree(cfg["k"])
    report = {"config": cfg, "topology": summarize(G)}
    G, failed = fail_links(G, cfg["fail_pct"], cfg["seed"], cfg["include_host_links"])
    report["failed_links"] = len(failed)
    report["host_pair_reachability"] = host_reachability(G)

    if cfg["output"]:
        data = nx.node_link_data(G, edges="links")
        data["graph"]["failed_links"] = [list(e) for e in G.graph.get("failed_links", [])]
        with open(cfg["output"], "w") as f:
            json.dump(data, f, indent=1)
    return G, report


def main(argv=None):
    try:
        cfg = resolve_config(parse_args(argv))
        _, report = run(cfg)
    except (ValueError, OSError, json.JSONDecodeError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    print(json.dumps(report, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
