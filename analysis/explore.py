"""Exploratory data analysis for Creative Rotation Brain.

Generates deterministic text summaries in analysis/out/*.txt
and plots in analysis/out/*.png from data/*.csv.
Run from repository root:
    python3 analysis/explore.py
"""

from __future__ import annotations

import os
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd

SEED = 42
N_BOOT = 2000
DATA_DIR = Path("data")
OUT_DIR = Path("analysis/out")
DOW_NAMES = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
PNG_METADATA = {"Software": "explore.py"}


def load_data() -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    creatives = pd.read_csv(DATA_DIR / "creatives.csv")
    reported = pd.read_csv(DATA_DIR / "daily_reported.csv")
    final = pd.read_csv(DATA_DIR / "daily_final.csv")
    competitors = pd.read_csv(DATA_DIR / "competitor_launches.csv")
    return creatives, reported, final, competitors


def cluster_bootstrap_ci(
    df: pd.DataFrame,
    cluster_col: str,
    stat_fn,
    n_boot: int = N_BOOT,
    seed: int = SEED,
) -> tuple[float, float, float]:
    """Bootstrap by resampling whole clusters (creatives) with replacement."""
    rng = np.random.default_rng(seed)
    clusters = sorted(df[cluster_col].unique())
    n_clusters = len(clusters)
    grouped = {c: sub for c, sub in df.groupby(cluster_col, sort=True)}
    point = float(stat_fn(df))
    if n_clusters < 2:
        return point, float("nan"), float("nan")
    vals = np.empty(n_boot, dtype=float)
    for b in range(n_boot):
        sampled = rng.choice(clusters, size=n_clusters, replace=True)
        boot_df = pd.concat([grouped[c] for c in sampled], ignore_index=True)
        vals[b] = float(stat_fn(boot_df))
    valid = vals[np.isfinite(vals)]
    if len(valid) == 0:
        return point, float("nan"), float("nan")
    lo, hi = np.quantile(valid, [0.025, 0.975])
    return point, float(lo), float(hi)


def creative_bootstrap_metric(
    df_creatives: pd.DataFrame,
    stat_fn,
    n_boot: int = N_BOOT,
    seed: int = SEED,
) -> tuple[float, float, float]:
    """Bootstrap over creative-level rows (one row per creative)."""
    rng = np.random.default_rng(seed)
    n = len(df_creatives)
    point = float(stat_fn(df_creatives))
    if n < 2:
        return point, float("nan"), float("nan")
    vals = np.empty(n_boot, dtype=float)
    for b in range(n_boot):
        idx = rng.integers(0, n, size=n)
        vals[b] = float(stat_fn(df_creatives.iloc[idx]))
    valid = vals[np.isfinite(vals)]
    if len(valid) == 0:
        return point, float("nan"), float("nan")
    lo, hi = np.quantile(valid, [0.025, 0.975])
    return point, float(lo), float(hi)


def fmt_ci(pt: float, lo: float, hi: float, digits: int = 4) -> str:
    return f"{pt:.{digits}f} [95% CI: {lo:.{digits}f}, {hi:.{digits}f}]"


def pearson_corr(x: np.ndarray, y: np.ndarray) -> float:
    mask = np.isfinite(x) & np.isfinite(y)
    x, y = x[mask], y[mask]
    if len(x) < 2 or np.std(x) == 0 or np.std(y) == 0:
        return float("nan")
    return float(np.corrcoef(x, y)[0, 1])


def spearman_corr(x: np.ndarray, y: np.ndarray) -> float:
    mask = np.isfinite(x) & np.isfinite(y)
    if mask.sum() < 2:
        return float("nan")
    rx = pd.Series(x[mask]).rank(method="average").to_numpy()
    ry = pd.Series(y[mask]).rank(method="average").to_numpy()
    return pearson_corr(rx, ry)


def ols_slope(x: np.ndarray, y: np.ndarray) -> float:
    mask = np.isfinite(x) & np.isfinite(y)
    x, y = x[mask], y[mask]
    if len(x) < 2 or np.var(x) == 0:
        return float("nan")
    xm = x - x.mean()
    ym = y - y.mean()
    return float(np.dot(xm, ym) / np.dot(xm, xm))


def save_fig(fig: plt.Figure, filename: str) -> None:
    fig.tight_layout()
    fig.savefig(OUT_DIR / filename, dpi=150, metadata=PNG_METADATA)
    plt.close(fig)


