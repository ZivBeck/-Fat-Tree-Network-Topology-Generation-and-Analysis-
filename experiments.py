import argparse
import math
import os
import random
import statistics
import time

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt

from fattree import (
    _log_comb, build_fat_tree, count_multi_failure_switches, expected_counts,
    expected_multi_failure_switches, fail_links, failable_links, num_failures, path_metrics,
)


RESULTS = os.path.join(os.path.dirname(os.path.abspath(__file__)), "results")

SERIES = ["#2a78d6", "#eb6834", "#1baf7a", "#eda100"]
INK, MUTED, GRID = "#1f2328", "#59636e", "#d8dee4"

plt.rcParams.update({
    "font.size": 10, "axes.edgecolor": GRID, "axes.labelcolor": INK, "axes.titlecolor": INK,
    "axes.titlesize": 11, "axes.titleweight": "bold", "xtick.color": MUTED,
    "ytick.color": MUTED, "axes.grid": True, "grid.color": GRID, "grid.linewidth": 0.6,
    "axes.spines.top": False, "axes.spines.right": False, "legend.frameon": False,
    "figure.dpi": 130, "lines.linewidth": 2, "lines.markersize": 5,
})


def mean_std(values):
    vals = [v for v in values if not math.isnan(v)]
    if not vals:
        return math.nan, math.nan
    return statistics.fmean(vals), statistics.pstdev(vals)


PATH_KEYS = ["avg_hops", "stretched_fraction", "ecmp_survival", "disconnected_fraction"]


def ecmp_survival_theory(k, pct):
    L = k**3 // 2
    m = num_failures(L, pct)
    return math.exp(_log_comb(L - 4, m) - _log_comb(L, m))


def experiment_paths(ks, pcts, trials, seed):
    rows = []
    for k in ks:
        G = build_fat_tree(k)
        for pct in pcts:
            samples = {key: [] for key in PATH_KEYS}
            for t in range(trials if pct > 0 else 1):
                H, _ = fail_links(G, pct, seed=hash((seed, k, pct, t)))
                for key, v in path_metrics(H).items():
                    samples[key].append(v)
            row = {"k": k, "fail_pct": pct, "trials": len(samples["avg_hops"])}
            for key in PATH_KEYS:
                row[f"{key}_mean"], row[f"{key}_std"] = mean_std(samples[key])
            row["ecmp_survival_theory"] = ecmp_survival_theory(k, pct)
            rows.append(row)
            print(f"  k={k:>2} fail={pct:>4}%  " + "  ".join(
                f"{key}={row[f'{key}_mean']:.4f}" for key in PATH_KEYS), flush=True)
    return rows


def plot_paths(rows, ks):
    fig, axes = plt.subplots(2, 2, figsize=(10, 7.2), constrained_layout=True)
    panels = [
        ("avg_hops", "Average host-to-host path length", "hops", False),
        ("stretched_fraction", "Pairs whose shortest path got longer", "fraction of connected pairs", False),
        ("ecmp_survival", "Surviving equal-cost paths (inter-pod pairs)", "fraction of (k/2)² paths", False),
        ("disconnected_fraction", "Disconnected host pairs", "fraction of all pairs (log)", True),
    ]
    for ax, (key, title, ylabel, log) in zip(axes.flat, panels):
        for color, k in zip(SERIES, ks):
            r = [row for row in rows if row["k"] == k]
            x = [row["fail_pct"] for row in r]
            y = [row[f"{key}_mean"] for row in r]
            s = [row[f"{key}_std"] for row in r]
            if log:
                pts = [(a, b) for a, b in zip(x, y) if b > 0]
                if pts:
                    ax.plot(*zip(*pts), marker="o", color=color, label=f"k={k}")
                continue
            ax.plot(x, y, marker="o", color=color, label=f"k={k}")
            ax.fill_between(x, [a - b for a, b in zip(y, s)], [a + b for a, b in zip(y, s)],
                            color=color, alpha=0.12, linewidth=0)
        if key == "ecmp_survival":
            r = [row for row in rows if row["k"] == ks[-1]]
            ax.plot([row["fail_pct"] for row in r], [row["ecmp_survival_theory"] for row in r],
                    color=INK, linestyle="--", linewidth=1.2, label="theory ≈ (1−p)⁴")
        if log:
            ax.set_yscale("log")
        ax.set_title(title, loc="left")
        ax.set_xlabel("failed switch links (%)")
        ax.set_ylabel(ylabel)
    axes.flat[0].legend(loc="upper left")
    axes.flat[2].legend(loc="upper right")
    fig.suptitle("Fat-tree under random link failures (mean ± 1 std over trials)",
                 x=0.01, ha="left", color=INK, fontsize=12, fontweight="bold")
    path = os.path.join(RESULTS, "exp1_paths_vs_failures.png")
    fig.savefig(path)
    plt.close(fig)
    return path


