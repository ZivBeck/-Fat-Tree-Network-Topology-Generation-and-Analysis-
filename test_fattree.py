import itertools
import json
import random

import networkx as nx
import pytest

from fattree import (
    AGG, CORE, EDGE, HOST, baseline_hops, build_fat_tree, count_multi_failure_switches,
    expected_counts, expected_multi_failure_switches, fail_links, failable_links,
    host_reachability, main, nodes_by_layer, num_failures, parse_args, path_metrics,
    resolve_config, summarize,
)

K_VALUES = [2, 4, 6, 8, 12]


@pytest.mark.parametrize("k", K_VALUES)
def test_counts_match_paper(k):
    assert summarize(build_fat_tree(k)) == expected_counts(k)


@pytest.mark.parametrize("k", K_VALUES)
def test_every_switch_uses_exactly_k_ports(k):
    G = build_fat_tree(k)
    for n, d in G.nodes(data=True):
        if d["layer"] == HOST:
            assert G.degree(n) == 1
        else:
            assert G.degree(n) == k, n


@pytest.mark.parametrize("k", K_VALUES)
def test_links_only_between_adjacent_layers(k):
    allowed = {frozenset((HOST, EDGE)), frozenset((EDGE, AGG)), frozenset((AGG, CORE))}
    G = build_fat_tree(k)
    for u, v in G.edges():
        assert frozenset((G.nodes[u]["layer"], G.nodes[v]["layer"])) in allowed


@pytest.mark.parametrize("k", K_VALUES)
def test_each_core_reaches_every_pod_once(k):
    G = build_fat_tree(k)
    for c in nodes_by_layer(G, CORE):
        pods = sorted(G.nodes[a]["pod"] for a in G.neighbors(c))
        assert pods == list(range(k))


def test_hop_distances_k4():
    G = build_fat_tree(4)
    assert nx.shortest_path_length(G, "h_0_0_0", "h_0_0_1") == 2
    assert nx.shortest_path_length(G, "h_0_0_0", "h_0_1_0") == 4
    assert nx.shortest_path_length(G, "h_0_0_0", "h_3_1_1") == 6