# ---------------------------------------------------------------------------
# Q1: Reported vs Final
# ---------------------------------------------------------------------------
def analyze_q1(reported: pd.DataFrame, final: pd.DataFrame) -> str:
    lines = ["=== Q1: REPORTED VS FINAL (daily_reported.csv vs daily_final.csv) ==="]
    merged = reported.merge(
        final,
        on=["day", "creative_id"],
        how="outer",
        suffixes=("_rep", "_fin"),
        indicator=True,
    )
    n_total = len(merged)
    both = (merged["_merge"] == "both").sum()
    lines.append(f"Row alignment: total={n_total}, matched_both={both}, unmatched={n_total - both}")

    cols = ["budget", "spend", "impressions", "clicks", "purchases", "revenue"]
    lines.append("\nColumn differences (across n={} matched rows):".format(both))
    for col in cols:
        c_rep = merged[f"{col}_rep"]
        c_fin = merged[f"{col}_fin"]
        diff = c_fin - c_rep
        n_diff = int((diff.abs() > 1e-9).sum())
        max_abs = float(diff.abs().max())
        sum_rep = float(c_rep.sum())
        sum_fin = float(c_fin.sum())
        lines.append(
            f"  - {col:11s}: n_diff={n_diff:4d}/{both} ({100*n_diff/both:5.1f}%), "
            f"sum_rep={sum_rep:12.2f}, sum_fin={sum_fin:12.2f}, "
            f"ratio_sum={sum_fin/sum_rep if sum_rep else float('nan'):.4f}, max_abs_diff={max_abs:.2f}"
        )

    merged["age_at_60"] = 60 - merged["day"]
    merged["rev_ratio_row"] = np.where(
        merged["revenue_rep"] > 0, merged["revenue_fin"] / merged["revenue_rep"], np.nan
    )
    merged["pur_ratio_row"] = np.where(
        merged["purchases_rep"] > 0, merged["purchases_fin"] / merged["purchases_rep"], np.nan
    )

    by_day = (
        merged.sort_values("day")
        .groupby("day", as_index=False)
        .agg(
            n_rows=("creative_id", "count"),
            age_at_60=("age_at_60", "first"),
            rev_rep=("revenue_rep", "sum"),
            rev_fin=("revenue_fin", "sum"),
            pur_rep=("purchases_rep", "sum"),
            pur_fin=("purchases_fin", "sum"),
            n_rev_diff=("revenue_rep", lambda s: 0),
        )
    )
    diff_counts = (
        merged.assign(
            rev_changed=(merged["revenue_fin"] - merged["revenue_rep"]).abs() > 1e-9,
            pur_changed=(merged["purchases_fin"] - merged["purchases_rep"]).abs() > 1e-9,
        )
        .groupby("day", as_index=False)
        .agg(n_rev_diff=("rev_changed", "sum"), n_pur_diff=("pur_changed", "sum"))
    )
    by_day = by_day.drop(columns=["n_rev_diff"]).merge(diff_counts, on="day")
    by_day["rev_ratio"] = np.where(by_day["rev_rep"] > 0, by_day["rev_fin"] / by_day["rev_rep"], np.nan)
    by_day["pur_ratio"] = np.where(by_day["pur_rep"] > 0, by_day["pur_fin"] / by_day["pur_rep"], np.nan)
    by_day["rev_diff"] = by_day["rev_fin"] - by_day["rev_rep"]
    by_day["pur_diff"] = by_day["pur_fin"] - by_day["pur_rep"]

    # Summarize by age bins
    lines.append("\nFinal/Reported by age at day 60 (age = 60 - day):")
    age_bins = [
        ("age 0 (day 60)", merged["age_at_60"] == 0),
        ("age 1 (day 59)", merged["age_at_60"] == 1),
        ("age 2 (day 58)", merged["age_at_60"] == 2),
        ("age 3 (day 57)", merged["age_at_60"] == 3),
        ("age 4-6 (days 54-56)", (merged["age_at_60"] >= 4) & (merged["age_at_60"] <= 6)),
        ("age 7-10 (days 50-53)", (merged["age_at_60"] >= 7) & (merged["age_at_60"] <= 10)),
        ("age 11-13 (days 47-49!)", (merged["age_at_60"] >= 11) & (merged["age_at_60"] <= 13)),
        ("age 14-29 (days 31-46)", (merged["age_at_60"] >= 14) & (merged["age_at_60"] <= 29)),
        ("age 30-59 (days 1-30)", (merged["age_at_60"] >= 30) & (merged["age_at_60"] <= 59)),
    ]
    for label, mask in age_bins:
        sub = merged[mask]
        n = len(sub)
        r_rep, r_fin = sub["revenue_rep"].sum(), sub["revenue_fin"].sum()
        p_rep, p_fin = sub["purchases_rep"].sum(), sub["purchases_fin"].sum()
        n_chg = int(((sub["revenue_fin"] - sub["revenue_rep"]).abs() > 1e-9).sum())
        rr = r_fin / r_rep if r_rep > 0 else float("nan")
        pr = p_fin / p_rep if p_rep > 0 else float("nan")
        lines.append(
            f"  {label:26s}: n={n:4d}, n_rev_changed={n_chg:3d}, "
            f"rev_fin/rep={rr:6.4f} (${r_fin:,.0f}/${r_rep:,.0f}), "
            f"pur_fin/rep={pr:6.4f} ({p_fin}/{p_rep})"
        )

    lines.append("\nDay-by-day detail for days 44 to 60:")
    lines.append("  day | age60 | n_rows | n_diff | pur_rep -> pur_fin (ratio) | rev_rep -> rev_fin (ratio)")
    for _, row in by_day[by_day["day"] >= 44].iterrows():
        lines.append(
            f"  {int(row['day']):3d} | {int(row['age_at_60']):5d} | {int(row['n_rows']):6d} | "
            f"{int(row['n_rev_diff']):6d} | "
            f"{int(row['pur_rep']):7d} -> {int(row['pur_fin']):7d} ({row['pur_ratio']:.3f}) | "
            f"${row['rev_rep']:8.2f} -> ${row['rev_fin']:8.2f} ({row['rev_ratio']:.3f})"
        )

    # Check if any rows in days 1-46 changed
    early = merged[merged["day"] <= 46]
    early_diff = int(((early["revenue_fin"] - early["revenue_rep"]).abs() > 1e-9).sum())
    early_pdiff = int(((early["purchases_fin"] - early["purchases_rep"]).abs() > 1e-9).sum())
    lines.append(
        f"\nDays 1-46 check (n={len(early)} rows): rev_changed={early_diff}, pur_changed={early_pdiff}"
    )

    # Also check days 47-49 vs 44-46 and 50-52 in final metrics (spend, impressions, clicks, purchases, rev)
    lines.append("\nCloser look at days 44-52 in daily_final (checking if anomaly is reporting-only or delivery too):")
    fin_day = (
        final[final["day"].between(44, 52)]
        .groupby("day", as_index=False)
        .agg(
            n=("creative_id", "count"),
            spend=("spend", "sum"),
            impr=("impressions", "sum"),
            clicks=("clicks", "sum"),
            pur=("purchases", "sum"),
            rev=("revenue", "sum"),
        )
    )
    rep_day = (
        reported[reported["day"].between(44, 52)]
        .groupby("day", as_index=False)
        .agg(pur_rep=("purchases", "sum"), rev_rep=("revenue", "sum"))
    )
    chk = fin_day.merge(rep_day, on="day")
    for _, r in chk.iterrows():
        roas_fin = r["rev"] / r["spend"] if r["spend"] > 0 else float("nan")
        roas_rep = r["rev_rep"] / r["spend"] if r["spend"] > 0 else float("nan")
        lines.append(
            f"  day {int(r['day'])} (n={int(r['n'])}): spend=${r['spend']:.0f}, "
            f"pur_rep={int(r['pur_rep'])}, pur_fin={int(r['pur'])}, "
            f"rev_rep=${r['rev_rep']:.0f} (ROAS={roas_rep:.2f}), "
            f"rev_fin=${r['rev']:.0f} (ROAS={roas_fin:.2f}), ratio={r['rev']/r['rev_rep'] if r['rev_rep'] else float('nan'):.3f}"
        )

    # Plot
    fig, axes = plt.subplots(1, 2, figsize=(13, 4.8))
    ax = axes[0]
    ax.plot(by_day["day"], by_day["rev_ratio"], marker="o", ms=3.5, label="Revenue (final / reported)")
    ax.plot(by_day["day"], by_day["pur_ratio"], marker="s", ms=3.0, alpha=0.8, label="Purchases (final / reported)")
    ax.axvspan(46.5, 49.5, color="orange", alpha=0.2, label="Days 47-49")
    ax.axhline(1.0, color="gray", ls="--", lw=1)
    ax.set_title("Final / Reported Ratio by Day (n=1,784 rows)")
    ax.set_xlabel("Day (1 to 60)")
    ax.set_ylabel("Final / Reported Ratio")
    ax.legend(fontsize=8)
    ax.grid(alpha=0.25)

    ax2 = axes[1]
    sub_age = by_day[by_day["age_at_60"] <= 20].sort_values("age_at_60")
    ax2.plot(sub_age["age_at_60"], sub_age["rev_ratio"], marker="o", ms=4.5, label="Revenue ratio")
    ax2.plot(sub_age["age_at_60"], sub_age["pur_ratio"], marker="s", ms=4.0, alpha=0.8, label="Purchases ratio")
    ax2.axvspan(10.5, 13.5, color="orange", alpha=0.2, label="Age 11-13 (days 47-49)")
    ax2.axhline(1.0, color="gray", ls="--", lw=1)
    ax2.set_title("Final / Reported Ratio by Age at Day 60 (age <= 20)")
    ax2.set_xlabel("Row Age at Day 60 Export (60 - day, days)")
    ax2.set_ylabel("Final / Reported Ratio")
    ax2.legend(fontsize=8)
    ax2.grid(alpha=0.25)

    save_fig(fig, "01_reported_vs_final.png")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Q2: Spend vs Budget