def experiment_multi_failures(ks, pct, trials, seed):
    rows = []
    rng = random.Random(seed)
    for k in ks:
        G = build_fat_tree(k)
        links = failable_links(G)
        m = num_failures(len(links), pct)
        counts = [count_multi_failure_switches(G, rng.sample(links, m)) for _ in range(trials)]
        mean, std = mean_std(counts)
        switches = expected_counts(k)["switches"]
        rows.append({
            "k": k, "fail_pct": pct, "failed_links": m, "switches": switches, "trials": trials,
            "multi_fail_mean": mean, "multi_fail_std": std,
            "multi_fail_theory": expected_multi_failure_switches(k, m),
            "multi_fail_fraction": mean / switches,
        })
        print(f"  k={k:>2}  failed={m:>4}  multi-fail switches={mean:.2f} ± {std:.2f}  "
              f"theory={rows[-1]['multi_fail_theory']:.2f}", flush=True)
    return rows


def plot_multi_failures(rows):
    fig, (a1, a2) = plt.subplots(1, 2, figsize=(10, 3.8), constrained_layout=True)
    ks = [r["k"] for r in rows]
    mean = [r["multi_fail_mean"] for r in rows]
    std = [r["multi_fail_std"] for r in rows]
    a1.errorbar(ks, mean, yerr=std, marker="o", color=SERIES[0], capsize=3, label="simulation")
    a1.plot(ks, [r["multi_fail_theory"] for r in rows], color=INK, linestyle="--", linewidth=1.2,
            marker="x", markersize=7, zorder=3, label="hypergeometric expectation")
    a1.set_title("Switches with ≥ 2 failed links at 1% failures", loc="left")
    a1.set_xlabel("switch port count k")
    a1.set_ylabel("switches")
    a1.legend(loc="upper left")
    a2.plot(ks, [100 * r["multi_fail_fraction"] for r in rows], marker="o", color=SERIES[0])
    a2.set_title("…as a share of all switches", loc="left")
    a2.set_xlabel("switch port count k")
    a2.set_ylabel("% of switches")
    path = os.path.join(RESULTS, "exp2_multi_failure_switches.png")
    fig.savefig(path)
    plt.close(fig)
    return path


def experiment_scale(ks):
    rows = []
    for k in ks:
        c = expected_counts(k)
        rows.append({"k": k, "hosts": c["hosts"], "switches": c["switches"], "links": c["links"]})
    return rows


def plot_scale(rows):
    fig, ax = plt.subplots(figsize=(6, 3.8), constrained_layout=True)
    ks = [r["k"] for r in rows]
    for color, key, label in [(SERIES[0], "hosts", "hosts = k³/4"),
                              (SERIES[1], "switches", "switches = 5k²/4")]:
        ys = [r[key] for r in rows]
        ax.plot(ks, ys, marker="o", color=color)
        ax.annotate(label, (ks[-1], ys[-1]), xytext=(6, 0), textcoords="offset points",
                    va="center", color=INK)
    ax.set_xscale("log", base=2)
    ax.set_yscale("log")
    ax.set_xticks(ks, [str(k) for k in ks])
    ax.set_xlim(ks[0] * 0.85, ks[-1] * 2.4)
    ax.set_title("Network size vs. switch port count", loc="left")
    ax.set_xlabel("switch port count k (log)")
    ax.set_ylabel("count (log)")
    path = os.path.join(RESULTS, "exp3_scale.png")
    fig.savefig(path)
    plt.close(fig)
    return path


def main():
    p = argparse.ArgumentParser(description="Run fat-tree experiments 1-3 and write results/")
    p.add_argument("--quick", action="store_true", help="few trials, for a smoke test")
    p.add_argument("--seed", type=int, default=2026)
    args = p.parse_args()
    os.makedirs(RESULTS, exist_ok=True)

    path_ks = [4, 8, 12, 16]
    pcts = [0, 1, 2, 5, 10, 15, 20, 30, 40, 50]
    path_trials = 5 if args.quick else 30
    multi_ks = list(range(4, 49, 4))
    multi_trials = 50 if args.quick else 500

    t0 = time.time()
    print(f"Experiment 1: path metrics vs failure % ({path_trials} trials per point)")
    rows1 = experiment_paths(path_ks, pcts, path_trials, args.seed)
    print("  ->", plot_paths(rows1, path_ks))

    print(f"Experiment 2: switches with >= 2 failed links at 1% ({multi_trials} trials per k)")
    rows2 = experiment_multi_failures(multi_ks, 1, multi_trials, args.seed)
    print("  ->", plot_multi_failures(rows2))

    print("Experiment 3: network size vs k")
    rows3 = experiment_scale([4, 8, 16, 32, 48, 64])
    print("  ->", plot_scale(rows3))
    print(f"done in {time.time() - t0:.1f}s")


if __name__ == "__main__":
    main()