@pytest.mark.parametrize("k", [4, 6])
def test_inter_pod_equal_cost_paths(k):
    G = build_fat_tree(k)
    paths = list(nx.all_shortest_paths(G, "h_0_0_0", f"h_{k - 1}_0_0"))
    assert len(paths) == (k // 2) ** 2


def test_ip_addresses_unique_and_paper_style():
    G = build_fat_tree(4)
    ips = [d["ip"] for _, d in G.nodes(data=True)]
    assert len(ips) == len(set(ips))
    assert G.nodes["c_0_0"]["ip"] == "10.4.1.1"
    assert G.nodes["a_1_0"]["ip"] == "10.1.2.1"
    assert G.nodes["h_1_0_0"]["ip"] == "10.1.0.2"


@pytest.mark.parametrize("bad", [0, 3, -4, 5.0, "4"])
def test_rejects_invalid_k(bad):
    with pytest.raises(ValueError):
        build_fat_tree(bad)


def test_default_excludes_host_links():
    G = build_fat_tree(4)
    links = failable_links(G)
    assert len(links) == 2 * 4**3 // 4
    assert all(HOST not in (G.nodes[u]["layer"], G.nodes[v]["layer"]) for u, v in links)
    assert len(failable_links(G, include_host_links=True)) == G.number_of_edges()


@pytest.mark.parametrize("pct, expected", [(0, 0), (10, 26), (50, 128), (100, 256)])
def test_exact_failure_count(pct, expected):
    G = build_fat_tree(8)
    H, failed = fail_links(G, pct, seed=0)
    assert len(failed) == expected
    assert H.number_of_edges() == G.number_of_edges() - len(failed)
    assert G.number_of_edges() == 384


def test_seed_is_reproducible():
    G = build_fat_tree(6)
    assert fail_links(G, 20, seed=7)[1] == fail_links(G, 20, seed=7)[1]
    assert fail_links(G, 20, seed=7)[1] != fail_links(G, 20, seed=8)[1]


@pytest.mark.parametrize("bad", [-1, 100.5])
def test_rejects_bad_pct(bad):
    with pytest.raises(ValueError):
        fail_links(build_fat_tree(4), bad)


def test_reachability_bounds():
    G = build_fat_tree(4)
    assert host_reachability(G) == 1.0
    H, _ = fail_links(G, 100, seed=0)
    assert host_reachability(H) == pytest.approx(16 / (16 * 15))


def test_cli_overrides_config(tmp_path):
    cfg_file = tmp_path / "c.json"
    cfg_file.write_text(json.dumps({"k": 8, "fail_pct": 5, "seed": 1}))
    cfg = resolve_config(parse_args(["--config", str(cfg_file), "--fail-pct", "20"]))
    assert cfg["k"] == 8 and cfg["fail_pct"] == 20 and cfg["seed"] == 1


def test_cli_rejects_unknown_config_key(tmp_path, capsys):
    cfg_file = tmp_path / "c.json"
    cfg_file.write_text(json.dumps({"kk": 4}))
    assert main(["--config", str(cfg_file)]) == 2
    assert "unknown config keys" in capsys.readouterr().err


def test_cli_writes_output(tmp_path, capsys):
    out = tmp_path / "topo.json"
    assert main(["--k", "4", "--fail-pct", "25", "--seed", "3", "--output", str(out)]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["failed_links"] == 8
    data = json.loads(out.read_text())
    assert len(data["links"]) == 48 - 8
    assert len(data["graph"]["failed_links"]) == 8


@pytest.mark.parametrize("bad", ["-5", "150"])
def test_cli_rejects_bad_pct(bad, capsys):
    assert main(["--k", "4", "--fail-pct", bad]) == 2
    assert "fail_pct" in capsys.readouterr().err


def test_cli_can_disable_host_links_from_config(tmp_path):
    cfg_file = tmp_path / "c.json"
    cfg_file.write_text(json.dumps({"include_host_links": True}))
    cfg = resolve_config(parse_args(["--config", str(cfg_file), "--no-include-host-links"]))
    assert cfg["include_host_links"] is False


def brute_force_path_metrics(G):
    k = G.graph["k"]
    hosts = nodes_by_layer(G, HOST)
    edge_of = {}
    for h in hosts:
        nbrs = list(G.neighbors(h))
        edge_of[h] = nbrs[0] if nbrs else f"e_{G.nodes[h]['pod']}_{h.split('_')[2]}"
    total = connected = hops = stretched = inter = 0
    ecmp = 0.0
    for a, b in itertools.combinations(hosts, 2):
        total += 1
        base = baseline_hops(G, edge_of[a], edge_of[b])
        if base == 6:
            inter += 1
        if not nx.has_path(G, a, b):
            continue
        d = nx.shortest_path_length(G, a, b)
        connected += 1
        hops += d
        if d > base:
            stretched += 1
        elif base == 6:
            ecmp += len(list(nx.all_shortest_paths(G, a, b))) / (k // 2) ** 2
    return {
        "disconnected_fraction": 1 - connected / total,
        "avg_hops": hops / connected,
        "stretched_fraction": stretched / connected,
        "ecmp_survival": ecmp / inter,
    }


def test_intact_k4_values():
    m = path_metrics(build_fat_tree(4))
    assert m["avg_hops"] == pytest.approx((8 * 2 + 16 * 4 + 96 * 6) / 120)
    assert m["disconnected_fraction"] == 0
    assert m["stretched_fraction"] == 0
    assert m["ecmp_survival"] == 1


@pytest.mark.parametrize("k, pct, seed, host_links", [
    (4, 10, 0, False), (4, 30, 1, False), (4, 50, 2, False),
    (6, 20, 3, False), (4, 20, 4, True), (6, 15, 5, True),
])
def test_matches_brute_force(k, pct, seed, host_links):
    H, _ = fail_links(build_fat_tree(k), pct, seed=seed, include_host_links=host_links)
    fast, slow = path_metrics(H), brute_force_path_metrics(H)
    for key in fast:
        assert fast[key] == pytest.approx(slow[key]), key


def test_multi_failure_count():
    G = build_fat_tree(4)
    failed = [("a_0_0", "e_0_0"), ("a_0_0", "e_0_1"), ("a_1_0", "c_0_0")]
    assert count_multi_failure_switches(G, failed) == 1


def test_expected_multi_failure_matches_simulation():
    k, pct = 8, 5
    G = build_fat_tree(k)
    links = failable_links(G)
    m = num_failures(len(links), pct)
    rng = random.Random(0)
    trials = 4000
    mean = sum(count_multi_failure_switches(G, rng.sample(links, m)) for _ in range(trials)) / trials
    assert mean == pytest.approx(expected_multi_failure_switches(k, m), rel=0.05)