# ---------------------------------------------------------------------------
def analyze_q2(final: pd.DataFrame) -> str:
    lines = ["=== Q2: SPEND VS BUDGET (daily_final.csv) ==="]
    df = final.copy()
    n = len(df)
    df["shortfall"] = df["budget"] - df["spend"]
    df["spend_ratio"] = np.where(df["budget"] > 0, df["spend"] / df["budget"], np.nan)

    n_exact_eq = int((df["shortfall"].abs() <= 1e-6).sum())
    n_short_any = int((df["shortfall"] > 1e-6).sum())
    n_short_1pct = int((df["spend"] < 0.99 * df["budget"] - 1e-6).sum())
    n_over = int((df["shortfall"] < -1e-6).sum())
    lines.append(
        f"Total rows n={n}: exact_equal={n_exact_eq} ({100*n_exact_eq/n:.1f}%), "
        f"spend<budget={n_short_any} ({100*n_short_any/n:.1f}%), "
        f"spend<0.99*budget={n_short_1pct} ({100*n_short_1pct/n:.1f}%), "
        f"spend>budget={n_over}"
    )
    lines.append(
        f"Total budget=${df['budget'].sum():,.2f}, total spend=${df['spend'].sum():,.2f} "
        f"(overall delivery={100*df['spend'].sum()/df['budget'].sum():.2f}%)"
    )

    lt1 = df[df["budget"] < 1.0]
    eq0 = df[df["budget"] == 0.0]
    bw0_1 = df[(df["budget"] > 0.0) & (df["budget"] < 1.0)]
    ge1 = df[df["budget"] >= 1.0]
    lines.append(
        f"\nBudget < $1: n={len(lt1)} (budget==0: n={len(eq0)}, 0<budget<1: n={len(bw0_1)})"
    )
    if len(lt1) > 0:
        lines.append(
            f"  When budget < $1 (n={len(lt1)}): max_spend=${lt1['spend'].max():.2f}, "
            f"sum_spend=${lt1['spend'].sum():.2f}, sum_impr={int(lt1['impressions'].sum())}, "
            f"sum_clicks={int(lt1['clicks'].sum())}, sum_pur={int(lt1['purchases'].sum())}, "
            f"sum_rev=${lt1['revenue'].sum():.2f}"
        )
    if len(bw0_1) > 0:
        lines.append(
            f"  Detail for 0 < budget < $1 (n={len(bw0_1)}): budgets={sorted(bw0_1['budget'].unique().tolist())[:10]}, "
            f"spends={sorted(bw0_1['spend'].unique().tolist())}"
        )

    ge1_short = int((ge1["shortfall"] > 1e-6).sum())
    lines.append(
        f"\nWhen budget >= $1 (n={len(ge1)}): spend<budget count={ge1_short} ({100*ge1_short/len(ge1):.1f}%), "
        f"mean spend/budget={ge1['spend_ratio'].mean():.4f}, "
        f"median={ge1['spend_ratio'].median():.4f}, min={ge1['spend_ratio'].min():.4f}, "
        f"p05={ge1['spend_ratio'].quantile(0.05):.4f}"
    )

    bins = [0, 1, 25, 50, 100, 200, 10000]
    labels = ["[0,1)", "[1,25)", "[25,50)", "[50,100)", "[100,200)", "[200+)"]
    df["b_bin"] = pd.cut(df["budget"], bins=bins, labels=labels, right=False)
    lines.append("\nDelivery by budget bin:")
    for lbl in labels:
        sub = df[df["b_bin"] == lbl]
        if len(sub) == 0:
            lines.append(f"  {lbl:10s}: n=0")
            continue
        frac_short = (sub["shortfall"] > 1e-6).mean()
        mean_ratio = (sub["spend"].sum() / sub["budget"].sum()) if sub["budget"].sum() > 0 else float("nan")
        lines.append(
            f"  {lbl:10s}: n={len(sub):4d}, frac(spend<budget)={100*frac_short:5.1f}%, "
            f"sum(spend)/sum(budget)={mean_ratio:.4f}"
        )

    # Check if shortfall correlates with day or creative
    short_ge1 = ge1[ge1["shortfall"] > 1e-6]
    if len(short_ge1) > 0:
        top_c = short_ge1["creative_id"].value_counts().head(5).to_dict()
        lines.append(f"  Top creatives with spend<budget (among budget>=1): {top_c}")

    fig, axes = plt.subplots(1, 2, figsize=(12.5, 4.6))
    ax = axes[0]
    ax.scatter(df["budget"], df["spend"], s=10, alpha=0.4)
    lim = max(df["budget"].max(), df["spend"].max()) * 1.05
    ax.plot([0, lim], [0, lim], color="red", ls="--", lw=1, label="spend = budget")
    ax.set_title(f"Spend vs Budget (daily_final, n={n})")
    ax.set_xlabel("Allocated Budget ($/day)")
    ax.set_ylabel("Delivered Spend ($/day)")
    ax.legend(fontsize=8)
    ax.grid(alpha=0.25)

    ax2 = axes[1]
    valid_ratio = ge1["spend_ratio"].dropna()
    ax2.hist(valid_ratio, bins=30, color="steelblue", edgecolor="white")
    ax2.set_title(f"Spend / Budget Distribution when Budget >= $1 (n={len(ge1)})")
    ax2.set_xlabel("Spend / Budget")
    ax2.set_ylabel("Row Count")
    ax2.grid(alpha=0.25)

    save_fig(fig, "02_spend_vs_budget.png")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Q3: Creative ROAS variation and noise
# ---------------------------------------------------------------------------
def analyze_q3(creatives: pd.DataFrame, final: pd.DataFrame) -> tuple[str, pd.DataFrame]:
    lines = ["=== Q3: CREATIVE ROAS VARIATION AND NOISE (daily_final.csv) ==="]
    df = final.merge(creatives, on="creative_id", how="left").sort_values(["creative_id", "day"])
    pos = df[df["spend"] > 0].copy()
    pos["roas"] = pos["revenue"] / pos["spend"]
    pos["ctr"] = np.where(pos["impressions"] > 0, pos["clicks"] / pos["impressions"], np.nan)

    c_agg = (
        pos.groupby("creative_id", as_index=False)
        .agg(
            n_days=("day", "count"),
            first_day=("day", "min"),
            last_day=("day", "max"),
            total_spend=("spend", "sum"),
            total_rev=("revenue", "sum"),
            total_pur=("purchases", "sum"),
            total_impr=("impressions", "sum"),
            total_clicks=("clicks", "sum"),
            mean_daily_roas=("roas", "mean"),
            std_daily_roas=("roas", "std"),
            zero_pur_days=("purchases", lambda s: int((s == 0).sum())),
        )
        .merge(creatives, on="creative_id", how="left")
    )
    c_agg["roas"] = c_agg["total_rev"] / c_agg["total_spend"]
    c_agg["ctr"] = c_agg["total_clicks"] / c_agg["total_impr"]
    c_agg["cpm"] = 1000.0 * c_agg["total_spend"] / c_agg["total_impr"]

    n_c = len(c_agg)
    lines.append(f"Served creatives: n={n_c} (out of {len(creatives)} total; unserved={len(creatives)-n_c})")
    lines.append(
        f"Daily rows with spend>0: n={len(pos)} (rows with spend==0: {len(df)-len(pos)}), "
        f"zero-purchase days={int((pos['purchases']==0).sum())}/{len(pos)} "
        f"({100*(pos['purchases']==0).mean():.1f}%)"
    )
    lines.append(
        f"Creative lifetime ROAS (n={n_c}): "
        f"mean={c_agg['roas'].mean():.3f}, sd={c_agg['roas'].std(ddof=1):.3f}, "
        f"median={c_agg['roas'].median():.3f}, IQR=[{c_agg['roas'].quantile(0.25):.3f}, {c_agg['roas'].quantile(0.75):.3f}], "
        f"min={c_agg['roas'].min():.3f}, max={c_agg['roas'].max():.3f}"
    )
    lines.append(
        f"Days served per creative (n={n_c}): min={int(c_agg['n_days'].min())}, "
        f"median={c_agg['n_days'].median():.1f}, max={int(c_agg['n_days'].max())}"
    )
    lines.append(
        f"Within-creative daily ROAS SD (n={n_c}): median={c_agg['std_daily_roas'].median():.3f}, "
        f"IQR=[{c_agg['std_daily_roas'].quantile(0.25):.3f}, {c_agg['std_daily_roas'].quantile(0.75):.3f}]"
    )

    # Odd vs even days per creative
    odd = pos[pos["day"] % 2 == 1].groupby("creative_id").agg(s_odd=("spend", "sum"), r_odd=("revenue", "sum"), n_odd=("day", "count"))
    even = pos[pos["day"] % 2 == 0].groupby("creative_id").agg(s_even=("spend", "sum"), r_even=("revenue", "sum"), n_even=("day", "count"))
    oe = c_agg[["creative_id", "n_days", "total_spend"]].merge(odd, on="creative_id").merge(even, on="creative_id")
    oe["roas_odd"] = oe["r_odd"] / oe["s_odd"]
    oe["roas_even"] = oe["r_even"] / oe["s_even"]

    oe_r_pt, oe_r_lo, oe_r_hi = creative_bootstrap_metric(
        oe, lambda d: pearson_corr(d["roas_odd"].to_numpy(), d["roas_even"].to_numpy())
    )
    oe_s_pt, oe_s_lo, oe_s_hi = creative_bootstrap_metric(
        oe, lambda d: spearman_corr(d["roas_odd"].to_numpy(), d["roas_even"].to_numpy())
    )
    lines.append(
        f"\nOdd vs Even day ROAS across creatives (n={len(oe)}):"
        f"\n  Pearson r  = {fmt_ci(oe_r_pt, oe_r_lo, oe_r_hi, 3)}"
        f"\n  Spearman r = {fmt_ci(oe_s_pt, oe_s_lo, oe_s_hi, 3)}"
    )

    # First half vs second half of each creative's served life
    half_rows = []
    for cid, sub in pos.groupby("creative_id", sort=True):
        sub = sub.sort_values("day")
        if len(sub) < 4:
            continue
        mid = len(sub) // 2
        h1, h2 = sub.iloc[:mid], sub.iloc[mid:]
        half_rows.append(
            {
                "creative_id": cid,
                "n1": len(h1),
                "n2": len(h2),
                "roas_h1": h1["revenue"].sum() / h1["spend"].sum(),
                "roas_h2": h2["revenue"].sum() / h2["spend"].sum(),
            }
        )
    hf = pd.DataFrame(half_rows)
    hf["diff_h2_h1"] = hf["roas_h2"] - hf["roas_h1"]
    hf_r_pt, hf_r_lo, hf_r_hi = creative_bootstrap_metric(
        hf, lambda d: pearson_corr(d["roas_h1"].to_numpy(), d["roas_h2"].to_numpy())
    )
    hf_s_pt, hf_s_lo, hf_s_hi = creative_bootstrap_metric(
        hf, lambda d: spearman_corr(d["roas_h1"].to_numpy(), d["roas_h2"].to_numpy())
    )
    hf_d_pt, hf_d_lo, hf_d_hi = creative_bootstrap_metric(
        hf, lambda d: d["diff_h2_h1"].mean()
    )
    lines.append(
        f"\nFirst half vs Second half of life ROAS (creatives with >=4 active days, n={len(hf)}):"
        f"\n  Pearson r  = {fmt_ci(hf_r_pt, hf_r_lo, hf_r_hi, 3)}"
        f"\n  Spearman r = {fmt_ci(hf_s_pt, hf_s_lo, hf_s_hi, 3)}"
        f"\n  Mean (H2 - H1) ROAS = {fmt_ci(hf_d_pt, hf_d_lo, hf_d_hi, 3)}"
    )

    # Variance decomposition of daily ROAS: between-creative vs total
    pos["c_mean_roas"] = pos.groupby("creative_id")["roas"].transform("mean")
    total_var = float(pos["roas"].var(ddof=1))
    within_var = float((pos["roas"] - pos["c_mean_roas"]).var(ddof=1))
    lines.append(
        f"\nDaily row ROAS variance decomposition (n={len(pos)} rows, {n_c} creatives): "
        f"total_var={total_var:.3f}, within_creative_var={within_var:.3f} "
        f"(within share={100*within_var/total_var:.1f}%, between share={100*(1 - within_var/total_var):.1f}%)"
    )

    # Plot
    fig, axes = plt.subplots(1, 3, figsize=(15, 4.6))
    c_sorted = c_agg.sort_values("roas")
    axes[0].barh(range(n_c), c_sorted["roas"], color="steelblue", alpha=0.85)
    axes[0].axvline(c_agg["total_rev"].sum() / c_agg["total_spend"].sum(), color="red", ls="--", lw=1, label="Spend-wtd mean")
    axes[0].set_title(f"Creative Lifetime ROAS (n={n_c})")
    axes[0].set_xlabel("Lifetime ROAS (Revenue / Spend)")
    axes[0].set_ylabel("Creatives (sorted)")
    axes[0].legend(fontsize=8)
    axes[0].grid(alpha=0.25)

    axes[1].scatter(oe["roas_odd"], oe["roas_even"], alpha=0.75, s=30)
    mx = max(oe["roas_odd"].max(), oe["roas_even"].max()) * 1.05
    axes[1].plot([0, mx], [0, mx], color="red", ls="--", lw=1)
    axes[1].set_title(f"Odd vs Even Day ROAS (n={len(oe)}, r={oe_r_pt:.2f})")
    axes[1].set_xlabel("Odd-day ROAS ($/$)")
    axes[1].set_ylabel("Even-day ROAS ($/$)")
    axes[1].grid(alpha=0.25)

    axes[2].scatter(hf["roas_h1"], hf["roas_h2"], alpha=0.75, s=30, color="teal")
    mx2 = max(hf["roas_h1"].max(), hf["roas_h2"].max()) * 1.05
    axes[2].plot([0, mx2], [0, mx2], color="red", ls="--", lw=1)
    axes[2].set_title(f"1st Half vs 2nd Half ROAS (n={len(hf)}, r={hf_r_pt:.2f})")
    axes[2].set_xlabel("1st-Half ROAS ($/$)")
    axes[2].set_ylabel("2nd-Half ROAS ($/$)")
    axes[2].grid(alpha=0.25)

    save_fig(fig, "03_roas_variation_noise.png")
    return "\n".join(lines), c_agg


# ---------------------------------------------------------------------------
# Q7: Creative attributes (format, hook_type, cta) LOO prediction
# ---------------------------------------------------------------------------
def loco_eval(c_agg: pd.DataFrame, col: str) -> dict[str, float]:
    """Leave-one-creative-out MSE/RMSE/MAE for group mean vs LOO global mean."""
    y = c_agg["roas"].to_numpy()
    groups = c_agg[col].to_numpy()
    n = len(y)
    pred_global = np.empty(n, dtype=float)
    pred_group = np.empty(n, dtype=float)
    n_fallback = 0
    for i in range(n):
        mask = np.ones(n, dtype=bool)
        mask[i] = False
        g_mean = y[mask].mean()
        pred_global[i] = g_mean
        same_g = mask & (groups == groups[i])
        if same_g.sum() > 0:
            pred_group[i] = y[same_g].mean()
        else:
            pred_group[i] = g_mean
            n_fallback += 1
    mse_glob = float(np.mean((y - pred_global) ** 2))
    mse_grp = float(np.mean((y - pred_group) ** 2))
    mae_glob = float(np.mean(np.abs(y - pred_global)))
    mae_grp = float(np.mean(np.abs(y - pred_group)))
    return {
        "n": float(n),
        "rmse_global": float(np.sqrt(mse_glob)),
        "rmse_group": float(np.sqrt(mse_grp)),
        "delta_rmse": float(np.sqrt(mse_grp) - np.sqrt(mse_glob)),
        "mae_global": mae_glob,
        "mae_group": mae_grp,
        "oos_r2": float(1.0 - mse_grp / mse_glob) if mse_glob > 0 else float("nan"),
        "n_fallback": float(n_fallback),
    }


def analyze_q7(creatives: pd.DataFrame, c_agg: pd.DataFrame) -> str:
    lines = ["=== Q7: CREATIVE ATTRIBUTES VS ROAS (LOCO TEST, n=40 served creatives) ==="]
    unserved = creatives[creatives["launch_day"].isna()]

    for col in ["format", "hook_type", "cta"]:
        lines.append(f"\nAttribute: {col}")
        grp = (
            c_agg.groupby(col, sort=True)
            .agg(
                n_served=("creative_id", "count"),
                mean_roas=("roas", "mean"),
                sd_roas=("roas", "std"),
                median_roas=("roas", "median"),
                pooled_roas=("total_rev", lambda s: float("nan")),
            )
            .reset_index()
        )
        pooled = (
            c_agg.groupby(col, sort=True)
            .agg(rev=("total_rev", "sum"), sp=("total_spend", "sum"))
            .reset_index()
        )
        pooled["pooled_roas"] = pooled["rev"] / pooled["sp"]
        grp = grp.drop(columns=["pooled_roas"]).merge(pooled[[col, "pooled_roas"]], on=col)
        un_cnt = unserved[col].value_counts().to_dict()

        for _, r in grp.iterrows():
            val = r[col]
            n_un = un_cnt.get(val, 0)
            small_flag = " [SMALL n]" if r["n_served"] < 5 else ""
            lines.append(
                f"  {str(val):22s}: n_served={int(r['n_served']):2d} (unserved={n_un:2d}), "
                f"mean_ROAS={r['mean_roas']:.3f} (sd={r['sd_roas']:.3f}), "
                f"median={r['median_roas']:.3f}, pooled={r['pooled_roas']:.3f}{small_flag}"
            )
        # Check for any category only in unserved
        for val, cnt in sorted(un_cnt.items()):
            if val not in set(grp[col]):
                lines.append(f"  {str(val):22s}: n_served= 0 (unserved={cnt:2d}) [UNSEEN IN SERVED]")

        res = loco_eval(c_agg, col)
        d_pt, d_lo, d_hi = creative_bootstrap_metric(c_agg, lambda d, c=col: loco_eval(d, c)["delta_rmse"])
        r2_pt, r2_lo, r2_hi = creative_bootstrap_metric(c_agg, lambda d, c=col: loco_eval(d, c)["oos_r2"])
        lines.append(
            f"  LOCO vs Global Mean (n={int(res['n'])}, fallback={int(res['n_fallback'])}): "
            f"RMSE_global={res['rmse_global']:.4f}, RMSE_{col}={res['rmse_group']:.4f}, "
            f"delta_RMSE={fmt_ci(d_pt, d_lo, d_hi, 4)} (negative=better), "
            f"OOS_R2={fmt_ci(r2_pt, r2_lo, r2_hi, 4)}, "
            f"MAE_global={res['mae_global']:.4f} vs MAE_{col}={res['mae_group']:.4f}"
        )

    # Plot
    fig, axes = plt.subplots(1, 3, figsize=(15, 4.8))
    global_mean = c_agg["roas"].mean()
    for ax, col in zip(axes, ["format", "hook_type", "cta"]):
        order = sorted(c_agg[col].unique())
        data_groups = [c_agg[c_agg[col] == v]["roas"].to_numpy() for v in order]
        ax.boxplot(data_groups, tick_labels=[f"{v}\n(n={len(g)})" for v, g in zip(order, data_groups)])
        ax.axhline(global_mean, color="red", ls="--", lw=1, label=f"Global mean ({global_mean:.2f})")
        loco = loco_eval(c_agg, col)
        ax.set_title(f"{col} vs ROAS (LOCO OOS R2={loco['oos_r2']:.3f})")
        ax.set_ylabel("Creative Lifetime ROAS ($/$)")
        ax.tick_params(axis="x", rotation=25, labelsize=8)
        ax.legend(fontsize=8)
        ax.grid(alpha=0.25)

    save_fig(fig, "07_creative_attributes.png")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Q4: Diminishing returns (within-creative spend vs ROAS)
# ---------------------------------------------------------------------------
def analyze_q4(creatives: pd.DataFrame, final: pd.DataFrame) -> str:
    lines = ["=== Q4: DIMINISHING RETURNS (WITHIN-CREATIVE SPEND VS ROAS, daily_final.csv) ==="]
    df = final.merge(creatives[["creative_id", "launch_day"]], on="creative_id", how="left")
    n_all = len(df)
    pos = df[df["spend"] > 0].copy()
    n_pos = len(pos)
    n_c = pos["creative_id"].nunique()
    pos["roas"] = pos["revenue"] / pos["spend"]
    pos["log_spend"] = np.log(pos["spend"])
    pos["ctr"] = np.where(pos["impressions"] > 0, pos["clicks"] / pos["impressions"], np.nan)
    pos["cvr"] = np.where(pos["clicks"] > 0, pos["purchases"] / pos["clicks"], np.nan)
    pos["cpm"] = np.where(pos["impressions"] > 0, 1000.0 * pos["spend"] / pos["impressions"], np.nan)
    pos["age"] = pos["day"] - pos["launch_day"]

    # Creative fixed effects (demean within creative)
    for col in ["roas", "spend", "log_spend", "ctr", "cvr", "cpm", "age"]:
        pos[f"dm_{col}"] = pos[col] - pos.groupby("creative_id")[col].transform("mean")

    lines.append(
        f"Sample: total_rows={n_all}, spend>0 rows={n_pos} (dropped {n_all-n_pos} zero-spend rows), "
        f"n_creatives={n_c}"
    )

    # Check if raw correlation (confounded across creatives) differs from within-creative
    raw_corr = pearson_corr(pos["spend"].to_numpy(), pos["roas"].to_numpy())
    raw_slope = ols_slope(pos["spend"].to_numpy() / 100.0, pos["roas"].to_numpy())
    lines.append(
        f"NAIVE pooled (confounded across creatives): corr(spend, ROAS)={raw_corr:.3f}, "
        f"slope per +$100={raw_slope:.3f}"
    )

    def fe_slope_spend(d: pd.DataFrame) -> float:
        x = (d["spend"] - d.groupby("creative_id")["spend"].transform("mean")).to_numpy() / 100.0
        y = (d["roas"] - d.groupby("creative_id")["roas"].transform("mean")).to_numpy()
        return ols_slope(x, y)

    def fe_slope_log_spend(d: pd.DataFrame) -> float:
        x = (d["log_spend"] - d.groupby("creative_id")["log_spend"].transform("mean")).to_numpy()
        y = (d["roas"] - d.groupby("creative_id")["roas"].transform("mean")).to_numpy()
        return ols_slope(x, y)

    def fe_corr_spend(d: pd.DataFrame) -> float:
        x = (d["spend"] - d.groupby("creative_id")["spend"].transform("mean")).to_numpy()
        y = (d["roas"] - d.groupby("creative_id")["roas"].transform("mean")).to_numpy()
        return pearson_corr(x, y)

    def fe_slope_cpm(d: pd.DataFrame) -> float:
        x = (d["spend"] - d.groupby("creative_id")["spend"].transform("mean")).to_numpy() / 100.0
        y = (d["cpm"] - d.groupby("creative_id")["cpm"].transform("mean")).to_numpy()
        return ols_slope(x, y)

    def fe_slope_ctr(d: pd.DataFrame) -> float:
        x = (d["spend"] - d.groupby("creative_id")["spend"].transform("mean")).to_numpy() / 100.0
        y = (d["ctr"] - d.groupby("creative_id")["ctr"].transform("mean")).to_numpy()
        return ols_slope(x, y)

    def fe_slope_cvr(d: pd.DataFrame) -> float:
        x = (d["spend"] - d.groupby("creative_id")["spend"].transform("mean")).to_numpy() / 100.0
        y = (d["cvr"] - d.groupby("creative_id")["cvr"].transform("mean")).to_numpy()
        return ols_slope(x, y)

    # Also control for age (creative FE + linear age residualized)
    def fe_slope_spend_age_adj(d: pd.DataFrame) -> float:
        x = (d["spend"] - d.groupby("creative_id")["spend"].transform("mean")).to_numpy() / 100.0
        a = (d["age"] - d.groupby("creative_id")["age"].transform("mean")).to_numpy()
        y = (d["roas"] - d.groupby("creative_id")["roas"].transform("mean")).to_numpy()
        # FWL: residualize x and y on a
        b_xa = ols_slope(a, x)
        b_ya = ols_slope(a, y)
        return ols_slope(x - b_xa * a, y - b_ya * a)

    s_pt, s_lo, s_hi = cluster_bootstrap_ci(pos, "creative_id", fe_slope_spend)
    c_pt, c_lo, c_hi = cluster_bootstrap_ci(pos, "creative_id", fe_corr_spend)
    ls_pt, ls_lo, ls_hi = cluster_bootstrap_ci(pos, "creative_id", fe_slope_log_spend)
    sa_pt, sa_lo, sa_hi = cluster_bootstrap_ci(pos, "creative_id", fe_slope_spend_age_adj)
    cpm_pt, cpm_lo, cpm_hi = cluster_bootstrap_ci(pos, "creative_id", fe_slope_cpm)
    ctr_pt, ctr_lo, ctr_hi = cluster_bootstrap_ci(pos, "creative_id", fe_slope_ctr)
    cvr_pt, cvr_lo, cvr_hi = cluster_bootstrap_ci(pos, "creative_id", fe_slope_cvr)

    lines.append(f"\nWITHIN-CREATIVE (creative fixed effects, cluster bootstrap across {n_c} creatives):")
    lines.append(f"  Slope ROAS per +$100 spend: {fmt_ci(s_pt, s_lo, s_hi, 3)}")
    lines.append(f"  Within-creative corr(spend, ROAS): {fmt_ci(c_pt, c_lo, c_hi, 3)}")
    lines.append(f"  Slope ROAS per +1 log(spend) (e.g. e-fold): {fmt_ci(ls_pt, ls_lo, ls_hi, 3)}")
    lines.append(f"  Age-adjusted slope ROAS per +$100 spend: {fmt_ci(sa_pt, sa_lo, sa_hi, 3)}")
    lines.append(f"  Mechanism checks (per +$100 spend within creative):")
    lines.append(f"    CPM ($/1k impr): {fmt_ci(cpm_pt, cpm_lo, cpm_hi, 3)}")
    lines.append(f"    CTR (clicks/impr): {fmt_ci(ctr_pt, ctr_lo, ctr_hi, 5)}")
    lines.append(f"    CVR (pur/clicks): {fmt_ci(cvr_pt, cvr_lo, cvr_hi, 4)}")

    # Binned within-creative demeaned spend (quintiles of dm_spend)
    pos["dm_spend_bin"] = pd.qcut(pos["dm_spend"], q=5, labels=["Q1 (lowest)", "Q2", "Q3", "Q4", "Q5 (highest)"])
    lines.append("\nWithin-creative demeaned ROAS by demeaned spend quintile:")
    bin_stats = []
    for lbl in ["Q1 (lowest)", "Q2", "Q3", "Q4", "Q5 (highest)"]:
        sub = pos[pos["dm_spend_bin"] == lbl]
        # Bootstrap mean of dm_roas across creatives for this bin
        m_pt, m_lo, m_hi = cluster_bootstrap_ci(
            pos,
            "creative_id",
            lambda d, l=lbl: d[
                (d["spend"] - d.groupby("creative_id")["spend"].transform("mean")).pipe(
                    lambda s: pd.qcut(s, q=5, labels=["Q1 (lowest)", "Q2", "Q3", "Q4", "Q5 (highest)"])
                )
                == l
            ].pipe(lambda sub_d: (sub_d["roas"] - sub_d["roas"].mean()).mean())
            if False
            else d.loc[d["dm_spend_bin"] == l, "dm_roas"].mean(),
        )
        lines.append(
            f"  {lbl:13s}: n_rows={len(sub):3d}, n_creatives={sub['creative_id'].nunique():2d}, "
            f"mean_spend=${sub['spend'].mean():6.1f} (dm={sub['dm_spend'].mean():+6.1f}), "
            f"mean_dm_ROAS={fmt_ci(m_pt, m_lo, m_hi, 3)}"
        )
        bin_stats.append((lbl, sub["dm_spend"].mean(), m_pt, m_lo, m_hi, len(sub)))

    # Plot
    fig, axes = plt.subplots(1, 2, figsize=(13, 4.8))
    ax = axes[0]
    xs = [b[1] for b in bin_stats]
    ys = [b[2] for b in bin_stats]
    yerr_lo = [b[2] - b[3] for b in bin_stats]
    yerr_hi = [b[4] - b[2] for b in bin_stats]
    ax.errorbar(xs, ys, yerr=[yerr_lo, yerr_hi], marker="o", capsize=4, lw=2)
    ax.axhline(0.0, color="gray", ls="--", lw=1)
    ax.axvline(0.0, color="gray", ls=":", lw=1)
    ax.set_title(f"Within-Creative Diminishing Returns (n={n_pos} rows, {n_c} creatives)")
    ax.set_xlabel("Spend Relative to Creative Mean ($/day)")
    ax.set_ylabel("ROAS Relative to Creative Mean ($/$)")
    ax.grid(alpha=0.25)

    # Also plot absolute spend bins after creative demeaning ROAS (spend <25, 25-50, 50-100, 100-150, 150+)
    abs_bins = [0, 25, 50, 100, 150, 10000]
    abs_labels = ["(0,25)", "[25,50)", "[50,100)", "[100,150)", "[150+)"]
    pos["abs_sp_bin"] = pd.cut(pos["spend"], bins=abs_bins, labels=abs_labels, right=False)
    ax2 = axes[1]
    ab_x, ab_y, ab_lo, ab_hi, ab_lbl = [], [], [], [], []
    lines.append("\nWithin-creative demeaned ROAS & CPM by absolute spend bin:")
    for lbl in abs_labels:
        sub = pos[pos["abs_sp_bin"] == lbl]
        if len(sub) == 0:
            continue
        pt, lo, hi = cluster_bootstrap_ci(
            pos, "creative_id", lambda d, l=lbl: d.loc[d["abs_sp_bin"] == l, "dm_roas"].mean()
        )
        cpm_m = sub["dm_cpm"].mean()
        lines.append(
            f"  {lbl:10s}: n_rows={len(sub):3d}, n_creatives={sub['creative_id'].nunique():2d}, "
            f"mean_dm_ROAS={fmt_ci(pt, lo, hi, 3)}, mean_dm_CPM=${cpm_m:+.2f}"
        )
        ab_x.append(len(ab_x))
        ab_y.append(pt)
        ab_lo.append(pt - lo)
        ab_hi.append(hi - pt)
        ab_lbl.append(f"{lbl}\n(n={len(sub)})")
    ax2.errorbar(ab_x, ab_y, yerr=[ab_lo, ab_hi], marker="s", capsize=4, lw=2, color="darkorange")
    ax2.axhline(0.0, color="gray", ls="--", lw=1)
    ax2.set_xticks(ab_x)
    ax2.set_xticklabels(ab_lbl, fontsize=8)
    ax2.set_title("Demeaned ROAS by Daily Spend Bin (Creative FE)")
    ax2.set_xlabel("Daily Spend Bin ($)")
    ax2.set_ylabel("ROAS Relative to Creative Mean ($/$)")
    ax2.grid(alpha=0.25)

    save_fig(fig, "04_diminishing_returns.png")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Q5: Fatigue (ROAS and CTR vs days since launch)
# ---------------------------------------------------------------------------
def analyze_q5(creatives: pd.DataFrame, final: pd.DataFrame) -> str:
    lines = ["=== Q5: FATIGUE (WITHIN-CREATIVE ROAS & CTR VS DAYS SINCE LAUNCH) ==="]
    df = final.merge(creatives[["creative_id", "launch_day"]], on="creative_id", how="left")
    pos = df[df["spend"] > 0].copy()
    pos["age"] = pos["day"] - pos["launch_day"]
    pos["roas"] = pos["revenue"] / pos["spend"]
    pos["ctr"] = np.where(pos["impressions"] > 0, pos["clicks"] / pos["impressions"], np.nan)
    pos["cvr"] = np.where(pos["clicks"] > 0, pos["purchases"] / pos["clicks"], np.nan)
    pos["cpm"] = np.where(pos["impressions"] > 0, 1000.0 * pos["spend"] / pos["impressions"], np.nan)

    # Check continuity from launch_day to day 60
    gaps = 0
    for cid, sub in df.groupby("creative_id"):
        ld = int(sub["launch_day"].iloc[0])
        expected = set(range(ld, 61))
        if set(sub["day"]) != expected:
            gaps += 1
    lines.append(
        f"Sample: n_rows={len(pos)}, n_creatives={pos['creative_id'].nunique()}, "
        f"creatives_with_day_gaps_from_launch_to_60={gaps}"
    )

    for col in ["roas", "ctr", "cvr", "cpm", "spend"]:
        pos[f"dm_{col}"] = pos[col] - pos.groupby("creative_id")[col].transform("mean")

    def fe_age_slope(d: pd.DataFrame, target: str, scale: float = 10.0) -> float:
        x = (d["age"] - d.groupby("creative_id")["age"].transform("mean")).to_numpy() / scale
        y = (d[target] - d.groupby("creative_id")[target].transform("mean")).to_numpy()
        return ols_slope(x, y)

    r_pt, r_lo, r_hi = cluster_bootstrap_ci(pos, "creative_id", lambda d: fe_age_slope(d, "roas", 10.0))
    c_pt, c_lo, c_hi = cluster_bootstrap_ci(pos, "creative_id", lambda d: fe_age_slope(d, "ctr", 10.0))
    cv_pt, cv_lo, cv_hi = cluster_bootstrap_ci(pos, "creative_id", lambda d: fe_age_slope(d, "cvr", 10.0))
    cp_pt, cp_lo, cp_hi = cluster_bootstrap_ci(pos, "creative_id", lambda d: fe_age_slope(d, "cpm", 10.0))
    sp_pt, sp_lo, sp_hi = cluster_bootstrap_ci(pos, "creative_id", lambda d: fe_age_slope(d, "spend", 10.0))

    lines.append("\nWithin-creative linear slopes per +10 days since launch (creative cluster bootstrap):")
    lines.append(f"  ROAS slope (/10d): {fmt_ci(r_pt, r_lo, r_hi, 3)}")
    lines.append(f"  CTR  slope (/10d): {fmt_ci(c_pt, c_lo, c_hi, 5)} (in pct pts: {100*c_pt:+.3f} pp/10d)")
    lines.append(f"  CVR  slope (/10d): {fmt_ci(cv_pt, cv_lo, cv_hi, 4)}")
    lines.append(f"  CPM  slope (/10d): {fmt_ci(cp_pt, cp_lo, cp_hi, 3)}")
    lines.append(f"  Spend slope(/10d): {fmt_ci(sp_pt, sp_lo, sp_hi, 2)}")

    # 7-day age bins
    age_edges = [0, 7, 14, 21, 28, 35, 42, 49, 65]
    age_labels = ["0-6", "7-13", "14-20", "21-27", "28-34", "35-41", "42-48", "49+"]
    pos["age_bin"] = pd.cut(pos["age"], bins=age_edges, labels=age_labels, right=False)
    lines.append("\nBinned by days since launch (creative FE demeaned):")
    b_rows = []
    for lbl in age_labels:
        sub = pos[pos["age_bin"] == lbl]
        nc = sub["creative_id"].nunique()
        if len(sub) == 0:
            continue
        rm, rlo, rhi = cluster_bootstrap_ci(pos, "creative_id", lambda d, l=lbl: d.loc[d["age_bin"] == l, "dm_roas"].mean())
        cm, clo, chi = cluster_bootstrap_ci(pos, "creative_id", lambda d, l=lbl: d.loc[d["age_bin"] == l, "dm_ctr"].mean())
        small = " [SMALL n_c]" if nc < 10 else ""
        lines.append(
            f"  age {lbl:5s}: n_rows={len(sub):3d}, n_creatives={nc:2d}, "
            f"dm_ROAS={fmt_ci(rm, rlo, rhi, 3)}, "
            f"dm_CTR={fmt_ci(100*cm, 100*clo, 100*chi, 3)} pp, "
            f"mean_spend=${sub['spend'].mean():5.1f}{small}"
        )
        b_rows.append((lbl, len(sub), nc, rm, rlo, rhi, 100 * cm, 100 * clo, 100 * chi))

    fig, axes = plt.subplots(1, 2, figsize=(13, 4.8))
    xs = list(range(len(b_rows)))
    x_lbls = [f"{r[0]}\n(c={r[2]})" for r in b_rows]

    axes[0].errorbar(
        xs,
        [r[3] for r in b_rows],
        yerr=[[r[3] - r[4] for r in b_rows], [r[5] - r[3] for r in b_rows]],
        marker="o",
        capsize=4,
        lw=2,
    )
    axes[0].axhline(0.0, color="gray", ls="--", lw=1)
    axes[0].set_xticks(xs)
    axes[0].set_xticklabels(x_lbls, fontsize=8)
    axes[0].set_title(f"Within-Creative ROAS vs Days Since Launch (n={len(pos)})")
    axes[0].set_xlabel("Days Since Launch (c = # creatives)")
    axes[0].set_ylabel("Demeaned ROAS ($/$)")
    axes[0].grid(alpha=0.25)

    axes[1].errorbar(
        xs,
        [r[6] for r in b_rows],
        yerr=[[r[6] - r[7] for r in b_rows], [r[8] - r[6] for r in b_rows]],
        marker="s",
        capsize=4,
        lw=2,
        color="purple",
    )
    axes[1].axhline(0.0, color="gray", ls="--", lw=1)
    axes[1].set_xticks(xs)
    axes[1].set_xticklabels(x_lbls, fontsize=8)
    axes[1].set_title(f"Within-Creative CTR vs Days Since Launch (n={len(pos)})")
    axes[1].set_xlabel("Days Since Launch (c = # creatives)")
    axes[1].set_ylabel("Demeaned CTR (percentage points)")
    axes[1].grid(alpha=0.25)

    save_fig(fig, "05_fatigue.png")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Q6: Day-of-week effect
# ---------------------------------------------------------------------------
def analyze_q6(final: pd.DataFrame) -> str:
    lines = ["=== Q6: DAY-OF-WEEK EFFECT (day 1 = Monday, daily_final.csv) ==="]
    pos = final[final["spend"] > 0].copy()
    pos["dow"] = (pos["day"] - 1) % 7
    pos["roas"] = pos["revenue"] / pos["spend"]
    pos["ctr"] = np.where(pos["impressions"] > 0, pos["clicks"] / pos["impressions"], np.nan)
    pos["cpm"] = np.where(pos["impressions"] > 0, 1000.0 * pos["spend"] / pos["impressions"], np.nan)
    pos["cvr"] = np.where(pos["clicks"] > 0, pos["purchases"] / pos["clicks"], np.nan)
    for col in ["roas", "ctr", "cpm", "cvr"]:
        pos[f"dm_{col}"] = pos[col] - pos.groupby("creative_id")[col].transform("mean")

    lines.append(f"Sample: n_rows={len(pos)}, n_creatives={pos['creative_id'].nunique()}")
    rows = []
    for d_idx, d_name in enumerate(DOW_NAMES):
        sub = pos[pos["dow"] == d_idx]
        rm, rlo, rhi = cluster_bootstrap_ci(pos, "creative_id", lambda d, k=d_idx: d.loc[d["dow"] == k, "dm_roas"].mean())
        cm, clo, chi = cluster_bootstrap_ci(pos, "creative_id", lambda d, k=d_idx: d.loc[d["dow"] == k, "dm_cpm"].mean())
        ct, ctlo, cthi = cluster_bootstrap_ci(pos, "creative_id", lambda d, k=d_idx: d.loc[d["dow"] == k, "dm_ctr"].mean())
        lines.append(
            f"  {d_name}: n_rows={len(sub):3d}, n_creatives={sub['creative_id'].nunique():2d}, "
            f"raw_ROAS={sub['roas'].mean():.3f}, "
            f"dm_ROAS={fmt_ci(rm, rlo, rhi, 3)}, "
            f"dm_CPM=${cm:+.2f} [{clo:+.2f}, {chi:+.2f}], "
            f"dm_CTR={100*ct:+.3f}pp"
        )
        rows.append((d_name, len(sub), rm, rlo, rhi, cm, clo, chi))

    wknd_pt, wknd_lo, wknd_hi = cluster_bootstrap_ci(
        pos,
        "creative_id",
        lambda d: d.loc[d["dow"] >= 5, "dm_roas"].mean() - d.loc[d["dow"] < 5, "dm_roas"].mean(),
    )
    lines.append(f"\nWeekend (Sat+Sun) minus Weekday (Mon-Fri) dm_ROAS: {fmt_ci(wknd_pt, wknd_lo, wknd_hi, 3)}")

    fig, axes = plt.subplots(1, 2, figsize=(12.5, 4.6))
    xs = list(range(7))
    x_lbls = [f"{r[0]}\n(n={r[1]})" for r in rows]
    axes[0].errorbar(
        xs,
        [r[2] for r in rows],
        yerr=[[r[2] - r[3] for r in rows], [r[4] - r[2] for r in rows]],
        marker="o",
        capsize=4,
        lw=2,
    )
    axes[0].axhline(0.0, color="gray", ls="--", lw=1)
    axes[0].set_xticks(xs)
    axes[0].set_xticklabels(x_lbls, fontsize=8)
    axes[0].set_title(f"Demeaned ROAS by Day of Week (n={len(pos)} rows)")
    axes[0].set_ylabel("ROAS Relative to Creative Mean ($/$)")
    axes[0].grid(alpha=0.25)

    axes[1].errorbar(
        xs,
        [r[5] for r in rows],
        yerr=[[r[5] - r[6] for r in rows], [r[7] - r[5] for r in rows]],
        marker="s",
        capsize=4,
        lw=2,
        color="seagreen",
    )
    axes[1].axhline(0.0, color="gray", ls="--", lw=1)
    axes[1].set_xticks(xs)
    axes[1].set_xticklabels(x_lbls, fontsize=8)
    axes[1].set_title(f"Demeaned CPM by Day of Week (n={len(pos)} rows)")
    axes[1].set_ylabel("CPM Relative to Creative Mean ($/1k impr)")
    axes[1].grid(alpha=0.25)

    save_fig(fig, "06_day_of_week.png")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Q8: Competitor launches
# ---------------------------------------------------------------------------
def analyze_q8(creatives: pd.DataFrame, final: pd.DataFrame, competitors: pd.DataFrame) -> str:
    lines = ["=== Q8: COMPETITOR LAUNCHES VS OUR ROAS & CPM (daily_final.csv) ==="]
    lines.append(
        f"competitor_launches.csv: n={len(competitors)} rows, days {int(competitors['day'].min())}..{int(competitors['day'].max())}"
    )
    lines.append(f"  by platform: {competitors['platform'].value_counts().to_dict()}")
    lines.append(f"  by format: {competitors['format'].value_counts().to_dict()}")
    lines.append(f"  by hook_type: {competitors['hook_type'].value_counts().to_dict()}")

    pos = final[final["spend"] > 0].merge(
        creatives[["creative_id", "format", "hook_type"]], on="creative_id", how="left"
    )
    pos["roas"] = pos["revenue"] / pos["spend"]
    pos["cpm"] = np.where(pos["impressions"] > 0, 1000.0 * pos["spend"] / pos["impressions"], np.nan)
    pos["dm_roas"] = pos["roas"] - pos.groupby("creative_id")["roas"].transform("mean")
    pos["dm_cpm"] = pos["cpm"] - pos.groupby("creative_id")["cpm"].transform("mean")

    # Build exposure flags for lookback windows in days before day t:
    # [1..3] (strictly before, 1-3d), [1..7] (1-7d), [0..3], [0..7]
    comp_records = competitors[["day", "format", "hook_type"]].to_dict("records")
    windows = [("prev_1_3d", 1, 3), ("prev_1_7d", 1, 7), ("win_0_3d", 0, 3), ("win_0_7d", 0, 7)]

    plot_rows = []
    for w_name, lo_d, hi_d in windows:
        lines.append(f"\nLookback window {w_name} (comp_day in [day-{hi_d}, day-{lo_d}]):")
        for kind in ["same_hook", "same_format", "any_launch"]:
            flags = np.zeros(len(pos), dtype=int)
            days_arr = pos["day"].to_numpy()
            fmt_arr = pos["format"].to_numpy()
            hook_arr = pos["hook_type"].to_numpy()
            for i in range(len(pos)):
                d_i = days_arr[i]
                cnt = 0
                for c in comp_records:
                    lag = d_i - c["day"]
                    if lo_d <= lag <= hi_d:
                        if kind == "any_launch":
                            cnt += 1
                        elif kind == "same_hook" and c["hook_type"] == hook_arr[i]:
                            cnt += 1
                        elif kind == "same_format" and c["format"] == fmt_arr[i]:
                            cnt += 1
                flags[i] = 1 if cnt > 0 else 0
            col_name = f"{w_name}_{kind}"
            pos[col_name] = flags
            n_exp = int(flags.sum())
            n_unexp = len(pos) - n_exp
            n_c_exp = int(pos.loc[pos[col_name] == 1, "creative_id"].nunique())

            if n_exp == 0 or n_unexp == 0:
                lines.append(f"  {kind:11s}: n_exp={n_exp}, n_unexp={n_unexp} (skipped)")
                continue

            roas_pt, roas_lo, roas_hi = cluster_bootstrap_ci(
                pos,
                "creative_id",
                lambda d, c=col_name: d.loc[d[c] == 1, "dm_roas"].mean() - d.loc[d[c] == 0, "dm_roas"].mean(),
            )
            cpm_pt, cpm_lo, cpm_hi = cluster_bootstrap_ci(
                pos,
                "creative_id",
                lambda d, c=col_name: d.loc[d[c] == 1, "dm_cpm"].mean() - d.loc[d[c] == 0, "dm_cpm"].mean(),
            )
            lines.append(
                f"  {kind:11s}: n_exp={n_exp:4d} ({n_c_exp:2d} creatives), n_unexp={n_unexp:4d} | "
                f"diff dm_ROAS={fmt_ci(roas_pt, roas_lo, roas_hi, 3)} | "
                f"diff dm_CPM=${fmt_ci(cpm_pt, cpm_lo, cpm_hi, 3)}"
            )
            if w_name in ("prev_1_3d", "prev_1_7d"):
                plot_rows.append(
                    (f"{w_name}\n{kind}\n(n={n_exp})", roas_pt, roas_lo, roas_hi, cpm_pt, cpm_lo, cpm_hi)
                )

    fig, axes = plt.subplots(1, 2, figsize=(13.5, 4.8))
    xs = list(range(len(plot_rows)))
    x_lbls = [r[0] for r in plot_rows]
    axes[0].errorbar(
        xs,
        [r[1] for r in plot_rows],
        yerr=[[r[1] - r[2] for r in plot_rows], [r[3] - r[1] for r in plot_rows]],
        fmt="o",
        capsize=4,
        lw=2,
    )
    axes[0].axhline(0.0, color="gray", ls="--", lw=1)
    axes[0].set_xticks(xs)
    axes[0].set_xticklabels(x_lbls, fontsize=8)
    axes[0].set_title(f"Exposed minus Unexposed Demeaned ROAS (n={len(pos)})")
    axes[0].set_ylabel("Diff in Demeaned ROAS ($/$)")
    axes[0].grid(alpha=0.25)

    axes[1].errorbar(
        xs,
        [r[4] for r in plot_rows],
        yerr=[[r[4] - r[5] for r in plot_rows], [r[6] - r[4] for r in plot_rows]],
        fmt="s",
        capsize=4,
        lw=2,
        color="crimson",
    )
    axes[1].axhline(0.0, color="gray", ls="--", lw=1)
    axes[1].set_xticks(xs)
    axes[1].set_xticklabels(x_lbls, fontsize=8)
    axes[1].set_title(f"Exposed minus Unexposed Demeaned CPM (n={len(pos)})")
    axes[1].set_ylabel("Diff in Demeaned CPM ($/1k impr)")
    axes[1].grid(alpha=0.25)

    save_fig(fig, "08_competitor_launches.png")
    return "\n".join(lines)


def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    creatives, reported, final, competitors = load_data()

    q1_txt = analyze_q1(reported, final)
    q2_txt = analyze_q2(final)
    q3_txt, c_agg = analyze_q3(creatives, final)
    q7_txt = analyze_q7(creatives, c_agg)

    q4_txt = analyze_q4(creatives, final)
    q5_txt = analyze_q5(creatives, final)
    q6_txt = analyze_q6(final)
    q8_txt = analyze_q8(creatives, final, competitors)

    part1 = "\n\n".join([q1_txt, q2_txt, q3_txt, q7_txt]) + "\n"
    part2 = "\n\n".join([q4_txt, q5_txt, q6_txt, q8_txt]) + "\n"
    full = part1 + "\n" + part2

    (OUT_DIR / "part1_q1_q2_q3_q7.txt").write_text(part1, encoding="utf-8")
    (OUT_DIR / "part2_q4_q5_q6_q8.txt").write_text(part2, encoding="utf-8")
    (OUT_DIR / "summary.txt").write_text(full, encoding="utf-8")

    print(full)


if __name__ == "__main__":
    main()

